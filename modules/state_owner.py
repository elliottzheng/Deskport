# -*- coding: utf-8 -*-
"""状态目录所有者锁（批次 L L6）：web 启动即取，其它进程只读。

背景（规格 .drive/批次L-主管兜底.md L6）：探针脚本直读直写线上状态目录，
根因是"只靠规格里的文字约束"。这里把约束变成机制：web 进程启动时在状态
目录取得独占所有者锁（owner.lock 锁文件 + 进程 id）；其它进程加载
TaskRepository / feedback / leads 等存储时，若发现锁被其它存活进程持有，
进入只读模式——允许读，任何 save/append 抛 StateDirReadOnly 并记日志。

锁的存活判定用 OS 级字节区排它锁（msvcrt，先例见 state._FileLock）：进程
退出（含被 kill）即释放，所以 watchdog 重启时旧进程一走锁就没了，不会留
删不掉的文件锁；锁文件里的 pid 只用于错误消息与人工排查。

本进程身份三态（进程内一次性判定，_mode 粘住）：
    owner   web 本尊：acquire() 成功，持锁到进程结束；
    open    没取过锁、探测时锁空闲：照常读写（单进程脚本/测试的现状）；
    reader  探测时锁被其它进程持有：只读，写操作抛异常。

线上启动顺序不变：watchdog 重启时旧 web 先退出（端口独占同样要求），锁
随之释放，新 web 的 acquire() 最多等 ACQUIRE_TIMEOUT_SEC 即接过。
"""
import logging
import msvcrt
import os
import threading
import time

import config

log = logging.getLogger('state-owner')
LOCK_NAME = 'owner.lock'
LOCK_OFFSET = 64                # 锁 byte 64：pid 文本留在文件头（0 起）可读
                                # ——msvcrt 是强制锁，锁住的字节段连读都挡，
                                # 锁偏移必须避开 pid 文本，否则错误消息拿不到
                                # 「属于进程 <pid>」里的 pid
ACQUIRE_TIMEOUT_SEC = 15       # web 启动时等旧进程退出的上限（watchdog 重启场景）
ACQUIRE_RETRY_SEC = 0.5

_lock = threading.Lock()
_mode = ''                      # '' = 未判定；owner / open / reader
_ownerHandle = None             # owner 态：持锁的文件句柄（进程内不关）
_ownerPid = 0                   # reader 态：占用者的 pid（错误消息用）


class StateDirReadOnly(RuntimeError):
    """状态目录属于别的进程：只读，写被拒。"""

    def __init__(self, stateDir, pid):
        self.stateDir = stateDir
        self.pid = pid
        super().__init__('状态目录 %s 属于进程 %s，本进程只读' % (stateDir, pid))


def lockPath():
    return config.statePath(LOCK_NAME)


def _readPid(path):
    try:
        with open(path, encoding='utf-8') as f:
            return int((f.read() or '0').strip() or 0)
    except (OSError, ValueError):
        return 0


def _writePid(handle, pid):
    handle.seek(0)
    handle.truncate()
    handle.write(str(pid).encode('ascii'))   # 'a+b' 二进制：pid 走 bytes
    handle.flush()
    os.fsync(handle.fileno())


def _tryLock(path):
    """尝试拿 LOCK_OFFSET 处 1 字节的排它锁；成功返回持锁句柄，失败返回 None。

    与 state._FileLock 同一套机制（msvcrt 字节区锁）：同机同文件第二把锁
    必然失败，进程死亡则锁自动释放—— stale 文件锁在这台机器上不存在。
    """
    try:
        handle = open(path, 'a+b')
    except OSError:
        return None
    try:
        handle.seek(LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        return None


def acquire():
    """web 启动时取所有者锁（幂等；已被本进程取过直接返回）。

    拿不到（旧 web 还没退出）时按 ACQUIRE_RETRY_SEC 重试至
    ACQUIRE_TIMEOUT_SEC——watchdog 重启时旧进程退出即释放锁，等一等就能
    接过；超时仍被占说明真有第二个 web 在跑（端口独占同样会拦），直接报错
    退出，不静默双写。
    """
    global _mode, _ownerHandle, _ownerPid
    with _lock:
        if _mode == 'owner':
            return
        path = lockPath()
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        deadline = time.monotonic() + ACQUIRE_TIMEOUT_SEC
        while True:
            handle = _tryLock(path)
            if handle is not None:
                _writePid(handle, os.getpid())
                _ownerHandle = handle      # 持锁到进程结束（句柄不关）
                _ownerPid = os.getpid()
                _mode = 'owner'
                log.info('state dir owner lock acquired pid=%s path=%s',
                         os.getpid(), path)
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(ACQUIRE_RETRY_SEC)
        raise RuntimeError('状态目录被进程 %d 持有所有者锁（%s）：本次启动'
                           '退出，请先停掉旧进程'
                           % (_readPid(path) or -1, path))


def adopt():
    """非 web 进程首次碰状态存储时判定身份（幂等，_mode 粘住）。"""
    global _mode, _ownerPid
    if _mode:
        return _mode
    with _lock:
        if _mode:
            return _mode
        path = lockPath()
        handle = _tryLock(path)
        if handle is not None:
            handle.close()      # 只是探测：不占锁，照常读写
            _mode = 'open'
            return _mode
        _ownerPid = _readPid(path)
        _mode = 'reader'
        log.warning('state dir %s 属于进程 %s：本进程只读',
                    os.path.dirname(path), _ownerPid)
        return _mode


def writable():
    """本进程能不能写状态目录（owner/open 能，reader 不能）。"""
    return adopt() != 'reader'


def ensureWritable(what):
    """写操作前的闸门（L6）：只读进程一律抛 StateDirReadOnly。

    what = 被拒的写入目标（文件/存储名），只进日志，消息保持规格原文。
    """
    if adopt() == 'reader':
        log.warning('write rejected in read-only mode: %s', what)
        raise StateDirReadOnly(os.path.dirname(lockPath()), _ownerPid or '?')
