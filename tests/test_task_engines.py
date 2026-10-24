# -*- coding: utf-8 -*-
"""任务引擎策略：注册表与线程适配器。"""
import threading

from modules import task_engines
from modules import tasks_runner  # noqa: F401  注册 acp/p 引擎


def test_register_and_get():
    class Fake:
        def start(self, task, manifest):
            pass

    fake = Fake()
    task_engines.register('test-engine', fake)
    assert task_engines.get('test-engine') is fake
    assert task_engines.get('TEST-ENGINE') is fake
    # 未知引擎回退 acp（runner 导入后已注册）
    assert task_engines.get('no-such-engine') is not None


def test_thread_engine_passes_task_and_manifest():
    seen = []
    done = threading.Event()

    def target(task, manifest):
        seen.append((task, manifest))
        done.set()

    engine = task_engines.ThreadEngine(target)
    engine.start({'id': 't'}, [{'input': 'x'}])
    assert done.wait(2)
    assert seen[0][0]['id'] == 't'
    assert seen[0][1][0]['input'] == 'x'