# -*- coding: utf-8 -*-
"""任务事件内核：文本截断单一来源 + jsonl 流水尾部读取 + 快照剥离。

批 PS1 起 agent_logs/<id>.jsonl 是事件的唯一真相，agent_tasks.json 只存
任务元数据，内存窗口只留最近 MAX_EVENTS 条。写入端（tasks_runner）与重启
回填（tasks_store）必须走同一套截断规则，否则"重启后 /events 返回的旧事件"
与"重启前同一事件"字节不同——对外是契约漂移，故规则收敛在本模块。

本模块是共享内核：不 import flask / 不 import 特性层，只做纯字典与文件。
"""
import datetime
import json
import logging
import os
import shutil
import threading
import time

log = logging.getLogger('tasks')

MAX_EVENTS = 2000        # 内存事件窗口（只数非 chunk 事件）
MAX_TEXT = 8000          # assistant / tool args / plan 的落卡上限
MAX_TOOL_RESULT = 4000   # tool_result 的落卡上限

# 类型 → (字段, 上限, 是否落 truncated 标志)。写入端按本表截断，jsonl 回填
# 按本表还原，两边逐字段一致；user/status 等不在表里的类型不截（全文落卡）。
_TRUNC_RULES = {
    'assistant': ('text', MAX_TEXT, True),
    'tool': ('args', MAX_TEXT, True),
    'tool_result': ('text', MAX_TOOL_RESULT, True),
    'plan': ('text', MAX_TEXT, False),
}

_logWriteFailures = 0               # 进程内 jsonl 写失败计数（/api/diagnostics）
_logWriteFailLock = threading.Lock()


def truncateEvent(etype, text):
    """事件文本截断（单一来源）：返回 (落卡值, truncated 标志)。

    标志为 None 的类型（plan）不落 truncated 键；不在表里的类型原样返回。
    写入端与回填都调它，保证重启前后同一事件逐字段相等。
    """
    rule = _TRUNC_RULES.get(etype)
    if rule is None or not isinstance(text, str):
        return text, None
    return text[:rule[1]], (len(text) > rule[1]) if rule[2] else None


def eventFromLog(entry):
    """jsonl 行 → 内存窗口事件形状（与写入端 append_event 落卡一致）。

    jsonl 里带 full 的事件（assistant/tool/tool_result）的 text 键存全文，
    按写入端同一条规则截回去；tool 的落卡字段是 args，写入端把 full 放在
    text 载体键里，截断与标志都按 full 算。truncated 键的有无以 jsonl 行为准
    （调用方没传该kwarg 时内存事件也没有这个键）。坏行由 readLogTail 先过滤。
    """
    event = dict(entry)
    rule = _TRUNC_RULES.get(event.get('type'))
    if rule is None:
        return event
    field, _limit, _withFlag = rule
    hasFlag = 'truncated' in event
    if field == 'args' and 'text' in event:
        event[field] = event.pop('text')   # full 载体键：tool 按 full 截
    if field in event:
        value, flag = truncateEvent(event.get('type'), event[field])
        event[field] = value
        if hasFlag and flag is not None:
            event['truncated'] = flag
        elif 'truncated' in event:
            event.pop('truncated', None)
    return event


def _parseLine(raw):
    """一行 jsonl → 事件 dict；坏行/半行/chunk 行/缺 seq 行返回 None。"""
    try:
        obj = json.loads(raw.strip().decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get('type') == 'assistant_chunk' \
            or obj.get('seq') is None:
        return None
    return obj


def readLogTail(path, want):
    """读 jsonl 尾部最后 want 条非 chunk 事件（坏行跳过，与读取端容错一致）。

    只从文件尾部倒序抓够 want 条即停，不回放全文件；开头的半行按坏行跳过。
    """
    events = []
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            block = 64 * 1024
            partial = b''
            while pos > 0 and len(events) < want:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                lines = (f.read(step) + partial).split(b'\n')
                partial = lines.pop(0)   # 最前面可能还是半行，留给下一块拼
                for raw in reversed(lines):
                    obj = _parseLine(raw)
                    if obj is not None:
                        events.append(obj)
                        if len(events) >= want:
                            break
            if pos == 0 and len(events) < want:
                obj = _parseLine(partial)
                if obj is not None:
                    events.append(obj)
    except OSError:
        return []
    events.reverse()
    return events


def _seqOf(value):
    """seq 兜底解析：脏值（手改过的状态文件）按 0 算，不把访问路径打挂。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def hydrateEvents(task, logPath):
    """惰性回填（批 PS1）：快照没有 events 的任务，首次被访问时从 jsonl 尾部
    补回最近 MAX_EVENTS 条；seq 与 jsonl 最大 seq 取大校正（覆盖"jsonl 写了、
    快照没写"的崩溃中间态，新事件 seq 不重复）。_eventsHydrated 标记只放内存，
    不落盘。旧格式快照（events 已在字典里）只做 seq 校正，不重建窗口。"""
    if not isinstance(task, dict) or task.get('_eventsHydrated'):
        return
    task['_eventsHydrated'] = True
    tail = readLogTail(logPath, MAX_EVENTS)
    if 'events' not in task:
        task['events'] = [eventFromLog(e) for e in tail]
    maxSeq = max((_seqOf(e.get('seq')) for e in tail), default=0)
    if maxSeq > _seqOf(task.get('seq')):
        task['seq'] = maxSeq


def metaSnapshot(tasks):
    """快照只存元数据（批 PS1）：剥掉每个任务的 events 键与所有 _ 开头运行时键。

    只影响落盘，不动内存；任务卡建时本就没有 _ 键，规则安全。
    """
    return {tid: ({k: v for k, v in t.items()
                   if k != 'events' and not k.startswith('_')}
                  if isinstance(t, dict) else t)
            for tid, t in tasks.items()}


def snapshotPayload(tasks, keepLegacyShape=False):
    """序列化快照；keepLegacyShape=True 时按旧格式整包写（备份失败时的保守回退）。"""
    return json.dumps(tasks if keepLegacyShape else metaSnapshot(tasks),
                      ensure_ascii=False)


def backupLegacyStore(storePath):
    """旧格式快照留原件（批 PS1 主控追加）：复制为 <store>.pre-migration-<时间戳>.bak。

    已存在同名则不覆盖；复制失败返回 False——调用方据此按旧格式继续写，
    宁可不瘦身也不丢原件。返回 True 表示已留底（含同名已存在）。
    """
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = '%s.pre-migration-%s.bak' % (storePath, stamp)
    if os.path.exists(backup):
        return True
    try:
        shutil.copy2(storePath, backup)
    except OSError as e:
        log.error('backup legacy task store failed: %s', e)
        return False
    log.info('legacy task store backed up to %s', backup)
    return True


def appendJsonl(logDir, path, line):
    """追加一行到 jsonl；失败重试 3 次（间隔 50/100/200ms）。

    仍失败：log.error + 进程内计数（logWriteFailures），返回 False——事件仍
    留在内存窗口（本进程可见），调用方不抛异常。批 PS1 起 jsonl 是事件唯一
    真相，写失败不能再静默（旧行为只 warning，因为快照里还有一份）。
    """
    for attempt in range(4):
        try:
            os.makedirs(logDir, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line)
            return True
        except OSError as e:
            if attempt == 3:
                log.error('append task log failed: %s', e)
                countLogWriteFailure()
                return False
            time.sleep(0.05 * 2 ** attempt)
    return False


def countLogWriteFailure():
    """jsonl 写失败计一次（重试后仍败的那次）。"""
    global _logWriteFailures
    with _logWriteFailLock:
        _logWriteFailures += 1


def logWriteFailures():
    """进程内 jsonl 写失败总数（GET /api/diagnostics 的 taskLogWriteFailures）。"""
    with _logWriteFailLock:
        return _logWriteFailures


class HydratingMap(dict):
    """tasks 容器：直接按下标或 .get 取值也触发事件回填。

    惰性回填的触发面（批 PS1）：除 get()/list() 外，存量代码与测试里直接摸
    repo.tasks[<id>] 的路径也要拿到回填后的窗口，否则重启后那些路径看到的事件
    是空的。写入（put/update/迭代）不触发——load/save 故意不回调填。
    """

    def __init__(self, hydrate):
        super().__init__()
        self._hydrate = hydrate

    def __getitem__(self, key):
        task = super().__getitem__(key)
        self._hydrate(task)
        return task

    def get(self, key, default=None):
        task = super().get(key, default)
        if task is not default:
            self._hydrate(task)
        return task
