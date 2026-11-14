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


def saveSnapshot(path, payload, seq, savedSeq):
    """把已序列化的 payload 原子写到 path；返回更新后的 savedSeq。

    三层竞争分别堵上：独立临时文件（不互替换掉对方的 .tmp）、全进程串行
    写盘（并发 os.replace 同一目标必撞 WinError 5/32）、序号门（旧快照不
    覆写已落盘的更新快照——savedSeq 由调用方持有并传回）。
    """
    with _IO_LOCK:
        if seq <= savedSeq:
            return savedSeq
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                       prefix='.tasks-', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(payload)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning('save tasks failed: %s', e)
            return savedSeq
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
