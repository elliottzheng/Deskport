# -*- coding: utf-8 -*-
"""序号门回归（验收-R P1/P2）：旧快照不得覆写已落盘的新快照。

P1（abbd2df 实测盘上落成 OLDER-STATE）：tasks_store.save 在锁外读
`_savedSeq` 再传给 store_io.saveSnapshot，比较用的是锁外读到的旧值——旧
序号线程读走值后被调度走、新序号线程写完盘，旧线程拿旧值进门把新快照盖掉，
savedSeq 回退。修后"读 savedSeq→比较→写盘→更新 savedSeq"整段收进
store_io 的 _IO_LOCK（SeqGate），锁外再无 savedSeq 读写。

本文件直穿 store_io / tasks_store，不起服务。探针兼容两种签名：修前
saveSnapshot(path, payload, seq, savedSeq) 收 int 回 int，修后改收 SeqGate
门对象——同一组断言因此修前红（d）/修后绿。
"""
import json
import logging
import os
import threading

from modules import store_io
from modules.tasks_store import TaskRepository


class _Gate:
    """跨版本序号门观察者：修后即 store_io.SeqGate（锁内持有 savedSeq）；
    旧签名没有门对象，用返回值回填，保证同一探针修前/修后都能跑。"""

    def __init__(self):
        self.savedSeq = 0


def _snap(path, payload, seq, gate):
    """跨版本 saveSnapshot：新版传门对象，旧版传 int 并取回更新值。"""
    if hasattr(store_io, 'SeqGate'):
        return store_io.saveSnapshot(path, payload, seq, gate)
    gate.savedSeq = store_io.saveSnapshot(path, payload, seq, gate.savedSeq)
    return gate.savedSeq


def _disk(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _savedSeqOf(repo):
    """跨版本读已落盘序号（仅断言用，线程已 join；生产路径不锁外读它）。"""
    gate = getattr(repo, '_savedGate', None)
    return gate.savedSeq if gate is not None else repo._savedSeq


# ---- a 新先旧后：后到的旧快照必须被门拦下 ----

def test_newer_written_first_older_later_keeps_newer_on_disk(tmp_path):
    path = str(tmp_path / 'tasks.json')
    gate = _Gate()
    assert _snap(path, 'NEWER-STATE', 6, gate) == 6
    assert _snap(path, 'OLDER-STATE', 5, gate) == 6, '旧序号不得推进 savedSeq'
    assert _disk(path) == 'NEWER-STATE', '后到的旧快照不得覆写新快照'
    assert gate.savedSeq == 6, 'savedSeq 不回退'


# ---- b 旧先新后：正常顺序不受影响 ----

def test_older_written_first_newer_later_lands_newer(tmp_path):
    path = str(tmp_path / 'tasks.json')
    gate = _Gate()
    assert _snap(path, 'OLDER-STATE', 5, gate) == 5
    assert _snap(path, 'NEWER-STATE', 6, gate) == 6
    assert _disk(path) == 'NEWER-STATE', '盘上必须是最新快照'
    assert gate.savedSeq == 6


# ---- c 写失败不推进序号，下一次 save 能成功 ----

def test_failed_write_does_not_advance_saved_seq(tmp_path, monkeypatch):
    path = str(tmp_path / 'tasks.json')
    gate = _Gate()

    def _deny(src, dst):
        raise PermissionError(5, '拒绝访问', src)

    monkeypatch.setattr(os, 'replace', _deny)
    assert _snap(path, 'FIRST', 1, gate) == 0, '写失败不得推进 savedSeq'
    monkeypatch.undo()
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith('.tasks-')]
    assert leftovers == [], '写失败不留临时文件: %s' % leftovers
    assert _snap(path, 'SECOND', 2, gate) == 2, '下一次 save 应成功写入'
    assert _disk(path) == 'SECOND'
    assert gate.savedSeq == 2


# ---- d P1 竞态原位：旧序号线程停在窗口里，也盖不掉新快照 ----

def test_interleaved_old_save_cannot_clobber_newer_snapshot(tmp_path,
                                                             monkeypatch):
    """threading.Event 精确控制：旧序号线程在"读到 savedSeq 之后、写盘之前"
    停住（saveSnapshot 包装层——参数求值完毕即旧值已读走），新序号线程完整
    写完，再放行旧线程。盘上必须仍是新快照、savedSeq 不得回退。

    修前（abbd2df）本条红：旧线程拿锁外读到的旧值进门（1 > 0 放过），把新
    序号线程刚落盘的新快照盖成 OLDER-STATE，savedSeq 从 2 回退到 1。
    """
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    real = store_io.saveSnapshot
    oldRead = threading.Event()     # 旧序号线程已读走（旧）savedSeq、未写盘
    newDone = threading.Event()     # 新序号线程已完整写完

    def wrapped(path, payload, seq, arg):
        if 'OLDER-STATE' in payload:
            oldRead.set()
            assert newDone.wait(10), '新序号线程应在此期间写完'
        return real(path, payload, seq, arg)

    monkeypatch.setattr(store_io, 'saveSnapshot', wrapped)

    repo.put({'id': 't1', 'v': 'OLDER-STATE'})
    old = threading.Thread(target=repo.save, daemon=True)
    old.start()
    assert oldRead.wait(10), '旧序号线程应停在写盘前窗口'
    repo.put({'id': 't1', 'v': 'NEWER-STATE'})
    new = threading.Thread(target=repo.save, daemon=True)
    new.start()
    new.join(10)
    assert not new.is_alive(), '新序号线程应写完'
    newDone.set()
    old.join(10)
    assert not old.is_alive()

    disk = json.loads((tmp_path / 'tasks.json').read_text(encoding='utf-8'))
    assert disk['t1']['v'] == 'NEWER-STATE', \
        '旧序号 save 后到不得覆写已落盘的新快照: %r' % disk['t1']['v']
    assert _savedSeqOf(repo) == 2, 'savedSeq 不得回退: %r' % _savedSeqOf(repo)


# ---- replace 退避重试（批 PS2 追件：WinError 5/32 不是状态损坏） ----

def test_replace_retries_permission_error_then_succeeds(tmp_path, monkeypatch,
                                                         caplog):
    """os.replace 前两次抛 PermissionError（杀毒/索引/并发读者短暂占用目标
    → WinError 5/32）→ 按 20/40ms 退避重试后成功：落盘、推进 savedSeq、
    不记 save tasks failed、临时文件不残留。"""
    caplog.set_level(logging.WARNING, logger='tasks')
    path = str(tmp_path / 'tasks.json')
    real = os.replace
    calls = {'n': 0}

    def flaky(src, dst):
        if str(dst) == path:
            calls['n'] += 1
            if calls['n'] <= 2:
                raise PermissionError('[WinError 5] 拒绝访问。')
        return real(src, dst)

    monkeypatch.setattr(store_io.os, 'replace', flaky)
    gate = store_io.SeqGate()
    assert store_io.saveSnapshot(path, '{"a": 1}', 1, gate) == 1
    assert calls['n'] == 3                 # 两次败 + 第三次成
    assert _disk(path) == '{"a": 1}'
    assert gate.savedSeq == 1
    assert not [r for r in caplog.records if 'save tasks failed' in r.message]
    assert not list(tmp_path.glob('.tasks-*.tmp'))


def test_replace_gives_up_after_four_retries(tmp_path, monkeypatch, caplog):
    """一直 PermissionError → 退避重试 4 次（20/40/80/160ms）后放弃：记一次
    save tasks failed、savedSeq 不推进、临时文件清掉（不泄露 .tmp）。"""
    caplog.set_level(logging.WARNING, logger='tasks')
    path = str(tmp_path / 'tasks.json')
    real = os.replace
    calls = {'n': 0}

    def alwaysDeny(src, dst):
        if str(dst) == path:
            calls['n'] += 1
            raise PermissionError('[WinError 32] 另一个程序正在使用此文件。')
        return real(src, dst)

    monkeypatch.setattr(store_io.os, 'replace', alwaysDeny)
    gate = store_io.SeqGate()
    assert store_io.saveSnapshot(path, '{"a": 1}', 1, gate) == 0
    assert calls['n'] == 5                 # 首发 + 4 次退避重试
    assert gate.savedSeq == 0
    failed = [r for r in caplog.records if 'save tasks failed' in r.message]
    assert len(failed) == 1
    assert not list(tmp_path.glob('.tasks-*.tmp'))
    # 放弃不毒化后续：真实 replace 恢复后同一条 path 照常落盘
    monkeypatch.undo()
    assert store_io.saveSnapshot(path, '{"b": 2}', 2, gate) == 2
    assert _disk(path) == '{"b": 2}'
