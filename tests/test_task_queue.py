# -*- coding: utf-8 -*-
"""R5-5 轮中插话：持久化消息队列（queue 默认 / steer 打断 / reject 409）。

覆盖：轮中入队与轮末按序投递、steer 插队首打断、排队查看/撤回、
入队后 web 重启（repo 重载）再投递、挂起审批时入队但审批落定前不投递。
"""
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_runner as runner
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
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo,
                           tmp=tmp_path, nodesMod=nodesMod)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def msg(env, tid, body):
    return env.client.post('/api/tasks/%s/message' % tid, headers=H,
                           data=json.dumps(body),
                           content_type='application/json')


def makeClient(record, blocker=None, cancelCalls=None, sid='sess-q'):
    """prompt 逐条记账；第一轮可在 blocker 上阻塞（模拟长轮），
    cancel() 解除阻塞并返回（steer 打断路径）。"""
    class ScriptedClient:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0, model=''):
            self.on_update = on_update or (lambda u: None)
            self.on_permission = on_permission
            self.cancelCalls = cancelCalls if cancelCalls is not None else []

        def start(self, session_id=None):
            return sid

        def prompt(self, text, timeout=None):
            record.append(text)
            if blocker is not None and len(record) == 1:
                blocker.wait(10)
            return {'stopReason': 'end_turn' if blocker is None
                    or len(record) > 1 else 'cancelled'}

        def set_mode(self, mode):
            pass

        def cancel(self):
            self.cancelCalls.append('cancel')
            if blocker is not None:
                blocker.set()

        def close(self):
            pass

        def shutdown(self):
            pass
    return ScriptedClient


def startThread(env, tid, factory):
    from modules import tasks_runner as runner
    with env.repo.lock:
        task = env.repo.get(tid)
    threading.Thread(target=runner._sessionThread, args=(task, []),
                     kwargs={'clientFactory': factory},
                     daemon=True).start()
    return task


def waitStatus(task, want, timeout=10):
    for _ in range(int(timeout * 10)):
        with store.repo.lock:
            if task.get('status') == want:
                return True
        time.sleep(0.1)
    return False


def waitQueueEmpty(task, timeout=10):
    for _ in range(int(timeout * 10)):
        with store.repo.lock:
            if not (task.get('messageQueue') or []):
                return True
        time.sleep(0.1)
    return False


def waitDelivered(record, n, timeout=10):
    for _ in range(int(timeout * 10)):
        if len(record) >= n:
            return True
        time.sleep(0.1)
    return False


# ---- 轮中排队：默认 queue，轮末按序投递 ----

def test_queue_mode_enqueues_mid_round_and_delivers_in_order(env):
    record = []
    blocker = threading.Event()
    resp = create(env, {'prompt': '做点事'})
    tid = resp.get_json()['task']['id']
    task = startThread(env, tid, makeClient(record, blocker))
    assert waitStatus(task, 'running'), '首轮应进入 running'
    r1 = msg(env, tid, {'text': '补充一'})
    r2 = msg(env, tid, {'text': '补充二'})
    assert r1.status_code == 202 and r2.status_code == 202
    b1, b2 = r1.get_json(), r2.get_json()
    assert b1['queued'] is True and b1['position'] == 1
    assert b2['queued'] is True and b2['position'] == 2
    blocker.set()   # 放行首轮 → 轮末清队
    assert waitDelivered(record, 3) and waitStatus(task, 'waiting')
    assert record[1:] == ['补充一', '补充二'], '按入队顺序投递'
    with store.repo.lock:
        userTexts = [e['text'] for e in task['events']
                     if e.get('type') == 'user'
                     and e.get('text') in ('补充一', '补充二')]
    assert userTexts == ['补充一', '补充二']
    # 事件卡可见排队痕迹（投递前有 status 事件）
    with store.repo.lock:
        statuses = [e['text'] for e in task['events'] if e.get('type') == 'status']
    assert any('已排队' in s for s in statuses)


# ---- steer：插队首 + 打断当前轮 ----

def test_steer_cancels_round_and_delivers_first(env):
    record = []
    cancelCalls = []
    blocker = threading.Event()
    resp = create(env, {'prompt': '做点事'})
    tid = resp.get_json()['task']['id']
    task = startThread(env, tid, makeClient(record, blocker,
                                            cancelCalls=cancelCalls))
    assert waitStatus(task, 'running')
    r1 = msg(env, tid, {'text': '普通排队'})
    assert r1.get_json()['position'] == 1
    r2 = msg(env, tid, {'text': '插话', 'mode': 'steer'})
    assert r2.status_code == 202
    body = r2.get_json()
    assert body['queued'] is True and body['steered'] is True
    assert body['position'] == 1, 'steer 插队首'
    assert cancelCalls == ['cancel'], 'steer 应对当前轮调 cancel'
    assert waitDelivered(record, 3) and waitStatus(task, 'waiting')
    assert record[1:] == ['插话', '普通排队'], 'steer 优先于先排队的消息'
    with store.repo.lock:
        userEvents = [e for e in task['events']
                      if e.get('type') == 'user' and e.get('text') == '插话']
    assert userEvents and userEvents[-1].get('steer') is True
    with store.repo.lock:
        statuses = [e['text'] for e in task['events'] if e.get('type') == 'status']
    assert any('插话' in s and '打断' in s for s in statuses)


# ---- 查看与撤回 ----

def test_queued_messages_list_and_retract(env):
    record = []
    blocker = threading.Event()
    resp = create(env, {'prompt': '做点事'})
    tid = resp.get_json()['task']['id']
    task = startThread(env, tid, makeClient(record, blocker))
    assert waitStatus(task, 'running')
    m1 = msg(env, tid, {'text': '第一条'}).get_json()
    m2 = msg(env, tid, {'text': '第二条'}).get_json()
    detail = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()['task']
    assert detail['queuedMessages'] == 2
    eventsBody = env.client.get('/api/tasks/%s/events' % tid,
                                headers=H).get_json()
    assert eventsBody['queuedMessages'] == 2
    listing = env.client.get('/api/tasks/%s/messages' % tid,
                             headers=H).get_json()
    assert listing['count'] == 2
    assert [i['text'] for i in listing['items']] == ['第一条', '第二条']
    assert listing['items'][0]['id'] == m1['messageId']
    assert listing['items'][0]['steer'] is False
    gone = env.client.delete('/api/tasks/%s/messages/%s'
                             % (tid, m1['messageId']), headers=H)
    assert gone.status_code == 200 and gone.get_json()['count'] == 1
    missing = env.client.delete('/api/tasks/%s/messages/%s'
                                % (tid, m1['messageId']), headers=H)
    assert missing.status_code == 404, '已撤回的再删 404'
    blocker.set()
    assert waitDelivered(record, 2)
    assert record[1:] == ['第二条'], '撤回的那条不投递'


def test_reject_mode_keeps_409_and_bad_mode_is_400(env):
    record = []
    blocker = threading.Event()
    resp = create(env, {'prompt': '做点事'})
    tid = resp.get_json()['task']['id']
    task = startThread(env, tid, makeClient(record, blocker))
    assert waitStatus(task, 'running')
    r = msg(env, tid, {'text': 'x', 'mode': 'reject'})
    assert r.status_code == 409
    assert '这一轮还在执行' in r.get_json()['error']
    bad = msg(env, tid, {'text': 'x', 'mode': 'barge'})
    assert bad.status_code == 400
    blocker.set()
    assert waitStatus(task, 'waiting')


# ---- 持久化：入队后 web 重启（repo 重载）再投递 ----

def test_queued_message_survives_restart_and_delivers_on_resume(env, monkeypatch):
    record = []
    blocker = threading.Event()
    resp = create(env, {'prompt': '做点事'})
    tid = resp.get_json()['task']['id']
    task = startThread(env, tid, makeClient(record, blocker))
    assert waitStatus(task, 'running')
    assert msg(env, tid, {'text': '重启前排队'}).status_code == 202
    # 模拟重启：磁盘上任务还在 running（带 sessionId）→ load() 转 paused
    repo2 = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
    repo2.load()
    with repo2.lock:
        persisted = repo2.get(tid)
    assert persisted.get('status') == 'paused', '重启后 running → paused'
    assert [i['text'] for i in persisted.get('messageQueue') or []] == ['重启前排队']
    monkeypatch.setattr(store, 'repo', repo2)
    record2 = []
    monkeypatch.setattr(env.nodesMod, 'clientFactoryFor',
                        lambda t: makeClient(record2, sid='sess-r2'))
    r = msg(env, tid, {'text': '重启后续聊'})
    assert r.status_code == 202, r.get_json()
    # 轮询预算 30s：全量跑时 CPU 争抢下 resume 线程（session/load + 清积压）
    # 可能超过 10s——2026-11-14 全量里这个断言 flakes 过一次，单独跑 5/5 过
    for _ in range(300):
        if len(record2) >= 2:
            break
        time.sleep(0.1)
    assert record2 == ['重启前排队', '重启后续聊'], \
        'resume 先清重启前的积压，再执行本次续聊'
    with store.repo.lock:
        assert repo2.get(tid).get('status') == 'waiting'


# ---- 挂起审批：入队放行，审批落定前不投递 ----

def test_queue_with_live_pending_delivers_only_after_permission(env):
    record = []
    resp = create(env, {'prompt': '做点事', 'policy': {}})
    tid = resp.get_json()['task']['id']

    class ApprovalClient:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0, model=''):
            self.on_update = on_update or (lambda u: None)
            self.on_permission = on_permission

        def start(self, session_id=None):
            return 'sess-a1'

        def prompt(self, text, timeout=None):
            record.append(text)
            if len(record) == 1:
                # 首轮发起一次工具调用审批并原地等决策（runner 线程）
                self.on_permission(
                    {'toolCall': {'toolCallId': 'c1', 'title': 'Bash',
                                  'kind': 'execute',
                                  'rawInput': {'command': 'echo x'}},
                     'options': [{'optionId': 'o1', 'name': '允许',
                                  'kind': 'allow_once'}]}, 'req-1')
            return {'stopReason': 'end_turn'}

        def set_mode(self, mode):
            pass

        def cancel(self):
            pass

        def close(self):
            pass

        def shutdown(self):
            pass

    task = startThread(env, tid, ApprovalClient)
    for _ in range(100):
        with store.repo.lock:
            if task.get('pendingPermission'):
                break
        time.sleep(0.1)
    assert task.get('pendingPermission') == 'req-1'
    r = msg(env, tid, {'text': '审批后再说'})
    assert r.status_code == 202 and r.get_json()['queued'] is True, \
        '有挂起审批时 queue 照常入队（不再 409）'
    time.sleep(0.5)
    with store.repo.lock:
        mq = task.get('messageQueue') or []
        seqs = {e.get('type'): e.get('seq', 0) for e in task['events']}
    assert len(mq) == 1, '审批未落定，闸门不投递'
    assert len(record) == 1, '审批悬着时不开新一轮'
    perm = env.client.post('/api/tasks/%s/permission' % tid, headers=H,
                           data=json.dumps({'requestId': 'req-1',
                                            'optionId': 'o1'}),
                           content_type='application/json')
    assert perm.status_code == 200
    assert waitDelivered(record, 2) and waitStatus(task, 'waiting')
    assert record[1:] == ['审批后再说'], '审批落定后投递'
    with store.repo.lock:
        events = task['events']
    permSeq = max(e['seq'] for e in events if e['type'] == 'permission_result')
    userSeq = min(e['seq'] for e in events
                  if e['type'] == 'user' and e.get('text') == '审批后再说')
    assert permSeq < userSeq, '先批完审批，再投排队消息'
