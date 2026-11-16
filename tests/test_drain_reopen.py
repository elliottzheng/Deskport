# -*- coding: utf-8 -*-
"""批次 DR：部署排空（不打断正在进行的一轮）+ 关闭的会话能接回。

排空侧跑真实会话线程（桩客户端）并经 HTTP 端点调用 drain；接回侧打桩节点
（nodeRequest）与 shutdownNodeSession。"""
import hashlib
import json
import threading
import time as _time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import session_adopt, task_drain
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'd' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}


def _sid(n='a'):
    return 'session_' + n * 8 + '-' + n * 4 + '-' + n * 4 + '-' + n * 4 + '-' + n * 12


@pytest.fixture(autouse=True)
def cleanDrain():
    assert not task_drain.draining()
    yield
    assert not task_drain.draining()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    import modules.agent_nodes as nodesMod
    import time as _t
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _t.monotonic(), 'data': {}})
    from modules import tasks_api
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path,
                           nodes=nodesMod)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def drainPost(env, body, **kw):
    return env.client.post('/api/system/drain', data=json.dumps(body),
                           content_type='application/json', **kw)


def _taskId(resp):
    return resp.get_json()['task']['id']


def _waitFor(pred, timeout=10):
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if pred():
            return True
        _time.sleep(0.05)
    return False


def _statusIs(repo, tid, wanted):
    with repo.lock:
        return (repo.get(tid).get('status') or '') == wanted


def _runThread(task, factory):
    from modules import tasks_runner as runner
    threading.Thread(target=runner._sessionThread, args=(task, []),
                     kwargs={'clientFactory': factory},
                     daemon=True).start()


class RoundClient:
    """桩客户端：一轮 = 一次 prompt；release 控制本轮何时结束。"""

    def __init__(self, cwd, on_update=None, on_permission=None, mode='auto',
                 prompt_timeout=0, model='', release=None, prompts=None,
                 fail=False, delay=0):
        self.release = release if release is not None else threading.Event()
        self.prompts = prompts if prompts is not None else []
        self.fail = fail
        self.delay = delay
        self.stderr_lines = []

    def start(self, session_id=None):
        return 'sess-dr1'

    def prompt(self, text, timeout=None):
        self.prompts.append(text)
        if self.delay:
            _time.sleep(self.delay)
        if self.fail:
            raise RuntimeError('boom')
        self.release.wait(10)
        return {'stopReason': 'end_turn'}

    def cancel(self):
        self.release.set()

    def close(self):
        pass

    def shutdown(self):
        pass


def _factory(release=None, prompts=None, fail=False, delay=0):
    def make(cwd, on_update=None, on_permission=None, mode='auto',
             prompt_timeout=0):
        return RoundClient(cwd, release=release, prompts=prompts, fail=fail,
                           delay=delay)
    return make


def _stuckTask(env):
    """建一个卡在一轮中间的任务（线程真跑，prompt 永不返回）。"""
    stuck = threading.Event()
    tid = _taskId(create(env, {'prompt': '卡住的活'}))
    with env.repo.lock:
        task = env.repo.get(tid)
    _runThread(task, _factory(release=stuck))
    assert _waitFor(lambda: _statusIs(env.repo, tid, 'running'))
    return tid


# ---- 1. 排空：等在飞回合自然结束（经端点调用） ----

def test_drain_waits_for_round_then_returns(env):
    """桩任务在一轮中间：drain 阻塞到本轮自然结束才返回，不打断干活。"""
    release = threading.Event()
    tid = _taskId(create(env, {'prompt': '排空验收'}))
    with env.repo.lock:
        task = env.repo.get(tid)
    _runThread(task, _factory(release=release))
    assert _waitFor(lambda: _statusIs(env.repo, tid, 'running'))

    box = {}

    def doDrain():
        box['resp'] = drainPost(env, {'timeoutSec': 5})
    th = threading.Thread(target=doDrain, daemon=True)
    th.start()
    assert _waitFor(task_drain.draining)
    _time.sleep(0.4)
    assert th.is_alive()                     # 一轮没结束，drain 就在等
    release.set()                            # 本轮自然结束
    th.join(10)
    report = box['resp'].get_json()
    assert box['resp'].status_code == 200
    assert report['drained'] is True and report['timedOut'] is False
    assert report['remaining'] == []
    assert _waitFor(lambda: _statusIs(env.repo, tid, 'waiting'))


def test_drain_timeout_marks_paused_by_restart_and_conflicts(env):
    """超时路径：仍在跑的任务标记 pausedBy='restart'，放行部署不断错；
    排空进行中再来一次：端点 409，不动当前排空。"""
    tid = _stuckTask(env)
    box = {}

    def doDrain():
        box['resp'] = drainPost(env, {'timeoutSec': 2})
    th = threading.Thread(target=doDrain, daemon=True)
    th.start()
    assert _waitFor(task_drain.draining)
    conflict = drainPost(env, {'timeoutSec': 1})
    assert conflict.status_code == 409
    assert [t['id'] for t in conflict.get_json()['running']] == [tid]
    th.join(10)
    report = box['resp'].get_json()
    assert report['drained'] is False and report['timedOut'] is True
    assert [t['id'] for t in report['remaining']] == [tid]
    with env.repo.lock:
        assert env.repo.get(tid)['pausedBy'] == 'restart'
    events = env.client.get('/api/tasks/%s/events' % tid,
                            headers=H).get_json()['events']
    assert any('pausedBy=restart' in (e.get('text') or '') for e in events)


def test_drain_endpoint_probe_and_caller_gate(env):
    """端点：回环可调（探针不进排空）；非回环无会话 403；网页会话放行。"""
    r = drainPost(env, {'timeoutSec': 0})
    assert r.status_code == 200
    body = r.get_json()
    assert body['probe'] is True and body['count'] == 0
    assert not task_drain.draining()
    far = drainPost(env, {'timeoutSec': 0},
                    environ_base={'REMOTE_ADDR': '10.9.9.9'})
    assert far.status_code == 403
    with env.client.session_transaction() as sess:
        sess['authenticated'] = True
    ok = drainPost(env, {'timeoutSec': 0},
                   environ_base={'REMOTE_ADDR': '10.9.9.9'})
    assert ok.status_code == 200


# ---- 2. 排空期间新消息/新任务照常排队 ----

def test_message_queues_during_drain_then_delivers(env):
    """排空期间给等待中的任务发消息：只排队不开轮；排空结束后自动投递。"""
    prompts = []
    release = threading.Event()
    aTid = _taskId(create(env, {'prompt': '先跑一轮'}))
    with env.repo.lock:
        aTask = env.repo.get(aTid)
    _runThread(aTask, _factory(release=release, prompts=prompts))
    assert _waitFor(lambda: _statusIs(env.repo, aTid, 'running'))
    release.set()
    assert _waitFor(lambda: _statusIs(env.repo, aTid, 'waiting'))

    _stuckTask(env)                  # 让 drain 有等待对象
    box = {}

    def doDrain():
        box['resp'] = drainPost(env, {'timeoutSec': 2})
    th = threading.Thread(target=doDrain, daemon=True)
    th.start()
    assert _waitFor(task_drain.draining)

    resp = env.client.post('/api/tasks/%s/message' % aTid, headers=H,
                           data=json.dumps({'text': '排空中发的消息'}),
                           content_type='application/json')
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['queued'] is True and body['draining'] is True
    assert _statusIs(env.repo, aTid, 'waiting')     # 没有开轮
    assert len(prompts) == 1                        # 仍只有第一轮
    assert '先跑一轮' in prompts[0]                  # 首轮提示词（_promptFor 包装）

    th.join(10)                                      # 排空超时结束 → 唤醒续投
    assert _waitFor(lambda: '排空中发的消息' in prompts)
    assert _waitFor(lambda: _statusIs(env.repo, aTid, 'waiting'))


def test_new_task_parks_at_gate_during_drain(env):
    """排空期间新建任务照常排队：线程停在闸门前不开轮，排空结束后才跑。"""
    _stuckTask(env)
    box = {}

    def doDrain():
        box['resp'] = drainPost(env, {'timeoutSec': 2})
    th = threading.Thread(target=doDrain, daemon=True)
    th.start()
    assert _waitFor(task_drain.draining)

    prompts = []
    release = threading.Event()
    release.set()
    nTid = _taskId(create(env, {'prompt': '排空中新建'}))
    with env.repo.lock:
        nTask = env.repo.get(nTid)
    _runThread(nTask, _factory(release=release, prompts=prompts))
    _time.sleep(0.4)
    assert _statusIs(env.repo, nTid, 'queued')       # 排队，没有开轮
    events = env.client.get('/api/tasks/%s/events' % nTid,
                            headers=H).get_json()['events']
    assert not any(e.get('text') == '开始执行' for e in events)

    th.join(10)                                      # 排空结束 → 闸门放行
    assert _waitFor(lambda: _statusIs(env.repo, nTid, 'waiting'))
    assert prompts                                   # 第一轮真的跑了


# ---- 3. close 必须确保节点侧 kimi 进程退出 ----

def test_close_normal_path_also_releases_node_session(env, monkeypatch):
    """正常关闭（线程自行收尾）也按 sessionId 结束节点会话——原实现只在
    强制终止分支里做，正常路径全靠线程 finally，卡住即残留持锁进程。"""
    calls = []
    monkeypatch.setattr(env.nodes, 'shutdownNodeSession',
                        lambda node, sid: calls.append((node, sid)) or True)
    tid = _taskId(create(env, {'prompt': 'x'}))
    with env.repo.lock:
        task = env.repo.get(tid)
    release = threading.Event()
    release.set()
    _runThread(task, _factory(release=release))
    assert _waitFor(lambda: _statusIs(env.repo, tid, 'waiting'))
    resp = env.client.post('/api/tasks/%s/close' % tid, headers=H)
    assert resp.status_code == 200
    assert calls == [('local', 'sess-dr1')]


def test_close_releases_node_session_when_thread_fails(env, monkeypatch):
    """会话线程以 failed 落地、close 等待圈因此提前跳出：仍须杀节点会话
    （旧代码此路径不杀——接回立刻 "in use"）。"""
    calls = []
    monkeypatch.setattr(env.nodes, 'shutdownNodeSession',
                        lambda node, sid: calls.append((node, sid)) or True)
    monkeypatch.setattr(env.nodes, 'findNode',
                        lambda name: {'name': name, 'url': 'http://stub'})
    monkeypatch.setattr(env.nodes, 'findFederationMember', lambda name: None)
    tid = _taskId(create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                               'cwd': '/mnt/x'}))
    with env.repo.lock:
        task = env.repo.get(tid)
    _runThread(task, _factory(fail=True, delay=0.5))
    assert _waitFor(lambda: _statusIs(env.repo, tid, 'running'))
    resp = env.client.post('/api/tasks/%s/close' % tid, headers=H)
    assert resp.status_code == 200
    assert calls == [('ai-xmyz', 'sess-dr1')]
    with env.repo.lock:
        assert env.repo.get(tid)['status'] in ('failed', 'closed')


# ---- 4. 关闭后立即接回：in-use 善后 ----

def test_adopt_retries_after_stale_session_kill(env, monkeypatch):
    """关闭后立即接回：节点先报 in use（残留持锁进程）→ 按未关闭占用判定
    经节点结束会话 → 重试成功。"""
    monkeypatch.setattr(session_adopt, 'KILL_WAIT_SEC', 0)
    killed = []
    monkeypatch.setattr(env.nodes, 'shutdownNodeSession',
                        lambda node, sid: killed.append((node, sid)) or True)
    monkeypatch.setattr(env.nodes, 'findNode',
                        lambda name: {'name': name, 'url': 'http://stub'})
    sid = _sid('f')
    posts = []

    def fakeRequest(node, method, path, payload=None, timeout=30):
        posts.append((method, path))
        if method == 'POST' and path == '/api/sessions':
            creates = [p for p in posts
                       if p[0] == 'POST' and p[1] == '/api/sessions']
            if len(creates) == 1:
                return 400, {'error': 'This session is in use by another '
                                      'Kimi Code CLI instance'}
            return 200, {'sid': 's2', 'kimiSessionId': 'k2',
                         'attachSeq': 0, 'replayCount': 0}
        return 200, {}
    monkeypatch.setattr(env.nodes, 'nodeRequest', fakeRequest)

    client = env.nodes.RemoteAcpClient(cwd='/mnt/x', node='ai-xmyz',
                                       kimi_session_id=sid, fork=False)
    client._closed = True              # 别让事件流线程空转
    task = {'id': 't-adopt', 'node': 'ai-xmyz', 'sessionId': '',
            'kimiSessionId': sid}
    got = session_adopt.startSession(client, task)
    assert got == 'k2' and client.node_sid == 's2'
    assert killed == [('ai-xmyz', sid)]       # 经节点结束了残留会话
    assert len([p for p in posts if p[1] == '/api/sessions']) == 2


def test_adopt_with_live_holder_gets_409_and_no_kill(env, monkeypatch):
    """未关闭任务在用的会话：直接接管 409 点名，不强杀节点会话。"""
    killed = []
    monkeypatch.setattr(env.nodes, 'shutdownNodeSession',
                        lambda node, sid: killed.append((node, sid)) or True)
    monkeypatch.setattr(env.nodes, 'findNode',
                        lambda name: {'name': name, 'url': 'http://stub'})
    monkeypatch.setattr(env.nodes, 'findFederationMember', lambda name: None)
    sid = _sid('c')
    first = create(env, {'prompt': '先接管', 'node': 'ai-xmyz',
                         'cwd': '/mnt/x', 'kimiSessionId': sid,
                         'fork': False})
    assert first.status_code == 202
    second = create(env, {'prompt': '再接管', 'node': 'ai-xmyz',
                          'cwd': '/mnt/x', 'kimiSessionId': sid,
                          'fork': False})
    assert second.status_code == 409
    body = second.get_json()
    assert '占用' in body['error']
    assert body['holders'][0]['id'] == _taskId(first)
    assert killed == []


def test_holders_of_counts_unclosed_only(env):
    """占用判定：closed/cancelled 不算占用；接回任务自己可排除。"""
    sid = _sid('b')
    with env.repo.lock:
        env.repo.put({'id': 'live1', 'status': 'waiting',
                      'sessionId': sid, 'title': '活的'})
        env.repo.put({'id': 'gone1', 'status': 'closed',
                      'sessionId': sid, 'title': '关的'})
        env.repo.put({'id': 'self1', 'status': 'queued',
                      'kimiSessionId': sid, 'title': '自己'})
        env.repo.save()
    got = session_adopt.holdersOf(sid)
    assert [h['id'] for h in got] == ['live1', 'self1']
    assert session_adopt.holdersOf(sid, excludeTaskId='self1') == [got[0]]
    assert session_adopt.holdersOf('') == []
