# -*- coding: utf-8 -*-
"""任务状态机：唯一的状态集合、迁移表与派生标志。

任务字典里仍然存字符串（兼容前端与已有状态文件），但所有写入都经过
transition()，避免 store/runner/api 各自解释状态。
"""
import logging
from enum import StrEnum

log = logging.getLogger('task-state')


class TaskState(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    WAITING = 'waiting'
    PAUSED = 'paused'
    CLOSED = 'closed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    INTERRUPTED = 'interrupted'
    DONE = 'done'          # legacy engine='p' 的完成态


TRANSITIONS = {
    TaskState.QUEUED: {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED,
                       TaskState.CANCELLED, TaskState.INTERRUPTED, TaskState.CLOSED},
    TaskState.RUNNING: {TaskState.WAITING, TaskState.PAUSED, TaskState.FAILED,
                        TaskState.CANCELLED, TaskState.CLOSED, TaskState.DONE,
                        TaskState.INTERRUPTED},
    TaskState.WAITING: {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED,
                        TaskState.CANCELLED, TaskState.CLOSED, TaskState.INTERRUPTED},
    TaskState.PAUSED: {TaskState.QUEUED, TaskState.RUNNING, TaskState.FAILED,
                       TaskState.CLOSED},
    TaskState.FAILED: {TaskState.QUEUED, TaskState.CLOSED},
    TaskState.CANCELLED: {TaskState.CLOSED},
    TaskState.INTERRUPTED: {TaskState.QUEUED, TaskState.CLOSED},
    TaskState.CLOSED: set(),
    TaskState.DONE: {TaskState.CLOSED},
}

CONTINUABLE = {TaskState.WAITING, TaskState.PAUSED}
RESUMABLE = {TaskState.FAILED, TaskState.INTERRUPTED}


def stateOf(task):
    raw = (task or {}).get('status') or TaskState.QUEUED.value
    try:
        return TaskState(raw)
    except ValueError:
        return TaskState.QUEUED


def transition(task, to, reason=''):
    """迁移任务状态；非法迁移只告警，不中断业务流程。"""
    target = TaskState(to) if not isinstance(to, TaskState) else to
    current = stateOf(task)
    if current is target:
        task['status'] = target.value
        return target
    if target not in TRANSITIONS.get(current, set()):
        log.warning('非法任务状态迁移 %s -> %s (%s)', current.value, target.value,
                    reason or task.get('id') or '')
    task['status'] = target.value
    return target


def canContinue(task):
    return stateOf(task) in CONTINUABLE and bool((task or {}).get('sessionId'))


def canResume(task):
    return stateOf(task) in RESUMABLE and bool((task or {}).get('sessionId'))