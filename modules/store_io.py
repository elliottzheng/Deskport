# -*- coding: utf-8 -*-
"""状态文件原子落盘/容忍读：并发 save 不互撞、瞬时共享冲突不丢状态。

线上 run.log 里 160 次 `save tasks failed: [WinError 5]/[WinError 32]` 与
测试里偶发"重启续接读不到积压队列"的同源根因，都在这两个函数覆盖的面上：
并发 save 共用同一 .tmp 路径会互替换掉对方的临时文件；Windows 上并发
os.replace 同一目标必撞访问拒绝；读盘撞上 replace 的瞬间会被当成"无状态"，
启动时丢全部任务。
"""
import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger('tasks')

# 落盘 I/O 全进程串行：快照捕获仍在调用方状态锁内（保序），这里只串写盘；
# 锁序固定为 调用方锁→_IO_LOCK，无反向等待
_IO_LOCK = threading.Lock()

# 批 PS2 追件：os.replace 的退避重试（秒）。Windows 上杀毒软件/索引服务/
# 并发读者会短暂占用目标文件，replace 报 PermissionError（WinError 5/32）
# ——线上 160 次 save tasks failed 与全量里 test_concurrent_saves 偶发红都
# 是这个根因，而 payload 早已在临时文件里写好，重试一次往往就成
_REPLACE_BACKOFF_SEC = (0.02, 0.04, 0.08, 0.16)


class SeqGate:
    """序号门：已落盘序号。只允许在 _IO_LOCK 内读写——调用方（tasks_store）
    不再锁外摸 savedSeq，否则旧序号 save 拿锁外读到的旧值进门，会把已落盘
    的新快照盖掉（验收-R P1，探针实测盘上落成 OLDER-STATE）。"""

    def __init__(self):
        self.savedSeq = 0


def _replaceWithRetry(tmp, path):
    """os.replace 带退避重试（批 PS2 追件）：PermissionError 不直接放弃。

    Windows 上目标文件被杀毒/索引/并发读者短暂占用时 replace 报 WinError
    5/32（PermissionError）；临时文件里 payload 已写好，重试即可。按
    20/40/80/160ms 退避重试 4 次，仍败把最后一次的异常抛给调用方（记
    save tasks failed、清临时文件），不吞异常也不改序号门语义。
    """
    for wait in _REPLACE_BACKOFF_SEC:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(wait)
    os.replace(tmp, path)      # 最后一次：成败都交给调用方处理


def saveSnapshot(path, payload, seq, gate):
    """把已序列化的 payload 原子写到 path；返回落笔后的 savedSeq。

    三层竞争分别堵上：独立临时文件（不互替换掉对方的 .tmp）、全进程串行
    写盘（并发 os.replace 同一目标必撞 WinError 5/32）、序号门（旧快照不
    覆写已落盘的更新快照）。序号门的读-比-写-更新整段在 _IO_LOCK 内完成，
    比较用锁内读到的 gate.savedSeq，锁外读旧值绕过门的路堵死（P1）。
    批 PS2 追件：replace 遇 PermissionError 按 _REPLACE_BACKOFF_SEC 退避
    重试 4 次（瞬时占用是常态，不是状态损坏）。
    """
    with _IO_LOCK:
        if seq <= gate.savedSeq:
            return gate.savedSeq
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                       prefix='.tasks-', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(payload)
                _replaceWithRetry(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning('save tasks failed: %s', e)
            return gate.savedSeq
        gate.savedSeq = seq
        return seq


def loadSnapshot(path):
    """读状态文件；瞬时共享冲突（并发 save 正在 os.replace，Windows 上表现
    为 Errno 13/WinError 32）重试——把它当"无状态"会在启动时丢全部任务。
    文件真缺失/内容损坏才按空处理。"""
    for attempt in range(6):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError:
            log.warning('load tasks failed: %s 不是合法 JSON', path)
            return {}
        except OSError as e:
            if attempt == 5:
                log.warning('load tasks failed: %s', e)
                return {}
            time.sleep(0.05 * (attempt + 1))
    return {}
