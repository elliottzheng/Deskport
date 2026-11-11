# -*- coding: utf-8 -*-
"""R4-5a 回放污染：session/load 回放的 update/旧审批不得进入事件流或
成为 live 挂起；接手存活会话（进程复用）时事件流从 attachSeq 水位开始。"""
import json
import queue
import threading
import time as _time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events, tasks_api
from modules import tasks_store as store
from modules import tasks_runner as runner
from modules import agent_nodes as nodesMod
from modules import kimi_acp as kacp
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'f' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}

REAL_EDIT_ARGS = {'new_string': 'x', 'old_string': 'y',
                  'path': 'E:/事项/远程/web-platform-dev/m.py'}


# ---- stdio KimiAcp：回放窗口门禁 ----

def test_kimiACP_load_window_blocks_updates_and_cancels_perms(tmp_path):
    k = kacp.KimiAcp(cwd=str(tmp_path))
    seen = []
    k.on_update = lambda u: seen.append(u)

    k._loading = True
    for _ in range(3):
        k._handleMessage({'jsonrpc': '2.0', 'method': 'session/update',
                          'params': {'update': {'sessionUpdate':
                                                'agent_message_chunk'}}})
    sent = []
    k._send = lambda obj: sent.append(obj)
    k._handleMessage({'jsonrpc': '2.0', 'id': 9,
                      'method': 'session/request_permission',
                      'params': {'options': [{'optionId': 'a',
                                              'kind': 'allow_once'}]}})
    assert seen == []                    # 回放 update 不外发
    assert k.replayCount == 3
    assert k.replayPerms == 1
    assert sent[0]['result']['outcome']['outcome'] == 'cancelled'

    k._loading = False                   # load 返回后恢复外发
    k._handleMessage({'jsonrpc': '2.0', 'method': 'session/update',
                      'params': {'update': {'sessionUpdate':
                                            'agent_message_chunk'}}})
    assert len(seen) == 1 and k.replayCount == 3


# ---- node AcpSession：回放中的审批直接取消、不落 live ----

def test_node_session_permission_during_load_cancelled():
    import sys
    sys.path.insert(0, 'agent_node')
    try:
        from acp_session import AcpSession
        s = AcpSession('s1', '/tmp', 'auto', 'kimi')
        s._send = lambda obj: None
        s._loading = True
        s._handlePermission({'options': [{'optionId': 'a', 'kind': 'allow_once'}]},
                            7)
        assert s.replayPerms == 1
        ringTypes = [e['type'] for e in s.ring.since(0)]
        assert 'permission_request' not in ringTypes   # 不成为 live 挂起
    finally:
        sys.path.pop(0)


# ---- RemoteAcpClient：attachSeq 水位 + 回放 permission 跳过 ----

def test_remote_client_start_reads_attach_seq(monkeypatch):
    client = nodesMod.RemoteAcpClient(cwd='/tmp', node='ai-xmyz')
    sent = {}

    def fakeRequest(node, method, path, payload=None, timeout=30):
        sent['payload'] = payload
        return 200, {'sid': 's1', 'kimiSessionId': 'k1',
                     'attachSeq': 42, 'replayCount': 7}

    class _Resp:
        def __iter__(self):
            return iter([])

        def close(self):
            pass

    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeRequest)
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'url': 'x'})
    monkeypatch.setattr(nodesMod, 'tunnelPort', lambda node: 0, raising=False)
    monkeypatch.setattr(client, '_openStream',
                        lambda node, since, timeout=45: (_Resp(),
                                                         since))
    opened = {}

    def openStream(node, since, timeout=45):
        opened['since'] = since
        return _Resp()
    monkeypatch.setattr(client, '_openStream', openStream)
    client.start()
    assert client.attachSeq == 42 and client.replayCount == 7
    assert opened['since'] == 42        # 接手存活会话：从水位之后取，旧事件不涌入


def test_remote_client_skips_replayed_permission():
    client = nodesMod.RemoteAcpClient(cwd='/tmp', node='ai-xmyz')
    calls = []

    def onUpdate(u):
        calls.append(('update', u))
    client.on_update = onUpdate
    client.on_permission = lambda params, rid: calls.append(('perm', rid)) or 'a'

    client._closed = True   # 防 _streamLoop 干扰
    client._handleEvent({'type': 'update', 'replay': True, 'seq': 1,
                         'update': {'sessionUpdate': 'tool_call'}})
    client._handleEvent({'type': 'permission_request', 'replay': True,
                         'seq': 2, 'requestId': '99',
                         'title': '旧部署 prod', 'options': []})
    client._handleEvent({'type': 'update', 'seq': 3,
                         'update': {'sessionUpdate': 'agent_message_chunk',
                                    'content': {'type': 'text', 'text': 'live'}}})
    assert calls == [('update', {'sessionUpdate': 'agent_message_chunk',
                                 'content': {'type': 'text', 'text': 'live'}})]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
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


def _sid(n='a'):
    return 'session_' + n * 8 + '-' + n * 4 + '-' + n * 4 + '-' + n * 4 + '-' + n * 12


# ---- runner：接手后写「已加载历史 N 条」+ liveFrom 水位 ----

def test_resume_records_replay_summary_and_live_from(env, monkeypatch):
    import threading
    import time as _time
    monkeypatch.setattr(tasks_api.kimi_acp, 'forkSession',
                        lambda sid, timeout=90: 'session_' + '9' * 32)

    class FakeClient:
        replayCount = 7

        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0, model=''):
            pass

        def start(self, session_id=None):
            return 'sess-r1'

        def prompt(self, text, timeout=None):
            return {'stopReason': 'end_turn'}

        def set_mode(self, mode):
            pass

        def cancel(self):
            pass

        def close(self):
            pass

        def shutdown(self):
            pass

    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x',
                                            'kimiSessionId': _sid('a')}),
                           content_type='application/json')
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
    threading.Thread(target=runner._sessionThread, args=(task, []),
                     kwargs={'clientFactory': FakeClient},
                     daemon=True).start()
    for _ in range(80):
        with env.repo.lock:
            if task.get('status') == 'waiting':
                break
        _time.sleep(0.1)
    texts = [e.get('text') or '' for e in task['events']
             if e.get('type') == 'status']
    assert any('已加载历史 7 条' in t for t in texts)
    assert task.get('liveFrom', 0) > 0
    env.client.post('/api/tasks/%s/close' % tid, headers=H)


# ---- R4-3b 重建的 liveFrom 门（R4-5a #3）----

def test_wait_rebuild_ignores_pre_live_permission(env):
    import threading
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        task['seq'] = 3
        task['events'] = [
            {'seq': 2, 'at': store._now(), 'type': 'permission_request',
             'requestId': '99', 'name': '旧部署 prod', 'callId': 'old',
             'args': '旧动作', 'options': [
                 {'optionId': 'approve_once', 'name': 'Approve once',
                  'kind': 'allow_once'}]},
        ]
        task['pendingPermission'] = '99'   # 回放污染的标记
        task['liveFrom'] = 3               # 水位：99 是回放期的，不是 live
        env.repo.setAgent(tid, {'perm': None, 'permDecision': None,
                                'permEvent': threading.Event()})
        env.repo.save()
    body = env.client.get('/api/tasks/%s/wait?after=0&timeout=1' % tid,
                          headers=H).get_json()
    assert body['status'] != 'permission'       # 回放旧审批不得返回
    assert body['pending'] is None
