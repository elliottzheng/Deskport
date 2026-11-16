# -*- coding: utf-8 -*-
"""智能体令牌管理面（设置页「智能体令牌」卡片，批次 TK）。

覆盖: 管理接口只认网页会话（全权令牌打过来也 401）、签发后真打一次
/api/agent/hosts、明文只出现一次（GET 响应体正则扫 tokenHash/长 base64）、
非法 scopes 400 并列可选、名称校验与重名 400、超 10 条 400、删最后一条
全权令牌被拒、吊销后立刻 401、权限档位生效（share/upload+share 打 exec
403 need=full）、只落 hash 不落明文、审计。全部桩化，不碰真实配置。
"""
import hashlib
import json
import re
import types
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_api, events

PASSWORD = 'pw-123456'
LEAK_RE = re.compile(r'tokenHash|[A-Za-z0-9_-]{40,}')
FAKE_HOST = {'id': 'h1', 'name': 'ai-x', 'host': '10.0.0.9', 'port': 22}


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def fakeLog(action, target='', result='ok', detail='', ip=''):
        audit.append({'action': action, 'target': target, 'result': result,
                      'detail': detail, 'ip': ip})

    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(events, 'log', fakeLog)
    configMod._cache.update(mtime=None, data=None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    try:
        yield SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                              app=app, audit=audit, tmp=tmp_path,
                              monkeypatch=monkeypatch)
    finally:
        configMod._cache.update(mtime=None, data=None)


def session(client):
    client.post('/login', data={'password': PASSWORD})


def create(client, name='Kimi', scopes=None):
    body = {'name': name}
    if scopes is not None:
        body['scopes'] = scopes
    return client.post('/api/settings/agent-tokens',
                       data=json.dumps(body), content_type='application/json')


def listed(client):
    return client.get('/api/settings/agent-tokens').get_json()['tokens']


def auditOf(env, action):
    return [a for a in env.audit if a['action'] == action]


def stubExec(monkeypatch, calls):
    monkeypatch.setattr(agent_api, 'hosts', types.SimpleNamespace(
        findHost=lambda hostId: FAKE_HOST,
        repo=SimpleNamespace(all=lambda: [FAKE_HOST])))
    monkeypatch.setattr(agent_api, '_execOnHost',
                        lambda entry, command, timeoutSec, cwd='':
                        calls.append(command) or
                        {'exit': 0, 'out': b'hi\n', 'err': b'',
                         'timedOut': False, 'reason': '', 'pid': None,
                         'truncated': False})


# ---------------------------------------------------------------- 鉴权

def test_management_endpoints_require_session(env):
    assert env.client.get('/api/settings/agent-tokens').status_code == 401
    assert create(env.client).status_code == 401
    assert env.client.delete('/api/settings/agent-tokens/x').status_code == 401


def test_agent_token_cannot_reach_management(env):
    """管理面是网页会话接口: 有效全权令牌打过来一样 401。"""
    session(env.client)
    token = create(env.client, 'Claude').get_json()['token']
    headers = {'X-Agent-Token': token}
    assert env.fresh.get('/api/settings/agent-tokens', headers=headers).status_code == 401
    assert env.fresh.post('/api/settings/agent-tokens',
                          data=json.dumps({'name': 'x'}),
                          content_type='application/json',
                          headers=headers).status_code == 401
    assert env.fresh.delete('/api/settings/agent-tokens/x',
                            headers=headers).status_code == 401


# ---------------------------------------------------------------- 签发与使用

def test_created_token_works_immediately(env):
    session(env.client)
    body = create(env.client, 'Kimi').get_json()
    assert re.match(r'^[A-Za-z0-9_-]{20,}$', body['token'])
    assert body['name'] == 'Kimi' and body['scopes'] is None and body['id']
    # 真打一次: 新令牌直接访问智能体 API（写完热生效, 无需重启）
    resp = env.client.get('/api/agent/hosts',
                          headers={'X-Agent-Token': body['token']})
    assert resp.status_code == 200
    assert resp.get_json()['tokenName'] == 'Kimi'
    # 列表只含 {id, name, scopes, createdAt, lead}, 全权令牌 scopes 为 null
    rows = listed(env.client)
    assert [t['name'] for t in rows] == ['Kimi']
    assert set(rows[0]) == {'id', 'name', 'scopes', 'createdAt', 'lead'}
    assert rows[0]['scopes'] is None and rows[0]['createdAt']
    assert rows[0]['lead'] == ''   # 批次 Q：没绑主管为空串（键一直都在）


def test_plaintext_appears_only_in_create_response(env):
    session(env.client)
    body = create(env.client, ' once ').get_json()
    assert body['name'] == 'once'
    token = body['token']
    assert json.dumps(body).count(token) == 1
    raw = env.client.get('/api/settings/agent-tokens').get_data(as_text=True)
    assert token not in raw
    assert not LEAK_RE.search(raw), raw


def test_stores_only_hash(env):
    session(env.client)
    body = create(env.client, 'Kimi').get_json()
    text = (env.tmp / 'config.json').read_text(encoding='utf-8')
    stored = json.loads(text)['agentTokens']
    assert len(stored) == 1
    assert set(stored[0]) == {'id', 'name', 'tokenHash', 'createdAt'}
    assert stored[0]['tokenHash'] == \
        hashlib.sha256(body['token'].encode()).hexdigest()
    assert body['token'] not in text   # 明文不落盘
    assert text.count('"password"') == 1   # 其他配置键保留


def test_explicit_null_scopes_means_full(env):
    session(env.client)
    resp = env.client.post('/api/settings/agent-tokens',
                           data=json.dumps({'name': 'n', 'scopes': None}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert resp.get_json()['scopes'] is None
    assert listed(env.client)[0]['scopes'] is None


# ---------------------------------------------------------------- 校验

def test_invalid_scopes_400_with_options(env):
    session(env.client)
    bad = [['full'], ['upload'], ['upload', 'share', 'upload_remote'],
           ['admin'], 'share', ['share', 'share'], ['upload', 'upload'],
           [], [None]]
    for scopes in bad:
        resp = create(env.client, 't', scopes=scopes)
        assert resp.status_code == 400, scopes
        err = resp.get_json()['error']
        assert '全权' in err and 'upload' in err and 'share' in err
    assert listed(env.client) == []   # 校验失败不落盘


def test_valid_scope_tiers(env):
    session(env.client)
    assert create(env.client, 'a').get_json()['scopes'] is None
    assert sorted(create(env.client, 'b', ['upload', 'share'])
                  .get_json()['scopes']) == ['share', 'upload']
    assert create(env.client, 'c', ['share']).get_json()['scopes'] == ['share']
    assert create(env.client, 'd', ['share', 'upload']).status_code == 200  # 顺序无关
    assert [t['name'] for t in listed(env.client)] == ['a', 'b', 'c', 'd']


def test_name_validation_and_duplicate(env):
    session(env.client)
    for bad in ('', '   ', 'x' * 25, 'a<b', 'a"b', "a'b", 'a\nb', 'a\tb'):
        assert create(env.client, bad).status_code == 400, repr(bad)
    assert create(env.client, 'ok').status_code == 200
    assert create(env.client, 'ok').status_code == 400        # 重名
    assert create(env.client, ' ok ').status_code == 400      # strip 后重名
    assert [t['name'] for t in listed(env.client)] == ['ok']


def test_max_ten_tokens(env):
    session(env.client)
    for i in range(10):
        assert create(env.client, 't%d' % i).status_code == 200
    resp = create(env.client, 't10')
    assert resp.status_code == 400 and '10' in resp.get_json()['error']


# ---------------------------------------------------------------- 吊销

def test_cannot_revoke_last_full_token(env):
    session(env.client)
    full = create(env.client, 'full').get_json()
    share = create(env.client, 'share', ['share']).get_json()
    resp = env.client.delete('/api/settings/agent-tokens/' + full['id'])
    assert resp.status_code == 400 and '全权' in resp.get_json()['error']
    assert env.client.delete(
        '/api/settings/agent-tokens/' + share['id']).status_code == 200
    # 现在只剩 full: 再删仍被拒（两条全权时可删一条）
    assert env.client.delete(
        '/api/settings/agent-tokens/' + full['id']).status_code == 400
    second = create(env.client, 'full2').get_json()
    assert env.client.delete(
        '/api/settings/agent-tokens/' + second['id']).status_code == 200
    assert env.client.delete(
        '/api/settings/agent-tokens/nope').status_code == 404


def test_revoke_takes_effect_immediately(env):
    session(env.client)
    first = create(env.client, 'Kimi').get_json()
    second = create(env.client, 'Kimi2').get_json()
    headers = {'X-Agent-Token': first['token']}
    assert env.client.get('/api/agent/hosts', headers=headers).status_code == 200
    # 留一条全权兜底, 删另一条: 写完即热生效, 无需重启
    assert env.client.delete(
        '/api/settings/agent-tokens/' + first['id']).status_code == 200
    assert env.client.get('/api/agent/hosts', headers=headers).status_code == 401
    keep = {'X-Agent-Token': second['token']}
    assert env.client.get('/api/agent/hosts', headers=keep).status_code == 200
    assert [t['name'] for t in listed(env.client)] == ['Kimi2']


# ---------------------------------------------------------------- 权限档位

def test_scope_tiers_enforced(env):
    session(env.client)
    share = create(env.client, 'share', ['share']).get_json()['token']
    up = create(env.client, 'up', ['upload', 'share']).get_json()['token']
    full = create(env.client, 'full').get_json()['token']
    payload = {'host': 'ai-x', 'command': 'echo hi'}
    for token in (share, up):
        resp = env.client.post('/api/agent/ssh/exec',
                               headers={'X-Agent-Token': token},
                               data=json.dumps(payload),
                               content_type='application/json')
        assert resp.status_code == 403
        assert resp.get_json()['need'] == 'full'
    assert env.client.get('/api/agent/hosts',
                          headers={'X-Agent-Token': share}).status_code == 403
    # 全权令牌照常放行（exec 打桩, 不碰真实 SSH）
    calls = []
    stubExec(env.monkeypatch, calls)
    resp = env.client.post('/api/agent/ssh/exec',
                           headers={'X-Agent-Token': full},
                           data=json.dumps(payload),
                           content_type='application/json')
    assert resp.status_code == 200 and calls == ['echo hi']


# ---------------------------------------------------------------- 审计

def test_audit_trail(env):
    session(env.client)
    body = create(env.client, 'Kimi', ['share']).get_json()
    entry = auditOf(env, 'agent.token.created')[-1]
    assert entry['target'] == 'Kimi' and entry['result'] == 'ok'
    assert 'scopes=share' in entry['detail']
    env.client.delete('/api/settings/agent-tokens/' + body['id'])
    entry = auditOf(env, 'agent.token.revoked')[-1]
    assert entry['target'] == 'Kimi' and entry['result'] == 'ok'
