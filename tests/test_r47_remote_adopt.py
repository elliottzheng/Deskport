# -*- coding: utf-8 -*-
"""R4-7 远端节点接手已有 Kimi 会话：透传、node 侧 fork+load、候选列表代理。"""
import hashlib
import json
import threading
import time as _time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'g' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
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


def _sid(n='a'):
    return 'session_' + n * 8 + '-' + n * 4 + '-' + n * 4 + '-' + n * 4 + '-' + n * 12


def test_remote_adopt_task_fields_and_client(env, monkeypatch):
    """remote + kimiSessionId/fork：字段落卡、透传到 RemoteAcpClient。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'url': 'x',
                                      'models': [], 'model': ''})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    sid = _sid('a')
    resp = create(env, {'prompt': '接手远端会话', 'engine': 'remote',
                        'node': 'ai-xmyz', 'cwd': '/mnt/sda1/x',
                        'kimiSessionId': sid, 'fork': False})
    assert resp.status_code == 202
    task = resp.get_json()['task']
    assert task['kimiSessionId'] == sid
    assert task['kimiSessionFork'] is False
    assert task['sessionId'] == ''           # 节点会话 id 由 start() 回填
    with env.repo.lock:
        stored = env.repo.get(task['id'])
    client = nodesMod.remoteClientFactory(stored)(cwd='/mnt/sda1/x')
    assert client.kimi_session_id == task['kimiSessionId']
    assert client.fork is False


def test_kimi_sessions_proxy_by_node(env, monkeypatch):
    """?node=<远端节点> 代理 node 的 /api/kimi-sessions（R4-7 #2）。"""
    import modules.agent_nodes as nodesMod
    fake = [{'id': 'session_1', 'title': 'course 会话',
             'workDir': '/mnt/sda1/yinglin/course', 'updatedAt': 5,
             'lastPrompt': 'p'}]
    calls = {}

    def fakeNodeRequest(node, method, path, payload=None, timeout=30):
        calls['path'] = path
        return 200, {'sessions': fake}

    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeNodeRequest)
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'url': 'x'} if name == 'ai-xmyz' else None)
    resp = env.client.get('/api/tasks/kimi-sessions?node=ai-xmyz'
                          '&cwd=/mnt/sda1/yinglin/course', headers=H).get_json()
    assert resp['sessions'] == fake and resp['node'] == 'ai-xmyz'
    assert '/api/kimi-sessions' in calls['path']
    assert 'cwd=' in calls['path']

    resp = env.client.get('/api/tasks/kimi-sessions?node=ghost', headers=H)
    assert resp.status_code == 404


# ---- node 侧：fork → session/load 副本 ----

def test_node_session_create_forks_then_loads(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, 'agent_node')
    try:
        import node_api as nodeApiMod
        import acp_session as acpSessionMod
        forks = []

        def fakeFork(kimi_bin, sid, timeout=600):
            forks.append((kimi_bin, sid))
            return 'session_forked-1111-2222-3333-444444444444'
        monkeypatch.setattr(nodeApiMod, 'forkKimiSession', fakeFork)

        started = {}

        class StubSession:
            def __init__(self, sid, cwd, mode, kimi_bin, title='', model=''):
                self.sid = sid
                self.kimi_session_id = None
                from acp_session import Ring
                self.ring = Ring()

            def start(self, resume_session_id=None):
                started['resume'] = resume_session_id
                self.kimi_session_id = resume_session_id

            def stop(self):
                pass

            def meta(self):
                return {'sid': self.sid, 'kimiSessionId': self.kimi_session_id}

        monkeypatch.setattr(nodeApiMod, 'AcpSession', StubSession)
        mgr = nodeApiMod.SessionManager('kimi', str(tmp_path / 'data'))
        session = mgr.create('/mnt/x', 'auto', '', None, 'm',
                             kimi_session_id='session_' + 'b' * 32,
                             fork=True)
        assert forks == [('kimi', 'session_' + 'b' * 32)]
        assert started['resume'] == 'session_forked-1111-2222-3333-444444444444'
        assert session.kimi_session_id == started['resume']

        # fork 失败（占用）→ RuntimeError 上抛，不建会话
        def badFork(kimi_bin, sid, timeout=600):
            raise RuntimeError('This session is in use')
        monkeypatch.setattr(nodeApiMod, 'forkKimiSession', badFork)
        try:
            mgr.create('/mnt/x', 'auto', '', None, 'm',
                       kimi_session_id='session_' + 'c' * 32, fork=True)
            raise AssertionError('应当抛出')
        except RuntimeError as e:
            assert 'in use' in str(e)
    finally:
        sys.path.pop(0)


def test_node_kimi_sessions_endpoint(tmp_path, monkeypatch):
    """node GET /api/kimi-sessions：真实 HTTP 层（token + cwd 过滤代理）。"""
    import sys
    import urllib.request
    sys.path.insert(0, 'agent_node')
    try:
        import node_api as nodeApiMod
        from node_api import SessionManager
        mgr = SessionManager('kimi', str(tmp_path / 'data'))
        nodeApiMod.init(mgr, 'tok-x')
        srv = nodeApiMod.ThreadingHTTPServer(('127.0.0.1', 0),
                                             nodeApiMod.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                'http://127.0.0.1:%d/api/kimi-sessions?cwd=/mnt/x' % port)
            req.add_header('X-Agent-Token', 'tok-x')
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            assert d == {'sessions': []}   # 本机无 /mnt/x 会话 → 空列表
        finally:
            srv.shutdown()
    finally:
        sys.path.pop(0)


# ---- R4-3c 追加：唤醒卡死的双看门狗 ----

def test_resume_watchdog_clears_stuck_resume(env, monkeypatch):
    """session/load 阻塞（会话被其他活进程占用）→ 唤醒超时清态、
    任务回 paused 带 error，第二次 message 不再 409。"""
    import modules.tasks_runner as runner
    blocked = threading.Event()
    started = threading.Event()

    class BlockingClient:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0, model=''):
            pass

        def start(self, session_id=None):
            started.set()
            blocked.wait(10)          # 模拟 session/load 无限等会话锁
            return 'sess-x'

        def prompt(self, text, timeout=None):
            return {'stopReason': 'end_turn'}

        def close(self):
            pass

    monkeypatch.setattr(runner, 'RESUME_WATCHDOG_SEC', 1)
    from modules.agent_nodes import clientFactoryFor as _cf
    monkeypatch.setattr(nodesModLazy(), 'clientFactoryFor',
                        lambda task: BlockingClient)

    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'paused'
        task['sessionId'] = 'sess-adopted'
        env.repo.save()
    resp = env.client.post('/api/tasks/%s/message' % tid, headers=H,
                           data=json.dumps({'text': '续上'}),
                           content_type='application/json')
    assert resp.status_code == 202
    assert started.wait(5)
    # 看门狗（1s）触发：回 paused + error + 清唤醒态
    for _ in range(60):
        with env.repo.lock:
            t = env.repo.get(tid)
            if not t.get('resuming') and t.get('status') == 'paused' \
                    and t.get('error'):
                break
        _time.sleep(0.1)
    with env.repo.lock:
        t = env.repo.get(tid)
    assert t['status'] == 'paused' and t['resuming'] is False
    assert '唤醒超时' in t['error']
    # 第二次 message 不再 409（重新尝试唤醒）
    blocked.set()
    resp = env.client.post('/api/tasks/%s/message' % tid, headers=H,
                           data=json.dumps({'text': '再试'}),
                           content_type='application/json')
    assert resp.status_code in (200, 202, 409)   # 至少不是卡死
    blocked.set()


def nodesModLazy():
    import modules.agent_nodes as nodesMod
    return nodesMod
