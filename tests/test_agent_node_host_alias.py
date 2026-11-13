# -*- coding: utf-8 -*-
"""批次 J：`/api/agent/*` 的机器参数 node 与 host 同义（兼容优先，不破坏性改名）。

归一在一处（`agent_api.nodeArg`）：优先 node，其次 host，都缺省 = local；
两个都给且不同 → 400 且文案含「冲突」。响应体 node/host 两个键都回。
本文件覆盖 exec 与 sftp（list/put）五种组合；会话、upload、share/stage、
写操作的覆盖在各自回归集里（host 旧名继续 200）。
"""
import base64
import json

from modules import agent_api
from test_agent_api import (TOKEN, FakeSftp, installExecSpy,  # noqa: F401
                            installSftp, makeFakeHostsModule)
from test_agent_api import env as envFixture

env = envFixture   # 复用 test_agent_api 的夹具（令牌/审计/app），不复制第二份

H = {'X-Agent-Token': TOKEN}

ENTRIES = [{'id': 'h1', 'name': 'ai-xmyz', 'host': '10.0.0.9', 'port': 22}]


def _exec(env, body):
    return env.client.post('/api/agent/ssh/exec', headers=H,
                           data=json.dumps(body),
                           content_type='application/json')


def _sftp(env, path, body):
    return env.client.post(path, headers=H, data=json.dumps(body),
                           content_type='application/json')


def test_node_arg_unit(env):
    """归一函数本体：四种组合；冲突 → node=None + 400 应答，文案含「冲突」。"""
    assert agent_api.nodeArg({'node': 'a'}) == ('a', None)
    assert agent_api.nodeArg({'host': 'a'}) == ('a', None)
    assert agent_api.nodeArg({'node': 'a', 'host': 'a'}) == ('a', None)
    assert agent_api.nodeArg({}) == ('local', None)
    with env.client.application.app_context():
        node, err = agent_api.nodeArg({'node': 'a', 'host': 'b'})
        resp, status = err
    assert node is None and status == 400
    assert '冲突' in resp.get_json()['error']


# ---- exec：五种组合 ----

def test_exec_node_alias_ok(env, monkeypatch):
    """新名 node 直接可用。"""
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(ENTRIES))
    installExecSpy(monkeypatch, calls)
    resp = _exec(env, {'node': 'ai-xmyz', 'command': 'uptime'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['exit'] == 0 and 'ok-out' in body['out']
    assert body['node'] == 'ai-xmyz' and body['host'] == 'ai-xmyz'
    assert calls and calls[0]['command'] == 'uptime'


def test_exec_host_alias_still_ok(env, monkeypatch):
    """旧名 host 仍可用（手机 App 与老客户端不改也能跑）。"""
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(ENTRIES))
    installExecSpy(monkeypatch, calls)
    resp = _exec(env, {'host': 'ai-xmyz', 'command': 'uptime'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['node'] == 'ai-xmyz' and body['host'] == 'ai-xmyz'
    assert calls and calls[0]['command'] == 'uptime'


def test_exec_node_and_host_same_ok(env, monkeypatch):
    """两个都给且相同 = 不冲突，照常放行。"""
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(ENTRIES))
    installExecSpy(monkeypatch, calls)
    resp = _exec(env, {'node': 'ai-xmyz', 'host': 'ai-xmyz', 'command': 'id'})
    assert resp.status_code == 200
    assert calls and calls[0]['command'] == 'id'


def test_exec_node_and_host_conflict_400(env, monkeypatch):
    """两个都给且不同 → 400 说清冲突，且绝不猜（不碰执行器）。"""
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(ENTRIES))
    installExecSpy(monkeypatch, calls)
    resp = _exec(env, {'node': 'ai-xmyz', 'host': 'other', 'command': 'uptime'})
    assert resp.status_code == 400
    assert '冲突' in resp.get_json()['error']
    assert calls == []


def test_exec_defaults_to_local_when_both_missing(env, monkeypatch):
    """都不给 → local（与 /api/tasks 的 node 缺省一致）。"""
    from modules import agent_local
    monkeypatch.setattr(agent_local, 'execLocal',
                        lambda command, timeoutSec, cwd='', shell='bash':
                        {'exit': 0, 'out': b'local-ok\n', 'err': b'',
                         'timedOut': False, 'reason': '', 'pid': 7,
                         'truncated': False, 'durationMs': 1})
    resp = _exec(env, {'command': 'echo hi'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['node'] == 'local' and body['host'] == 'local'
    assert 'local-ok' in body['out']


# ---- sftp：node/host 两个名 + 冲突 + 响应双键 ----

def test_sftp_list_node_alias_ok(env, monkeypatch):
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/a.txt'] = b'x'
    installSftp(monkeypatch, fake)
    resp = _sftp(env, '/api/agent/sftp/list', {'node': 'office', 'path': '/srv'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['node'] == 'office' and body['host'] == 'office'
    assert [e['name'] for e in body['entries']] == ['a.txt']


def test_sftp_list_host_alias_still_ok(env, monkeypatch):
    fake = FakeSftp()
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)
    resp = _sftp(env, '/api/agent/sftp/list', {'host': 'office', 'path': '/srv'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['node'] == 'office' and body['host'] == 'office'


def test_sftp_put_node_alias_ok(env, monkeypatch):
    fake = FakeSftp()
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)
    resp = _sftp(env, '/api/agent/sftp/put',
                 {'node': 'office', 'path': '/srv/n.txt',
                  'contentB64': base64.b64encode(b'body').decode()})
    assert resp.status_code == 200
    body = resp.get_json()
    assert fake.files['/srv/n.txt'] == b'body'
    assert body['node'] == 'office' and body['host'] == 'office'
    assert body['sha256']


def test_sftp_node_and_host_conflict_400(env, monkeypatch):
    fake = FakeSftp()
    installSftp(monkeypatch, fake)
    resp = _sftp(env, '/api/agent/sftp/list',
                 {'node': 'office', 'host': 'other', 'path': '/srv'})
    assert resp.status_code == 400
    assert '冲突' in resp.get_json()['error']
    assert fake.files == {}


def test_sftp_get_node_alias_ok(env, monkeypatch):
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/a.txt'] = b'hello'
    installSftp(monkeypatch, fake)
    resp = _sftp(env, '/api/agent/sftp/get', {'node': 'office', 'path': '/srv/a.txt'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert base64.b64decode(body['contentB64']) == b'hello'
    assert body['node'] == 'office' and body['host'] == 'office'
