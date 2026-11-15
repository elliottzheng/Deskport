# -*- coding: utf-8 -*-
"""批次 L L6：状态目录所有者锁——web 持锁时其它进程只读，锁空闲照常。

真起第二个进程持锁（规格允许的"两个进程"路线）：子进程把 pid 写进
owner.lock 并持 OS 级排它锁睡到被终结；本进程据此判只读——读放行，
save/append 一律抛 StateDirReadOnly（消息带占用者 pid）。子进程只按
pid 终结（proc.terminate），不按名字批量杀。
"""
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import feedback_store, leads_inbox, leads_store, state_owner
from modules import worklines_store
from modules.tasks_store import TaskRepository

# 持锁子进程：写 pid → 锁 byte 64（与 state_owner.LOCK_OFFSET 同一处）→
# 睡到被 kill（机制同 state_owner._tryLock）
_HOLDER = (
    "import msvcrt, os, sys, time\n"
    "path = sys.argv[1]\n"
    "f = open(path, 'a+b')\n"
    "f.seek(0); f.truncate(); f.write(str(os.getpid()).encode()); f.flush()\n"
    "f.seek(64)\n"
    "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
    "print(os.getpid(), flush=True)\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """状态目录指到 tmp；每个用例重新判定身份（_mode 重置）。"""

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(state_owner, '_mode', '')
    return SimpleNamespace(tmp=tmp_path)


def _spawnHolder(env):
    """起一个持锁子进程，返回 (proc, pid)；等它真把锁戴上。"""
    path = configMod.statePath(state_owner.LOCK_NAME)
    proc = subprocess.Popen([sys.executable, '-c', _HOLDER, path],
                            stdout=subprocess.PIPE, text=True)
    pid = int(proc.stdout.readline().strip())
    deadline = time.time() + 10
    while time.time() < deadline:
        if state_owner._tryLock(path) is None:
            return proc, pid       # 探测失败 = 锁已被它戴上
        time.sleep(0.05)
    proc.terminate()
    raise AssertionError('holder did not take the lock')


def test_write_rejected_when_other_process_owns_state_dir(env):
    """锁被其它存活进程持有：读放行，save/append 一律明确异常（带占用者 pid）。"""
    proc, pid = _spawnHolder(env)
    try:
        repo = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
        repo.put({'id': 't1', 'title': 'x', 'prompt': 'p', 'status': 'waiting',
                  'events': [], 'seq': 1, 'workspace': str(env.tmp / 'ws'),
                  'createdAt': time.time(), 'lastActiveAt': time.time()})
        repo.load()                       # 读放行（不抛）
        assert state_owner.adopt() == 'reader'
        with pytest.raises(state_owner.StateDirReadOnly) as err:
            repo.save()
        assert str(pid) in str(err.value) and '只读' in str(err.value)
        with pytest.raises(state_owner.StateDirReadOnly):
            repo.log_write(repo.get('t1'), {'at': 1, 'type': 'status'})
        # leads / feedback / worklines / inbox 同一道闸（规格「等存储」）
        with pytest.raises(state_owner.StateDirReadOnly):
            leads_store.create('甲')
        with pytest.raises(state_owner.StateDirReadOnly):
            leads_inbox.append('ld_x', 'up', 'report', 'x')
        with pytest.raises(state_owner.StateDirReadOnly):
            feedback_store._save({'schema_version': '1.0', 'items': []})
        with pytest.raises(state_owner.StateDirReadOnly):
            worklines_store.WorklineRepository(str(env.tmp / 'wl.json'),
                                               str(env.tmp / 'docs')).save()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_reads_and_writes_work_when_lock_free(env):
    """没有 web 持锁（单进程脚本/测试现状）：探测即放开，读写照常。"""
    assert state_owner.adopt() == 'open' and state_owner.writable()
    repo = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
    repo.put({'id': 't1', 'title': 'x', 'prompt': 'p', 'status': 'waiting',
              'events': [], 'seq': 1, 'workspace': str(env.tmp / 'ws'),
              'createdAt': time.time(), 'lastActiveAt': time.time()})
    repo.load()
    repo.save()                           # 不抛
    assert leads_store.markIdleNoticed('ld_x') == (None, leads_store.NOT_FOUND)
    assert leads_inbox.append('ld_x', 'up', 'report', 'x') == 1


def test_owner_released_when_process_dies(env):
    """旧进程退出即释放锁（watchdog 重启场景）：第二个进程接着就能写。"""
    proc, _pid = _spawnHolder(env)
    try:
        repo = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
        repo.put({'id': 't1', 'title': 'x', 'prompt': 'p', 'status': 'waiting',
                  'events': [], 'seq': 1, 'workspace': str(env.tmp / 'ws'),
                  'createdAt': time.time(), 'lastActiveAt': time.time()})
        repo.load()
        with pytest.raises(state_owner.StateDirReadOnly):
            repo.save()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    time.sleep(0.2)                       # 等 OS 收锁
    state_owner._mode = ''                # 新进程重新判定
    repo2 = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
    repo2.load()
    assert state_owner.adopt() == 'open'
    repo2.save()                          # 锁没了：照常写


def test_acquire_is_owner_and_idempotent(env):
    """acquire：取到即 owner（幂等）；锁文件里记着本进程 pid。"""
    try:
        state_owner.acquire()
        assert state_owner.adopt() == 'owner' and state_owner.writable()
        state_owner.acquire()                 # 第二次调用不抛、不换锁
        with open(configMod.statePath(state_owner.LOCK_NAME),
                  encoding='utf-8') as f:
            assert int(f.read().strip()) == __import__('os').getpid()
    finally:
        # 持锁句柄不能漏到 tmp 目录清理之外（生产里它活到进程结束）
        if state_owner._ownerHandle is not None:
            state_owner._ownerHandle.close()
            state_owner._ownerHandle = None
            state_owner._mode = ''
