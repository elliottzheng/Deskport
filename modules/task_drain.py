# -*- coding: utf-8 -*-
"""部署排空（批次 DR）：停止接受新的一轮开始，等在飞回合自然结束。

deploy.ps1 同步代码前 POST /api/system/drain 走进来：中枢把"新一轮开始"
的入口全部挡住（新消息/新任务照常排队，不拒绝），等在飞回合（status=
running 且有活客户端）自然结束或超时。超时也放行部署，但把仍在跑的任务
标记 pausedBy='restart'——web 重启后 store.load 把它们打成 paused，批次
L3 据此自动续接。

与接回链路的关系：kimi 的会话锁随持锁进程死亡释放，所以"等一轮自然结束"
只覆盖回合本身；任务关闭时的杀进程在 session_adopt / closeTaskCore。
"""
import logging
import threading
import time

from modules import tasks_store as store

log = logging.getLogger('tasks')

DEFAULT_TIMEOUT_SEC = 300
MAX_TIMEOUT_SEC = 3600
POLL_SEC = 0.5

_cond = threading.Condition()
_state = {'active': False, 'deadline': 0.0}


def draining():
    """是否正在排空（新一轮入口据此改排队）。"""
    with _cond:
        return _state['active']


def waitWhileDraining(task=None):
    """新一轮开始前的闸门：排空期间阻塞，排空结束放行。

    返回 True = 等待期间任务已被关闭/终态，调用方不要开这一轮
    （_promptOnce 直接返回，交回会话循环走 close 收尾）。
    """
    with _cond:
        while _state['active']:
            if task is not None and (task.get('closedByUser')
                                     or (task.get('status') or '')
                                     in ('closed', 'cancelled')):
                return True
            _cond.wait(0.5)
    return False


def _runningRounds():
    """在飞回合：status=running 且有活客户端的任务（轮中间的形态）。"""
    store.repo.load()
    with store.repo.lock:
        return [t for t in store.repo.list()
                if t.get('status') == 'running'
                and store.repo.agentOf(t['id']) is not None]


def _brief(tasks):
    return [{'id': t.get('id') or '', 'title': t.get('title') or '',
             'node': t.get('node') or '', 'status': t.get('status') or '',
             'owner': t.get('owner') or ''} for t in tasks]


def snapshot():
    """当前在飞回合清单（探针与响应共用）。"""
    return _brief(_runningRounds())


def _markRestart(tasks):
    """超时放行：仍在跑的任务标记 pausedBy='restart'（批次 L3 续接的依据）。"""
    for t in tasks:
        with store.repo.lock:
            t['pausedBy'] = 'restart'
        store.repo.append_event(
            t, 'status',
            text='部署排空超时，本轮未结束：已标记 pausedBy=restart，'
                 'web 重启后将自动续接')


def _wakeQueuedAgents():
    """排空结束：唤醒有持久排队消息的会话线程——它们在
    _drainQueuedMessages 被挡回后一直阻塞在 queue.get()。"""
    pokes = []
    with store.repo.lock:
        for tid, agent in list(store.repo.agents.items()):
            task = store.repo.get(tid)
            if task and (task.get('messageQueue') or []):
                pokes.append(agent)
    for agent in pokes:
        try:
            agent['queue'].put({'type': 'drain'})
        except Exception:
            pass


def runDrain(timeoutSec=DEFAULT_TIMEOUT_SEC):
    """排空主流程（POST /api/system/drain 调用，阻塞至排空结束/超时）。

    timeoutSec=0 = 探针：只报在飞回合，不进入排空。
    已在排空中 → 带 error 的报告（端点回 409）。
    """
    try:
        timeoutSec = int(timeoutSec)
    except (TypeError, ValueError):
        timeoutSec = DEFAULT_TIMEOUT_SEC
    timeoutSec = max(0, min(timeoutSec, MAX_TIMEOUT_SEC))
    with _cond:
        if _state['active']:
            return {'error': '排空已在进行中', 'running': snapshot()}
        if timeoutSec <= 0:
            running = snapshot()
            return {'probe': True, 'running': running, 'count': len(running)}
        _state['active'] = True
        _state['deadline'] = time.monotonic() + timeoutSec
        _cond.notify_all()
    started = time.monotonic()
    try:
        initial = _runningRounds()
        log.info('drain: 等待 %d 个任务的当前一轮结束……', len(initial))
        while True:
            if not _runningRounds():
                return {'drained': True, 'timedOut': False,
                        'initial': _brief(initial), 'remaining': [],
                        'waitedSec': round(time.monotonic() - started, 1)}
            if time.monotonic() >= _state['deadline']:
                break
            time.sleep(POLL_SEC)
        remaining = _runningRounds()
        _markRestart(remaining)
        return {'drained': False, 'timedOut': True,
                'initial': _brief(initial), 'remaining': _brief(remaining),
                'waitedSec': round(time.monotonic() - started, 1)}
    finally:
        with _cond:
            _state['active'] = False
            _state['deadline'] = 0.0
            _cond.notify_all()
        _wakeQueuedAgents()
