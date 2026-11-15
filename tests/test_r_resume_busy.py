# -*- coding: utf-8 -*-
"""批次 R 问题 1：重启后续接撞 409——先跟完旧回合再投新消息，不判失败。

形态 A（调查 §3，5 例中 4 例）：web 重启时回合正卡在工具调用上，节点侧
kimi 进程继续跑；主控发消息触发续接 → 新会话线程 attach 到节点上仍在
running 的同一会话 → prompt 被节点 409 → 任务直接 failed、新消息丢失，
之后旧回合的 tool/tool_result 还在继续到达。

本文件用假节点（不起真 kimi）：stub nodeRequest/findNode，_streamLoop
置空、事件由测试直喂 _handleEvent（与 test_r45a_replay 同款手法）。
另覆盖问题 2 的产品根因回归：并发 save 互撞丢持久化（store_io）。
"""
import hashlib
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_busy
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules import tasks_runner as runner
from modules import agent_nodes as nodesMod
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'r' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}

FOLLOWUP = '重启后续聊'


class FakeNode:
    """假节点：prompt 按计划返回 409/202，会话 running 可切，决策落账。"""

    def __init__(self):
        self.prompts = []        # 按序收到的 prompt 文本
        self.promptPlan = []     # 每次 POST prompt 的返回码（空则 202）
        self.running = True
        self.cancels = 0
        self.permissions = []    # (requestId, optionId)
        self.lock = threading.Lock()

    def request(self, node, method, path, payload=None, timeout=30):
        if method == 'POST' and path == '/api/sessions':
            return 200, {'sid': 'sn1', 'kimiSessionId': 'k1',
                         'attachSeq': 0, 'replayCount': 0}
        if method == 'GET' and path.startswith('/api/sessions/'):
            with self.lock:
                running = self.running
            return 200, {'session': {'sid': 'sn1', 'running': running,
                                     'dead': False}}
        if method == 'POST' and path.endswith('/prompt'):
            text = str((payload or {}).get('text') or '')
            with self.lock:
                self.prompts.append(text)
                code = self.promptPlan.pop(0) if self.promptPlan else 202
            if code == 409:
                return 409, {'error': '会话正在执行，请稍候'}
            return 202, {'accepted': True}
        if method == 'POST' and path.endswith('/permission'):
            with self.lock:
                self.permissions.append(
                    (str((payload or {}).get('requestId') or ''),
                     str((payload or {}).get('optionId') or '')))
            return 200, {'ok': True}
        if method == 'POST' and path.endswith('/cancel'):
            with self.lock:
                self.cancels += 1
            return 200, {'ok': True}
        return 404, {'error': 'not found: %s %s' % (method, path)}


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
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def startResume(env, monkeypatch, fake, firstMessage=FOLLOWUP):
    """建任务 + 用真 RemoteAcpClient（product 的 on_status 接线）起 resume
    会话线程。返回 (task, holder)。"""
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': '做点事'}),
                           content_type='application/json')
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
    holder = {}
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'url': 'http://fake'})
    monkeypatch.setattr(nodesMod, 'nodeRequest', fake.request)
    monkeypatch.setattr(nodesMod.RemoteAcpClient, '_streamLoop',
                        lambda self: None)

    def factory(cwd, on_update=None, on_permission=None, mode='auto',
                prompt_timeout=0):
        client = nodesMod.RemoteAcpClient(
            cwd=cwd, on_update=on_update, on_permission=on_permission,
            mode=mode, prompt_timeout=prompt_timeout, node='fake-node',
            on_status=agent_busy.taskStatusEmitter(task))
        holder['client'] = client
        return client

    threading.Thread(target=runner._sessionThread,
                     args=(task, [], True, firstMessage),
                     kwargs={'clientFactory': factory}, daemon=True).start()
    return task, holder


def waitStatus(task, want, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store.repo.lock:
            if task.get('status') == want:
                return True
        time.sleep(0.05)
    return False


def waitFor(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def eventTexts(task, etype):
    with store.repo.lock:
        return [e.get('text') or '' for e in task.get('events') or []
                if e.get('type') == etype]


def eventTypes(task):
    with store.repo.lock:
        return [e.get('type') for e in task.get('events') or []]


def pushTurnEnd(client, stopReason='end_turn'):
    """模拟事件流线程推来一个回合的结束事件。"""
    client._handleEvent({'type': 'turn_end', 'stopReason': stopReason,
                         'seq': int(time.time() * 1000) % 100000})


def waitBusyStatus(task):
    return waitFor(lambda: any('节点上一轮仍在执行' in t
                               for t in eventTexts(task, 'status')))


# ---- 409 + running：等旧回合结束再投 ----

def test_resume_busy_waits_old_turn_then_delivers(env, monkeypatch):
    fake = FakeNode()
    fake.promptPlan = [409, 202]
    task, holder = startResume(env, monkeypatch, fake)
    assert waitBusyStatus(task), '撞忙时应落 status 事件说明在等旧回合'
    assert fake.prompts == [FOLLOWUP], '此刻只发过第一次（被 409 拒）'
    # 等待期间旧回合的 live 事件照常落卡（不当回放丢掉）
    holder['client']._handleEvent({
        'type': 'update', 'seq': 2,
        'update': {'sessionUpdate': 'agent_message_chunk',
                   'content': {'type': 'text', 'text': '旧回合残响'}}})
    # 旧回合结束：turn_end 到达（running 也转假）
    fake.running = False
    pushTurnEnd(holder['client'])
    assert waitFor(lambda: len(fake.prompts) >= 2), '旧回合结束后应重投'
    pushTurnEnd(holder['client'])          # 新回合结束
    assert waitStatus(task, 'waiting'), '两条消息跑完应回 waiting（不是 failed）'
    assert fake.prompts == [FOLLOWUP, FOLLOWUP], \
        '第二次 POST 投递的就是新消息'
    assert 'error' not in eventTypes(task)
    assert any('旧回合残响' in t for t in eventTexts(task, 'assistant')), \
        '等待期间旧回合的 live 事件不得被丢掉'
    env.client.post('/api/tasks/%s/close' % task['id'], headers=H)


def test_retry_not_released_by_stale_turn_end(env, monkeypatch):
    """旧回合的 turn_end 已到（那正是等它的信号）：重投前必须清掉，否则新
    回合会被瞬间放行、半路返回。"""
    fake = FakeNode()
    fake.promptPlan = [409, 202]
    task, holder = startResume(env, monkeypatch, fake)
    assert waitBusyStatus(task)
    fake.running = False
    pushTurnEnd(holder['client'])          # 旧回合结束
    assert waitFor(lambda: len(fake.prompts) >= 2), '应重投'
    time.sleep(1.0)
    with env.repo.lock:
        assert task.get('status') == 'running', '新回合不应被旧 turn_end 放行'
    pushTurnEnd(holder['client'])          # 新回合真正结束
    assert waitStatus(task, 'waiting')
    env.client.post('/api/tasks/%s/close' % task['id'], headers=H)


# ---- 一直 409 / 假 409：重投上限与失败文案 ----

def test_resume_busy_all_409_fails_after_three_retries(env, monkeypatch):
    fake = FakeNode()
    fake.promptPlan = [409, 409, 409, 409]
    task, holder = startResume(env, monkeypatch, fake)
    assert waitBusyStatus(task)
    # 每次重投前旧回合都结束（等待不设总时限，不会自己往下走）
    for expect in (2, 3, 4):
        pushTurnEnd(holder['client'])
        assert waitFor(lambda n=expect: len(fake.prompts) >= n), \
            '第 %d 次 POST 应发生' % expect
    assert waitStatus(task, 'failed'), '3 次重投仍 409 才失败'
    with env.repo.lock:
        err = task.get('error') or ''
    assert '节点会话持续忙' in err and '已等待' in err, err
    assert len(fake.prompts) == 4, '首次 + 3 次重投'


def test_resume_busy_not_running_409_fails_with_node_text(env, monkeypatch):
    """running 为假却仍 409（调查 §7.2 不明例同形）：按 2 秒间隔重投 3 次，
    仍失败则失败并带上节点返回的原文。"""
    fake = FakeNode()
    fake.running = False
    fake.promptPlan = [409, 409, 409, 409]
    task, holder = startResume(env, monkeypatch, fake)
    assert waitStatus(task, 'failed', timeout=20)
    assert fake.prompts == [FOLLOWUP] * 4, '首次 + 3 次重投'
    with env.repo.lock:
        err = task.get('error') or ''
    assert '会话正在执行，请稍候' in err, 'error 须带节点返回原文: %s' % err


# ---- 等待中 close：不能卡住 ----

def test_close_during_busy_wait_lands_quickly(env, monkeypatch):
    fake = FakeNode()
    fake.promptPlan = [409]                # 重投永不发生（先被 close 打断）
    task, holder = startResume(env, monkeypatch, fake)
    assert waitBusyStatus(task)
    t0 = time.monotonic()
    resp = env.client.post('/api/tasks/%s/close' % task['id'], headers=H)
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 2, 'close 不能卡在续接等待上（%.1fs）' % elapsed
    assert waitFor(lambda: task.get('status') in ('closed', 'cancelled'),
                   timeout=2), task.get('status')
    assert fake.cancels >= 1, 'close 路径应调过 cancel'


# ---- 等待期间旧回合的 live 审批：挂起、批准、再投 ----

def test_live_permission_during_busy_wait_suspends_and_approves(env,
                                                                monkeypatch):
    fake = FakeNode()
    fake.promptPlan = [409, 202]
    task, holder = startResume(env, monkeypatch, fake)
    assert waitBusyStatus(task)
    # 事件流线程推来一条 live 审批（独立线程里挂起等决策）
    def streamPerm():
        holder['client']._handleEvent({
            'type': 'permission_request', 'requestId': '7',
            'title': 'Bash', 'callId': 'c1',
            'args': '{"command": "echo x"}', 'paths': [],
            'options': [{'optionId': 'allow-1', 'name': '允许',
                         'kind': 'allow_once'}], 'seq': 3})
    t = threading.Thread(target=streamPerm, daemon=True)
    t.start()
    assert waitFor(lambda: bool(task.get('pendingPermission'))), '审批应挂起'
    assert task.get('pendingPermission') == '7'
    perm = env.client.post('/api/tasks/%s/permission' % task['id'], headers=H,
                           data=json.dumps({'requestId': '7',
                                            'optionId': 'allow-1'}),
                           content_type='application/json')
    assert perm.status_code == 200
    t.join(5)
    assert ('7', 'allow-1') in fake.permissions, '批准应到达节点'
    # 批准后旧回合结束 → 重投 → 新回合结束 → waiting
    fake.running = False
    pushTurnEnd(holder['client'])
    assert waitFor(lambda: len(fake.prompts) >= 2)
    pushTurnEnd(holder['client'])
    assert waitStatus(task, 'waiting')
    assert fake.prompts[1] == FOLLOWUP
    types = eventTypes(task)
    assert 'permission_request' in types and 'permission_result' in types
    env.client.post('/api/tasks/%s/close' % task['id'], headers=H)


# ---- 问题 2 产品根因回归：并发 save 互撞 / 读盘被误判无状态 ----

def test_concurrent_saves_never_collide(tmp_path):
    """并发 save 曾共用同一 .tmp、并发 os.replace 同一目标，大量 WinError
    5/32 静默丢持久化（线上 160 次 save tasks failed 与重启续接读不到积压
    队列的同源根因）：修后零失败、盘上状态完整、不留临时文件。"""
    from modules.tasks_store import TaskRepository
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    for i in range(50):
        repo.put({'id': 't%02d' % i, 'status': 'waiting', 'events': [],
                  'seq': 0, 'x': 'y' * 200})

    caught = []

    class _H(logging.Handler):
        def emit(self, record):
            caught.append(record.getMessage())

    handler = _H()
    log = logging.getLogger('tasks')
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    try:
        def worker():
            for _ in range(60):
                repo.save()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        log.removeHandler(handler)
    fails = [m for m in caught if 'save tasks failed' in m]
    # 批 PS2 追件：os.replace 的 PermissionError（WinError 5/32，安全软件/
    # 索引器对目标的瞬间占用）已在 store_io 按 20/40/80/160ms 退避重试 4 次
    # ——瞬时占用不再变成失败，这里恢复零失败（另一批曾因此放宽到 ≤2 次）
    assert not fails, '并发 save 零失败: %s' % fails[:2]
    data = json.loads((tmp_path / 'tasks.json').read_text(encoding='utf-8'))
    assert len(data) == 50, '盘上状态完整'
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith('.tasks-')]
    assert leftovers == [], '不留临时文件: %s' % leftovers


def test_load_snapshot_tolerates_missing_and_corrupt(tmp_path):
    """读盘把瞬时共享冲突/锁冲突当"无状态"会在启动时丢全部任务：文件真
    缺失/内容损坏才按空处理（store_io.loadSnapshot）。"""
    from modules import store_io
    path = tmp_path / 'tasks.json'
    assert store_io.loadSnapshot(str(path)) == {}
    path.write_text('{not json', encoding='utf-8')
    assert store_io.loadSnapshot(str(path)) == {}
    path.write_text('{"a": 1}', encoding='utf-8')
    assert store_io.loadSnapshot(str(path)) == {'a': 1}
