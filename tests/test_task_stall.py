# -*- coding: utf-8 -*-
"""批次 L L4：卡住的工具调用报警 + 重启后 cancel 达节点并如实回报。

两半：task_health._toolStall 巡检（status 事件 + 主管 failure / 无主管进
指挥台待办，每次调用只报一次，工具收尾标记即清）；RemoteAcpClient.cancel
在重启后重新接上的会话里真的发出 session/cancel，且节点回合还在跑时
/api/tasks/<id>/cancel 如实返回 nodeStillRunning（用假节点桩，不起真 kimi）。
"""
import json
import queue
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import command_view
from modules import events as eventsMod
from modules import leads_inbox, task_health
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
TOKEN = 'tok-' + 's' * 24
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    import hashlib
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(
                                      TOKEN.encode()).hexdigest()}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    # 巡检线程本体由 conftest 空转；这里直接调 _toolStall（时钟自造）
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302
    return SimpleNamespace(client=client, fresh=app.test_client(), repo=repo,
                           tmp=tmp_path, monkeypatch=monkeypatch)


def _create(env):
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 202
    return resp.get_json()['task']['id']


def _stalledTask(env, lead=''):
    """running + 当前工具调用 1000s 无事件（默认阈值 900s）。"""
    tid = _create(env)
    now = time.time()
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'running'
        task['lead'] = lead
        task['_currentTool'] = {'name': 'Bash', 'since': now - 1000}
        task['lastActiveAt'] = now - 1000
        env.repo.save()
    return tid


def test_tool_stall_writes_status_and_lead_failure(env):
    """有主管：status 事件 + 收件箱 failure（任务 <id8> 的 <工具> 已 15 分钟无输出）。"""
    resp = env.client.post('/api/settings/leads',
                           data=json.dumps({'name': '甲'}),
                           content_type='application/json')
    leadId = resp.get_json()['lead']['id']
    tid = _stalledTask(env, lead=leadId)
    now = time.time()

    task_health._toolStall(now)
    events = env.repo.get(tid)['events']
    stall = [e for e in events if e.get('type') == 'status' and '无输出' in (e.get('text') or '')]
    assert len(stall) == 1
    assert 'Bash' in stall[0]['text'] and '15 分钟' in stall[0]['text']
    entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
    assert len(entries) == 1 and entries[0]['kind'] == 'failure'
    assert tid[:8] in entries[0]['text'] and 'Bash' in entries[0]['text']
    assert '15 分钟' in entries[0]['text']
    # 有主管的不留指挥台标记（两路分流）
    assert env.repo.get(tid).get('_toolStall') is None

    task_health._toolStall(now + 60)                   # 同一次调用只报一次
    stall = [e for e in env.repo.get(tid)['events']
             if e.get('type') == 'status' and '无输出' in (e.get('text') or '')]
    assert len(stall) == 1
    assert len(leads_inbox.entriesAfter(leadId, 0, 'up')[0]) == 1


def test_tool_stall_without_lead_enters_command_view(env):
    """无主管：留 _toolStall 标记，指挥台「等你处理」出现卡住行。"""
    tid = _stalledTask(env)
    task_health._toolStall(time.time())
    assert env.repo.get(tid)['_toolStall']['name'] == 'Bash'
    rows = [p for p in command_view.overview()['pending'] if p['kind'] == 'stall']
    assert len(rows) == 1
    assert rows[0]['taskId'] == tid and 'Bash' in rows[0]['detail']
    assert '15 分钟' in rows[0]['detail']

    # 工具收尾：标记清除，指挥台不再待办
    task = env.repo.get(tid)
    runner._handleAcpUpdate(task, {'sessionUpdate': 'tool_call_update',
                                   'toolCallId': 'c1', 'status': 'completed',
                                   'title': 'Bash'})
    assert task.get('_toolStall') is None and task.get('_currentTool') is None
    rows = [p for p in command_view.overview()['pending'] if p['kind'] == 'stall']
    assert rows == []


def test_tool_stall_silent_when_events_keep_coming(env):
    """阈值内有新事件（lastActiveAt 刷新）→ 不报。"""
    tid = _stalledTask(env)
    now = time.time()
    with env.repo.lock:
        env.repo.get(tid)['lastActiveAt'] = now - 60    # 1 分钟前还有事件
        env.repo.save()
    task_health._toolStall(now)
    assert not any('无输出' in (e.get('text') or '')
                   for e in env.repo.get(tid)['events'])


def test_tool_stall_under_one_minute_shows_seconds(env):
    """L 返工：toolStallSec < 60（如 30s）——文案与指挥台行显示秒数，
    不再是「0 分钟」/「? 分钟」。"""
    env.monkeypatch.setattr(configMod, 'getToolStallSec', lambda: 30)
    tid = _create(env)
    now = time.time()
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'running'
        task['_currentTool'] = {'name': 'Bash', 'since': now - 40}
        task['lastActiveAt'] = now - 40
        env.repo.save()
    task_health._toolStall(now)
    events = env.repo.get(tid)['events']
    stall = [e for e in events if e.get('type') == 'status' and '无输出' in (e.get('text') or '')]
    assert len(stall) == 1 and '30 秒' in stall[0]['text']
    rows = [p for p in command_view.overview()['pending'] if p['kind'] == 'stall']
    assert len(rows) == 1 and '30 秒' in rows[0]['detail']


def test_tool_stall_disabled_by_config(env):
    """toolStallSec=0：关闭。"""
    tid = _stalledTask(env)
    env.monkeypatch.setattr(configMod, 'getToolStallSec', lambda: 0)
    task_health._toolStall(time.time())
    assert not any('无输出' in (e.get('text') or '')
                   for e in env.repo.get(tid)['events'])


# ---------------------------------------------------------------- cancel 达节点

def _fakeNode(running=True):
    """假节点：记录调用；/api/sessions 复用同一 sid（重启后接上的形状），
    cancel 之后 GET 实况按 running 返回。"""
    calls = []

    def fakeRequest(node, method, path, payload=None, timeout=30):
        calls.append((method, path))
        if method == 'POST' and path == '/api/sessions':
            return 200, {'sid': 's1', 'kimiSessionId': 'k1',
                         'attachSeq': 3, 'replayCount': 0}
        if method == 'POST' and path.endswith('/cancel'):
            return 200, {'ok': True}
        if method == 'GET' and path == '/api/sessions/s1':
            return 200, {'session': {'sid': 's1', 'running': running,
                                     'dead': False}}
        return 200, {}
    return calls, fakeRequest


def _reattachedClient(env, running=True):
    """重启后重新接上的客户端：带 sessionId 建会话（节点复用活会话）。"""
    calls, fakeRequest = _fakeNode(running)
    env.monkeypatch.setattr(nodesMod, 'nodeRequest', fakeRequest)
    env.monkeypatch.setattr(nodesMod, 'findNode',
                            lambda name: {'name': name, 'url': 'x'})
    client = nodesMod.RemoteAcpClient(cwd='/tmp', node='ai-xmyz')
    client._closed = True                 # 不起事件流线程
    client.start(session_id='k1')
    return client, calls


def test_cancel_after_reattach_reaches_node_turn(env):
    """重启后重新接上的会话里，cancel 真的对同一 node sid 发出
    session/cancel；节点回合还在跑 → cancel 如实回报、端点带 nodeStillRunning。"""
    client, calls = _reattachedClient(env, running=True)
    assert client.node_sid == 's1'        # 接上的是节点上那个活会话

    still = client.cancel()
    assert still is True
    assert ('POST', '/api/sessions/s1/cancel') in calls
    assert ('GET', '/api/sessions/s1') in calls

    tid = _create(env)
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'running'
        env.repo.save()
    env.repo.setRunning(tid, client)
    env.repo.setAgent(tid, {'queue': queue.Queue(), 'client': client,
                            'perm': None})
    resp = env.client.post('/api/tasks/%s/cancel' % tid, headers=H)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['nodeStillRunning'] is True     # 不假装停了


def test_cancel_node_idle_reports_stopped(env):
    """节点回合已结束（running=False）→ 不带 nodeStillRunning。"""
    client, calls = _reattachedClient(env, running=False)
    assert client.cancel() is False
    tid = _create(env)
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'running'
        env.repo.save()
    env.repo.setRunning(tid, client)
    env.repo.setAgent(tid, {'queue': queue.Queue(), 'client': client,
                            'perm': None})
    resp = env.client.post('/api/tasks/%s/cancel' % tid, headers=H)
    assert resp.status_code == 200
    assert 'nodeStillRunning' not in resp.get_json()
