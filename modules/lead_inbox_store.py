# -*- coding: utf-8 -*-
"""主管收件箱的 JSONL 存储核心（2026-11-15 批次 A 二次返工从 leads_inbox
拆出腾 400 行预算）：每主管一个只追加文件 + 超限截头的原子重写。

leads_inbox 留触发点 / sweeper / 端点（编排面）；这里的函数只认文件——
inboxPath 按 config.statePath 定址，不碰 Flask、不碰锁（append 与截头
重写的互斥在 leads_inbox 的 _LOCK + state.fileLock 里）。
"""
import json
import os
import tempfile

import config


def inboxPath(leadId):
    return config.statePath('lead_inbox/%s.jsonl' % str(leadId or ''))


def readAll(leadId):
    """整文件读入（小）；坏行跳过（截头重写崩溃的残行不堵死读取）。"""
    try:
        with open(inboxPath(leadId), encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get('seq'):
            out.append(obj)
    return out


def writeAll(path, entries):
    """原子重写（临时文件 + os.replace）：截头不留半截文件。"""
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix='.tmp-', suffix='.jsonl')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)   # replace 成功时 tmp 已不在；清失败不掩蔽原异常
            except OSError:
                pass


def trimToMax(leadId, maxEntries):
    """把超限文件截回保留窗口（原子重写）：返回截掉几条。

    只修违背「≤maxEntries」不变式的状态（直接播种/损坏）——正常 append
    路径本就不会超限。N1：历史补标前调用，避免 CAP 逐出（按 at）与截头
    （按 seq）错位，留下"条目在盘上却没有落定标记"的一次性幽灵提醒。
    """
    path = inboxPath(leadId)
    entries = readAll(leadId)
    if len(entries) <= maxEntries:
        return 0
    writeAll(path, entries[-maxEntries:])
    return len(entries) - maxEntries
