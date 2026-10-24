# -*- coding: utf-8 -*-
"""任务引擎策略：引擎只暴露 start(task, manifest)，runner 不再 if/else。

新增引擎只需 register(name, engine)，不改 tasks_runner。
"""
import logging
import threading
from typing import Protocol

log = logging.getLogger('task-engines')


class TaskEngine(Protocol):
    def start(self, task, manifest): ...


class ThreadEngine:
    """把现有线程体适配成 TaskEngine（Adapter + Strategy）。"""

    def __init__(self, target, **kwargs):
        self.target = target
        self.kwargs = kwargs

    def start(self, task, manifest):
        threading.Thread(target=self.target, args=(task, manifest),
                         kwargs=self.kwargs, daemon=True).start()


_engines = {}


def register(name, engine):
    _engines[(name or 'acp').lower()] = engine


def get(name):
    key = (name or 'acp').lower()
    engine = _engines.get(key)
    if engine is None:
        log.warning('未知任务引擎 %s，回退 acp', key)
        engine = _engines.get('acp')
    return engine