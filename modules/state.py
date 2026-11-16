# -*- coding: utf-8 -*-
"""状态文件统一读写 — 原子写 + 跨进程追加锁。

web / locald / sshd 都会碰状态目录, 统一走这里:
    - atomicWriteJson: 临时文件 + os.replace, 写一半崩溃不会留半截文件
    - appendJsonl:     独立 .lock 文件做跨进程排它, 审计/事件不会交错
    - replaceWithRetry: 原子写的共用落盘步, 见下（批 PS2 追件）
"""
import json
import msvcrt
import os
import tempfile
import threading
import time

_threadLock = threading.Lock()

# 批 PS2 追件：os.replace 的退避重试（秒）。Windows 上杀毒软件/索引服务/
# 并发读者会短暂占用目标文件，replace 报 PermissionError（WinError 5/32）
# ——线上 160 次 save tasks failed 与全量里偶发的共享拒绝都是这个根因，而
# 内容早已在临时文件里写好，重试一次往往就成。atomicWriteJson 与 store_io
# 的 saveSnapshot 共用这一份，不各写一份
REPLACE_BACKOFF_SEC = (0.02, 0.04, 0.08, 0.16)


def _ensureDir(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def replaceWithRetry(tmp, path):
    """os.replace 带退避重试（批 PS2 追件）：PermissionError 不直接放弃。

    Windows 上目标文件被杀毒/索引/并发读者短暂占用时 replace 报 WinError
    5/32（PermissionError）；临时文件里内容已写好，重试即可。按
    20/40/80/160ms 退避重试 4 次，仍败把最后一次的异常抛给调用方（记失败/
    清临时文件），不吞异常。
    """
    for wait in REPLACE_BACKOFF_SEC:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(wait)
    os.replace(tmp, path)      # 最后一次：成败都交给调用方处理


def atomicWriteJson(path, data):
    _ensureDir(path)
    parent = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=parent, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        replaceWithRetry(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _FileLock:
    def __init__(self, path):
        self.path = path + '.lock'
        self.handle = None

    def __enter__(self):
        _ensureDir(self.path)
        self.handle = open(self.path, 'a+b')
        deadline = time.time() + 2.0
        while True:
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError('state lock timeout: %s' % self.path) from None
                time.sleep(0.02)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.handle:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                if self.handle:
                    self.handle.close()
            except OSError:
                pass


def fileLock(path):
    """跨进程排它锁（appendJsonl 内部用的就是它）。

    「读-改-写」要横跨多次文件操作的调用方（如主管收件箱：append 与超上限
    截头重写必须放在同一把锁里，否则并发写在重写窗口里丢条目）走这个公开
    入口，别复制一份 msvcrt 逻辑。
    """
    return _FileLock(path)


def appendJsonl(path, obj):
    _ensureDir(path)
    line = json.dumps(obj, ensure_ascii=False)
    with _threadLock:
        with _FileLock(path):
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')


def readJson(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default