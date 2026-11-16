# -*- coding: utf-8 -*-
"""任务事件内核：文本截断单一来源 + jsonl 流水尾部读取 + 快照剥离。

批 PS1 起 agent_logs/<id>.jsonl 是事件的唯一真相，agent_tasks.json 只存
任务元数据，内存窗口只留最近 MAX_EVENTS 条。写入端（tasks_runner）与重启
回填（tasks_store）必须走同一套截断规则，否则"重启后 /events 返回的旧事件"
与"重启前同一事件"字节不同——对外是契约漂移，故规则收敛在本模块。

流水里还可能出现**不带 seq** 的 tool_args_patch 补丁行（流式参数后到时
_backfillToolArgs 追加的回填记录）：它不占事件序号、本身不是事件，回填与
/event/<seq> 按 targetSeq 把它应用到对应 tool 事件上（最后一次为准）——
否则重启后任务卡上的工具调用看不到命令（主控 2026-11-15 决定不接受丢失）。

批 PS2 起追加流水走 JsonlWriter（每任务长开句柄 + 锁内写 + flush）：每条
open+write+close 约 3ms，8 任务 ×154/s 的叠加峰值下会排队（方案 §6 风险
6），长开句柄把单条降到 ~0.1ms；句柄在任务终态/记录删除时由仓库关掉，
进程退出时全部关掉（Windows 上句柄不关，日志文件不能改名/删除）。

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


def _parsePatch(raw):
    """一行 jsonl → tool_args_patch 补丁 dict；不是补丁行/坏行返回 None。

    补丁行由 _backfillToolArgs 追加（流式参数后到的回填记录），**不带 seq**
    ——不占事件序号：seq 校正、/event/<seq> 的按 seq 扫描都按无 seq 行跳过。
    """
    try:
        obj = json.loads(raw.strip().decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get('type') != 'tool_args_patch':
        return None
    return obj


def readLogTailWithPatches(path, want):
    """读 jsonl 尾部：返回 (事件列表, 补丁列表)，供回填路径分流。

    事件同 readLogTail（坏行/chunk 跳过）；补丁行按文件顺序（旧→新）收集，
    交给 applyArgPatches 应用。补丁总在目标事件之后落盘，倒序扫描先遇到补丁、
    后遇到目标——目标在窗口内则其补丁必然也已被扫到；目标被 MAX_EVENTS 挤出
    时补丁自然落空。只从文件尾部倒序抓够 want 条即停，不回放全文件。
    """
    events = []
    patches = []
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
                    elif b'tool_args_patch' in raw:
                        # 廉价预筛（批 PS2 建议）：事件行已被 _parseLine 收走，
                        # 走到这里的只有 chunk/坏行/补丁行——不含该子串的连
                        # json.loads 都不必做（事件正文里提到这个词也安全：
                        # 它先过了 _parseLine 那一关，到不了这里）
                        patch = _parsePatch(raw)
                        if patch is not None:
                            patches.append(patch)
            if pos == 0 and len(events) < want:
                obj = _parseLine(partial)
                if obj is not None:
                    events.append(obj)
                elif b'tool_args_patch' in partial:
                    patch = _parsePatch(partial)
                    if patch is not None:
                        patches.append(patch)
    except OSError:
        return [], []
    events.reverse()
    patches.reverse()
    return events, patches


def readLogTail(path, want):
    """读 jsonl 尾部最后 want 条非 chunk 事件（坏行跳过，与读取端容错一致）。

    只从文件尾部倒序抓够 want 条即停，不回放全文件；开头的半行按坏行跳过。
    补丁行不进事件列表（要补丁走 readLogTailWithPatches）。
    """
    events, _patches = readLogTailWithPatches(path, want)
    return events


def _applyPatch(event, patch):
    """把一条 tool_args_patch 补丁应用到事件上（批 PS2 建议：只留一份）。

    回填（applyArgPatches）与 /event/<seq>（eventBySeq）过去各写一遍
    「args/truncated 有就覆盖」，合并到这一处；同一 targetSeq 多次补丁以
    最后一次为准由调用方保证（先筛出 last 再应用）。
    """
    if 'args' in patch:
        event['args'] = patch['args']
    if 'truncated' in patch:
        event['truncated'] = patch['truncated']
    return event


def applyArgPatches(events, patches):
    """把 jsonl 补丁行应用到回填事件上（同一 targetSeq 多次补丁以最后一次为准）。

    补丁行不进窗口；targetSeq 不在窗口里（被 MAX_EVENTS 挤出）时自然落空。
    args 以 jsonl 补丁行为准：真实 Kimi 的流式形状（先 tool_call 后参数）下，
    补丁是重启后唯一还能看到命令的地方（_backfillToolArgs 只改内存不够）。
    """
    if not patches:
        return events
    last = {}
    for patch in patches:
        target = patch.get('targetSeq')
        if target is not None:
            last[target] = patch
    for event in events:
        patch = last.get(event.get('seq'))
        if patch is not None:
            _applyPatch(event, patch)
    return events


def eventBySeq(path, seq):
    """按 seq 从 jsonl 取单条事件（/api/tasks/<id>/event/<seq> 用）。

    与旧的内联扫描行为一致：坏行跳过、assistant_chunk 不返回、首个命中即取。
    补丁行（tool_args_patch，不带 seq）不是事件、永不当事件返回，但会按
    targetSeq 应用到命中的 tool 事件上（同目标多次补丁以最后一次为准）——
    流式参数后到的 args 经 /event/<seq> 重启后也看得见。文件缺失/读失败/
    seq 不存在返回 None（调用方回落内存窗口）。
    """
    target = None
    patch = None
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if target is not None and 'tool_args_patch' not in line:
                    continue   # 目标已到手：只剩它的补丁行还要看（预筛省解析）
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get('type') == 'tool_args_patch':
                    if obj.get('targetSeq') == seq:
                        patch = obj
                    continue
                if target is None and obj.get('seq') == seq \
                        and obj.get('type') != 'assistant_chunk':
                    target = obj
                    if obj.get('type') != 'tool':
                        break      # 补丁只落 tool 事件，其余提前收手
    except OSError:
        return None
    if target is None:
        return None
    if patch is not None:
        target = _applyPatch(dict(target), patch)
    return target


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
    不落盘。旧格式快照（events 已在字典里）只做 seq 校正，不重建窗口。

    补丁行（tool_args_patch）在回填时按 targetSeq 应用到对应事件上（最后一次
    为准），本身不进窗口——流式参数后到的 tool.args 重启后不丢；seq 校正只数
    事件行（补丁不带 seq），不受影响。
    """
    if not isinstance(task, dict) or task.get('_eventsHydrated'):
        return
    task['_eventsHydrated'] = True
    tail, patches = readLogTailWithPatches(logPath, MAX_EVENTS)
    if 'events' not in task:
        task['events'] = applyArgPatches([eventFromLog(e) for e in tail],
                                         patches)
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


class JsonlWriter:
    """每任务长开句柄 + 锁内写 + flush（批 PS2，方案 §6 风险 6）。

    每条事件 open+write+close 约 3ms，8 任务 ×154/s 的叠加峰值（1232/s）
    下会积 ~3.7s/s；长开句柄把单条降到 ~0.1ms。句柄按任务 id 缓存：
    写在锁内完成（取句柄→缺则开→write→flush），失败作废句柄重试，
    重试后仍败沿用第 1 批语义：log.error + 进程内计数，事件留在内存窗口。
    句柄关闭：任务终态/记录被删时由仓库在 save/flush 里扫到就关，进程退出
    closeAll 全关——Windows 上句柄不关，日志文件不能改名/删除。
    """

    def __init__(self, logDir):
        self._logDir = logDir
        self._handles = {}
        self._lock = threading.Lock()

    def append(self, taskId, line):
        """追加一行（调用方负责带上换行）；落盘返回 True，重试后仍败 False。"""
        for attempt in range(4):
            try:
                with self._lock:
                    f = self._handles.get(taskId)
                    if f is None:
                        os.makedirs(self._logDir, exist_ok=True)
                        f = open(os.path.join(self._logDir, taskId + '.jsonl'),
                                 'a', encoding='utf-8')
                        self._handles[taskId] = f
                    f.write(line)
                    f.flush()
                return True
            except OSError as e:
                with self._lock:
                    self._drop(taskId)
                if attempt == 3:
                    log.error('append task log failed: %s', e)
                    countLogWriteFailure()
                    return False
                time.sleep(0.05 * 2 ** attempt)
        return False

    def openTaskIds(self):
        """当前开着句柄的任务 id（仓库据此扫终态/已删任务）。"""
        with self._lock:
            return list(self._handles)

    def close(self, taskId):
        with self._lock:
            self._drop(taskId)

    def closeAll(self):
        with self._lock:
            for taskId in list(self._handles):
                self._drop(taskId)

    def _drop(self, taskId):
        f = self._handles.pop(taskId, None)
        if f is None:
            return
        try:
            f.close()
        except OSError:
            pass


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
