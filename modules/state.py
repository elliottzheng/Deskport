# -*- coding: utf-8 -*-
"""状态文件统一读写 — 原子写 + 跨进程追加锁。

web / locald / sshd 都会碰状态目录, 统一走这里:
    - atomicWriteJson: 临时文件 + os.replace, 写一半崩溃不会留半截文件
    - appendJsonl:     独立 .lock 文件做跨进程排它, 审计/事件不会交错
"""
import json
import msvcrt
import os
import tempfile
import threading
import time

_threadLock = threading.Lock()


def _ensureDir(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def atomicWriteJson(path, data):
    _ensureDir(path)
    parent = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=parent, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
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