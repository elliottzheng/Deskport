# -*- coding: utf-8 -*-
"""执行点分派表：按节点类型（local / agent / federation）注册启动器。

执行点只暴露 start(task, manifest)，runner 不再 if/else。
新增类型只需 register(kind, runner)，不改 tasks_runner。
"""
import logging
import threading
from typing import Protocol

log = logging.getLogger('task-executors')

KINDS = ('local', 'agent', 'federation')


class TaskRunner(Protocol):
    def start(self, task, manifest): ...


class ThreadRunner:
    """把现有线程体适配成 TaskRunner（Adapter + Strategy）。"""

    def __init__(self, target, **kwargs):
        self.target = target
        self.kwargs = kwargs

    def start(self, task, manifest):
        threading.Thread(target=self.target, args=(task, manifest),
                         kwargs=self.kwargs, daemon=True).start()


_runners = {}


def register(kind, runner):
    _runners[(kind or 'local').lower()] = runner


def get(kind):
    key = (kind or 'local').lower()
    runner = _runners.get(key)
    if runner is None:
        log.warning('未知执行点类型 %s，回退 local', key)
        runner = _runners.get('local')
    return runner