# -*- coding: utf-8 -*-
"""任务 API 校验与响应契约（外部智能体实测反馈 P1 批次 + P2-20/21/22）。

不启动真实 runner：_startTask 打桩，任务停在 queued，专测校验与字段。
"""
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'c' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def test_remote_missing_node_is_400_with_nodes(env, monkeypatch):
    """反馈 §3.5：缺 node 400 + nodes 提示，不再 500 HTML。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: None)
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    monkeypatch.setattr(nodesMod, 'listNodeNames', lambda: ['local', 'ai-xmyz'])
    resp = create(env, {'prompt': 'x', 'engine': 'remote'})
    body = resp.get_json()
    assert resp.status_code == 400
    assert body['nodes'] == ['local', 'ai-xmyz']
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'ghost',
                        'cwd': '/tmp'})
    assert resp.status_code == 400
    assert 'ghost' in json.dumps(resp.get_json()['nodes']) or \
        resp.get_json()['nodes'] == ['local', 'ai-xmyz']


def test_remote_missing_cwd_is_400_not_accepted(env, monkeypatch):
    """反馈 §3.5：缺 cwd 直接 400，不再受理后异步失败。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name} if name == 'local' else None)
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'local'})
    assert resp.status_code == 400
    assert 'cwd' in resp.get_json()['error']


def test_acp_rejects_cwd(env):
    """反馈 §3.7：acp 不再静默忽略 cwd，直接说明拒绝原因。"""
    resp = create(env, {'prompt': 'x', 'engine': 'acp', 'cwd': r'E:\事项'})
    assert resp.status_code == 400
    assert 'acp' in resp.get_json()['error']


def test_default_mode_is_default_not_auto(env):
    """反馈 §3.6：不传 mode = 逐项审批（安全默认），auto 必须显式。"""
    resp = create(env, {'prompt': 'x'})
    assert resp.status_code == 202
    assert resp.get_json()['task']['mode'] == 'default'
    resp = create(env, {'prompt': 'x', 'mode': 'auto'})
    assert resp.get_json()['task']['mode'] == 'auto'


def test_prompt_limit_in_error_message(env):
    resp = create(env, {'prompt': 'x' * 4001})
    assert resp.status_code == 400
    assert '4000' in resp.get_json()['error']


def test_engine_p_deprecated_alias(env):
    resp = create(env, {'prompt': 'x', 'engine': 'p'})
    assert resp.status_code == 202
    assert resp.get_json()['task']['engine'] == 'acp'
    resp = create(env, {'prompt': 'x', 'engine': 'zzz'})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'engine 只能是 acp / remote'


def test_list_counts_running_and_queued(env):
    for i in range(3):
        create(env, {'prompt': 'task-%d' % i})
    with env.repo.lock:
        first = env.repo.get(None) if False else None
    resp = env.client.get('/api/tasks', headers=H).get_json()
    assert resp['queued'] == 3
    assert resp['running'] == 0
    assert 'countScope' in resp   # 统计口径明示


def test_events_since_alias_accepted(env):
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    full = env.client.get('/api/tasks/%s/events' % tid, headers=H).get_json()
    seqs = [e['seq'] for e in full['events']]
    assert seqs
    since = seqs[0]
    r1 = env.client.get('/api/tasks/%s/events?after=%d' % (tid, since),
                        headers=H).get_json()
    r2 = env.client.get('/api/tasks/%s/events?since=%d' % (tid, since),
                        headers=H).get_json()
    assert [e['seq'] for e in r1['events']] == [e['seq'] for e in r2['events']]
    assert since not in [e['seq'] for e in r2['events']]


def test_close_returns_final_status(env):
    """反馈 §4：close 返回的就是终态。"""
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    body = env.client.post('/api/tasks/%s/close' % tid, headers=H).get_json()
    assert body['status'] == 'closed'


def test_summary_workspace_shows_remote_cwd(env, monkeypatch):
    """反馈 §4：remote 任务的 workspace 字段显示远端 cwd。"""
    task = {'id': 'r1', 'status': 'waiting', 'createdAt': 0, 'events': [],
            'engine': 'remote', 'node': 'ai-xmyz', 'remoteCwd': '/mnt/sda1',
            'workspace': r'E:\x\_agent_tasks\local-dir'}
    out = store.repo.summary(task)
    assert out['workspace'] == '/mnt/sda1'


def test_engine_not_overwritten_on_session_start(env):
    """反馈 §4：remote 任务建卡后 engine 保持 remote（曾被强制改成 acp）。"""
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'local',
                        'cwd': '/tmp'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        assert task['engine'] == 'remote'


def _approveSoon(agent, optionId, delay=0.0):
    """模拟审批方：等处理器挂起 perm 后写入决策并唤醒。"""
    import threading
    import time as _time

    def run():
        deadline = _time.time() + 3
        _time.sleep(delay)
        while _time.time() < deadline:
            if agent.get('perm'):
                agent['permDecision']['value'] = optionId
                agent['permEvent'].set()
                return
            _time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


def test_permission_request_carries_call_and_args(env):
    """反馈 §3.4：审批事件必须能对回具体工具调用。"""
    from modules.tasks_runner import _handleAcpPermission
    with store.repo.lock:
        task = {'id': 'p1', 'seq': 0, 'events': [], 'status': 'running',
                'createdAt': 0}
        store.repo.put(task)
    agent = {'perm': None, 'permDecision': None,
             'permEvent': __import__('threading').Event()}
    _approveSoon(agent, 'opt-allow')
    params = {'toolCall': {'toolCallId': 'call-42', 'title': 'Bash',
                           'rawInput': {'command': 'pwd'}},
              'options': [{'optionId': 'opt-allow', 'name': '允许',
                           'kind': 'allow_once'}]}
    value = _handleAcpPermission(task, agent, params, 'req-1')
    assert value == 'opt-allow'
    ev = [e for e in task['events'] if e['type'] == 'permission_request'][0]
    assert ev['callId'] == 'call-42'
    assert 'pwd' in ev['args']


def test_permission_falls_back_to_last_tool_call(env):
    """ACP permission 不带 toolCallId 时，退回最近一次 tool_call。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    with store.repo.lock:
        task = {'id': 'p2', 'seq': 0, 'events': [], 'status': 'running',
                'createdAt': 0}
        store.repo.put(task)
    _handleAcpUpdate(task, {'sessionUpdate': 'tool_call',
                            'toolCallId': 'call-7', 'title': 'Bash',
                            'rawInput': 'ls'})
    agent = {'perm': None, 'permDecision': None,
             'permEvent': __import__('threading').Event()}
    _approveSoon(agent, 'opt-a')
    params = {'toolCall': {'title': 'Bash'},
              'options': [{'optionId': 'opt-a', 'name': '允许',
                           'kind': 'allow_once'}]}
    value = _handleAcpPermission(task, agent, params, 'req-2')
    assert value == 'opt-a'
    ev = [e for e in task['events'] if e['type'] == 'permission_request'][0]
    assert ev['callId'] == 'call-7'
    assert ev['args'] == 'ls'


def test_api_404_is_json(env):
    """反馈 §3.8：/api/* 的 404/401 都是 JSON 而非 Flask HTML。

    未知 /api 路径由认证守卫 401 JSON（不暴露端点存在性）；
    已知路由下的资源缺失才是 404 JSON。
    """
    resp = env.client.get('/api/no-such-endpoint', headers=H)
    assert resp.status_code == 401
    assert resp.is_json and 'error' in resp.get_json()
    resp = env.client.get('/api/tasks/ghost-id-xyz', headers=H)
    assert resp.status_code == 404
    assert resp.is_json
    assert 'error' in resp.get_json()


def test_api_405_is_json(env):
    resp = env.client.get('/api/tasks', headers=H)   # GET 支持的列表；用 DELETE 打 405
    assert resp.status_code == 200
    resp = env.client.delete('/api/tasks', headers=H)
    assert resp.status_code == 405
    assert resp.is_json
    assert resp.get_json()['code'] == 405
