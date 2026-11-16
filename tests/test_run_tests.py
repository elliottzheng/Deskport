# -*- coding: utf-8 -*-
"""tools/run_tests.py 的并发安全（批次 V 返工）：多个执行方同时跑互不干扰。

- 两个 run_tests.py 同时跑（各选两个小文件）：都退出 0、都 ALL PASS、
  两者用不同的运行根目录、跑完各自把自己那份删掉；
- 陈旧运行根回收：超过 2 小时且对应 pid 已不存在的旧根被回收，年轻的不动。
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / 'tools' / 'run_tests.py'
BASE = Path(tempfile.gettempdir()) / 'wp-test-shards'
SMALL = ['test_pure.py', 'test_fsutil.py']


def _load_runner_module():
    spec = importlib.util.spec_from_file_location('run_tests_under_test',
                                                  str(RUNNER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid():
    """起一个必然退出的进程拿它的 pid（退出后极短时间內不会被复用；真复用了跳过）。"""
    p = subprocess.Popen([sys.executable, '-c', 'import sys; sys.exit(0)'])
    p.wait(timeout=30)
    return p.pid


def test_two_runners_concurrently_dont_interfere():
    """两个 run_tests.py 同时跑：都成功、根目录不同、跑完各自清掉自己那份。"""
    procs = [subprocess.Popen([sys.executable, str(RUNNER), '--files'] + SMALL,
                              cwd=str(ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              encoding='utf-8', errors='replace')
             for _ in range(2)]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    codes = [p.returncode for p in procs]
    assert codes == [0, 0], '\n\n'.join(outs)
    assert all('ALL PASS' in o for o in outs), '\n\n'.join(outs)
    roots = []
    for o in outs:
        m = re.search(r'basetemp 根目录: (\S+)', o)
        assert m, o
        roots.append(m.group(1))
    assert roots[0] != roots[1], roots       # 各用各的根，不 FileExistsError
    assert not Path(roots[0]).exists(), roots[0]
    assert not Path(roots[1]).exists(), roots[1]


def test_stale_run_roots_reclaimed():
    """pid 已死且超过 2 小时的旧运行根被回收；不满 2 小时的不能动。"""
    import pytest

    mod = _load_runner_module()
    pid = _dead_pid()
    if mod._pid_alive(pid):
        pytest.skip('pid 已被复用，跳过（回收判定依赖 pid 不存在）')
    old = BASE / ('%d-20260101-000000-000000' % pid)
    young = BASE / ('%d-20260101-000000-000001' % pid)
    (old / 'shard-1').mkdir(parents=True, exist_ok=True)
    young.mkdir(parents=True, exist_ok=True)
    past = time.time() - 3 * 3600
    os.utime(old, (past, past))
    try:
        mod.reclaim_stale_roots(BASE)
        assert not old.exists(), '超过 2 小时且 pid 已不存在的旧根应被回收'
        assert young.exists(), '不满 2 小时的运行根不能动（可能在跑）'
    finally:
        shutil.rmtree(young, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
