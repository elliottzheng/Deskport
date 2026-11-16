# -*- coding: utf-8 -*-
"""批次 G1：MCP 提交面（POST /api/mcp）——initialize/tools/list/tools/call
三步协议、按 expects 动态 schema、令牌鉴权、校验错误以 isError 返回。

kimi 实测（agentCapabilities.mcpCapabilities={http,sse}）：客户端走 MCP over
HTTP，JSON-RPC 2.0 单端点，令牌放 X-Deskport-Session-Token 头。
"""
import json

import pytest
from types import SimpleNamespace

import config as configMod
from modules import events as eventsMod
from modules import task_result, task_result_mcp
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
IMPL_OK = {'summary': '交付了 X', 'commit': 'abc1234',
           'tests': {'passed': 3, 'failed': 0, 'skipped': 0}}
VERIFY_OK = {'summary': '验过了', 'verdict': 'pass', 'target': '0badf00d'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def newTask(env, expects='impl'):
    resp = env.client.post('/api/tasks',
                           data=json.dumps({'prompt': 'x', 'expects': expects}),
                           content_type='application/json')
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def rpc(env, method, params=None, mid=1, token='tok'):
    body = {'jsonrpc': '2.0', 'id': mid, 'method': method}
    if params is not None:
        body['params'] = params
    return env.client.post('/api/mcp', data=json.dumps(body),
                           content_type='application/json',
                           headers={'X-Deskport-Session-Token': token})


def test_handshake_tools_list_call(env):
    """三步：initialize → tools/list → tools/call，结果落库。"""
    login(env.client)
    tid = newTask(env, 'impl')
    token = task_result_mcp.issueToken(tid, 'impl')
    init = rpc(env, 'initialize', {'protocolVersion': '2025-11-25'}, mid=0,
               token=token).get_json()
    assert init['result']['serverInfo']['name'] == 'deskport-result'
    assert init['result']['capabilities'] == {'tools': {}}
    listing = rpc(env, 'tools/list', mid=1, token=token).get_json()
    tools = listing['result']['tools']
    assert [t['name'] for t in tools] == ['submit_result']
    assert 'impl' in tools[0]['description'] or '实施' in tools[0]['description']
    called = rpc(env, 'tools/call',
                 {'name': 'submit_result', 'arguments': IMPL_OK},
                 mid=2, token=token).get_json()
    assert called['result'].get('isError') in (None, False)
    assert '结果已提交' in called['result']['content'][0]['text']
    assert env.repo.get(tid)['result']['commit'] == 'abc1234'


def test_tools_list_schema_differs_by_expects(env):
    """impl 与 verify 的入参 schema 不同（required 与字段集）。"""
    login(env.client)
    implTid = newTask(env, 'impl')
    verifyTid = newTask(env, 'verify')
    implTok = task_result_mcp.issueToken(implTid, 'impl')
    verifyTok = task_result_mcp.issueToken(verifyTid, 'verify')
    implSchema = rpc(env, 'tools/list', token=implTok).get_json() \
        ['result']['tools'][0]['inputSchema']
    verifySchema = rpc(env, 'tools/list', token=verifyTok).get_json() \
        ['result']['tools'][0]['inputSchema']
    assert implSchema['required'] == ['summary', 'commit', 'tests']
    assert verifySchema['required'] == ['verdict', 'target']
    assert 'risks' in implSchema['properties']
    assert 'mustFix' in verifySchema['properties']
    assert 'risks' not in verifySchema['properties']


def test_tools_call_validation_error_is_error(env):
    """校验失败：isError 结果带逐字段 errors（执行方可改后重交）。"""
    login(env.client)
    tid = newTask(env, 'impl')
    token = task_result_mcp.issueToken(tid, 'impl')
    out = rpc(env, 'tools/call',
              {'name': 'submit_result', 'arguments': {'commit': 'nope'}},
              token=token).get_json()
    assert out['result']['isError'] is True
    text = out['result']['content'][0]['text']
    assert 'summary' in text and 'commit' in text and 'tests' in text
    assert env.repo.get(tid).get('result') is None      # 没落
    # 改好重交 → 成功
    ok = rpc(env, 'tools/call',
             {'name': 'submit_result', 'arguments': IMPL_OK}, mid=9,
             token=token).get_json()
    assert ok['result'].get('isError') in (None, False)
    assert env.repo.get(tid)['resultCount'] == 1


def test_bad_token_rejected(env):
    """无令牌/错令牌 → 401（JSON-RPC error）。"""
    login(env.client)
    tid = newTask(env, 'impl')
    task_result_mcp.issueToken(tid, 'impl')
    resp = rpc(env, 'tools/list', token='wrong')
    assert resp.status_code == 401
    assert resp.get_json()['error']['code'] == -32001
    resp = env.client.post('/api/mcp', data=json.dumps(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}),
        content_type='application/json')
    assert resp.status_code == 401


def test_notifications_ping_unknown(env):
    """notifications/* → 202 空；ping → {}；未知方法 → -32601。"""
    login(env.client)
    tid = newTask(env, 'impl')
    token = task_result_mcp.issueToken(tid, 'impl')
    note = rpc(env, 'notifications/initialized', mid=None, token=token)
    assert note.status_code == 202
    ping = rpc(env, 'ping', token=token).get_json()
    assert ping['result'] == {}
    unknown = rpc(env, 'resources/list', token=token).get_json()
    assert unknown['error']['code'] == -32601
    badTool = rpc(env, 'tools/call', {'name': 'other_tool', 'arguments': {}},
                  token=token).get_json()
    assert badTool['result']['isError'] is True


def test_verify_flow_via_mcp(env):
    """verify 任务走 MCP：needs_judgment 也照常收（落库 + 收件箱另测）。"""
    login(env.client)
    tid = newTask(env, 'verify')
    token = task_result_mcp.issueToken(tid, 'verify')
    out = rpc(env, 'tools/call',
              {'name': 'submit_result',
               'arguments': dict(VERIFY_OK, verdict='rework',
                                 mustFix=['少一个字段'])},
              token=token).get_json()
    assert out['result'].get('isError') in (None, False)
    assert env.repo.get(tid)['result']['verdict'] == 'rework'
    assert env.repo.get(tid)['result']['mustFix'] == ['少一个字段']
