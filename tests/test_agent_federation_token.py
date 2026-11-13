# -*- coding: utf-8 -*-
"""联邦令牌当智能体令牌用（批次 FD）: X-Deskport-Token 命中本机
federationToken 时, /api/agent/* 与 /api/tasks* 按 full 放行, 审计记
「联邦:<来源>」（来源 = X-Deskport-From, 缺省来源 IP）。

覆盖: hosts 放行且 tokenName=联邦:*、本机 exec 200 且审计带联邦名、
无令牌/错令牌/联邦关闭 401、内置令牌优先（同值双配不歧义）、
full 语义（合成条目 scopes=['full'], need=upload 的端点也放行）、
Bearer 头同样认联邦令牌、任务 owner 与 agent.task.create 审计记联邦名、
联邦身份不能自吊销设备令牌（kind 不匹配 403）。
内置 agentTokens 与设备令牌的不回归由既有用例全量覆盖
（test_agent_api / test_device_login / test_agent_local 等）; 本文件
另留一条内置令牌链路断言（身份与审计格式不变）。
"""
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import config as configMod
from modules import agent_api, events, tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'a' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
FED_TOKEN = 'fed-' + 'f' * 24
FED_H = {'X-Deskport-Token': FED_TOKEN,
         'X-Deskport-From': quote('中枢A', safe='')}


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(configMod, 'getFederationToken', lambda: FED_TOKEN)
    monkeypatch.setattr(events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit, app=app,
                           tmp=tmp_path, monkeypatch=monkeypatch)


def post(env, path, body, headers):
    return env.client.post(path, headers=headers, data=json.dumps(body),
                           content_type='application/json')


# ---- 放行 ----

def test_hosts_accepts_federation_token(env):
    resp = env.client.get('/api/agent/hosts', headers=FED_H)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['tokenName'] == '联邦:中枢A'
    assert any(h['id'] == 'local' for h in body['hosts'])


def test_hosts_name_falls_back_to_ip_without_from(env):
    resp = env.client.get('/api/agent/hosts',
                          headers={'X-Deskport-Token': FED_TOKEN})
    assert resp.status_code == 200
    assert resp.get_json()['tokenName'] == '联邦:127.0.0.1'


def test_federation_token_via_bearer_header(env):
    """_tokenFromRequest 三级回退: Bearer 里放联邦令牌同样进智能体通道。"""
    resp = env.client.get('/api/agent/hosts',
                          headers={'Authorization': 'Bearer ' + FED_TOKEN})
    assert resp.status_code == 200
    assert resp.get_json()['tokenName'] == '联邦:127.0.0.1'


def test_local_exec_with_federation_token(env):
    """本机取 shell 的那条路: host='local' 真跑 Git Bash。"""
    resp = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'echo hi-fed; echo err-1 >&2'},
                FED_H)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['exit'] == 0
    assert 'hi-fed' in body['out'] and 'err-1' in body['err']
    execs = [a for a in env.audit if a['action'] == 'agent.exec']
    assert execs and execs[-1]['target'] == 'local(bash)'
    assert '联邦:中枢A' in execs[-1]['detail'] and 'echo hi-fed' in execs[-1]['detail']


def test_federation_entry_is_full_scope(env):
    """合成条目可审计: kind/scopes/name 固定, full 覆盖一切受限 scope。"""
    with env.app.test_request_context('/api/agent/hosts', headers=FED_H):
        agent = agent_api.agentFromRequest()
    assert agent == {'id': 'federation', 'name': '联邦:中枢A',
                     'kind': 'federation', 'scopes': ['full']}
    for scope in (agent_api.SCOPE_FULL, agent_api.SCOPE_UPLOAD,
                  agent_api.SCOPE_SHARE, agent_api.SCOPE_UPLOAD_REMOTE):
        assert agent_api.agentHasScope(agent, scope)


def test_restricted_endpoint_need_upload_also_passes(env):
    """联邦令牌打 need=upload 的端点同样放行——它拿到的就是 full,
    不是受限身份（防止将来有人以为它会在这里 403）。"""
    resp = env.client.get('/api/agent/upload/targets', headers=FED_H)
    assert resp.status_code == 200
    assert [t['id'] for t in resp.get_json()['targets']] == ['phone']


def test_task_create_audit_shows_federation(env, monkeypatch):
    repo = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(env.tmp / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    resp = post(env, '/api/tasks', {'prompt': 'echo hi'}, FED_H)
    assert resp.status_code == 202
    assert resp.get_json()['task']['owner'] == '联邦:中枢A'
    creates = [a for a in env.audit if a['action'] == 'agent.task.create']
    assert creates and creates[-1]['target'] == '联邦:中枢A'


def test_federation_cannot_self_revoke_devices(env):
    resp = env.client.post('/api/agent/device/logout', headers=FED_H)
    assert resp.status_code == 403
    assert '仅设备令牌' in resp.get_json()['error']


# ---- 拒绝 ----

def test_rejections(env, monkeypatch):
    body = {'host': 'local', 'command': 'echo hi'}
    assert post(env, '/api/agent/ssh/exec', body, {}).status_code == 401
    assert post(env, '/api/agent/ssh/exec', body,
                {'X-Agent-Token': 'bad'}).status_code == 401
    assert post(env, '/api/agent/ssh/exec', body,
                {'X-Deskport-Token': 'bad-fed'}).status_code == 401
    # 联邦关闭（federationToken 留空）: 联邦通道恒不命中
    monkeypatch.setattr(configMod, 'getFederationToken', lambda: '')
    assert post(env, '/api/agent/ssh/exec', body,
                {'X-Deskport-Token': FED_TOKEN}).status_code == 401


# ---- 不回归: 内置令牌 ----

def test_builtin_token_identity_unchanged(env):
    resp = env.client.get('/api/agent/hosts',
                          headers={'X-Agent-Token': TOKEN})
    assert resp.status_code == 200
    assert resp.get_json()['tokenName'] == '测试智能体'
    assert post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'echo hi-builtin'},
                {'X-Agent-Token': TOKEN}).status_code == 200
    execs = [a for a in env.audit if a['action'] == 'agent.exec']
    assert execs[-1]['detail'].startswith('[测试智能体] ')


def test_builtin_wins_when_same_value_configured_twice(env, monkeypatch):
    """同一值同时配成 agentToken 与 federationToken: 内置优先,
    身份记内置名, 不出现「联邦来源」的歧义命名。"""
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 'dual', 'name': '双配令牌',
                                  'tokenHash': hashlib.sha256(
                                      FED_TOKEN.encode()).hexdigest()}])
    resp = env.client.get('/api/agent/hosts', headers=FED_H)
    assert resp.status_code == 200
    assert resp.get_json()['tokenName'] == '双配令牌'
