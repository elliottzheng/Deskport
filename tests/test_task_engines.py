# -*- coding: utf-8 -*-
"""执行点分派表：按节点类型（local / agent / federation）注册与线程适配。"""
import threading

from modules import task_engines
from modules import agent_nodes  # noqa: F401  注册 local/agent/federation 执行点


def test_register_and_get():
    class Fake:
        def start(self, task, manifest):
            pass

    fake = Fake()
    task_engines.register('test-kind', fake)
    assert task_engines.get('test-kind') is fake
    assert task_engines.get('TEST-KIND') is fake
    # 未知类型回退 local（agent_nodes 导入后已注册）
    assert task_engines.get('no-such-kind') is not None
    for kind in task_engines.KINDS:
        assert task_engines.get(kind) is not None


def test_thread_runner_passes_task_and_manifest():
    seen = []
    done = threading.Event()

    def target(task, manifest):
        seen.append((task, manifest))
        done.set()

    runner = task_engines.ThreadRunner(target)
    runner.start({'id': 't'}, [{'input': 'x'}])
    assert done.wait(2)
    assert seen[0][0]['id'] == 't'
    assert seen[0][1][0]['input'] == 'x'