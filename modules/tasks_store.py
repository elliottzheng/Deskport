# -*- coding: utf-8 -*-
"""任务状态层：任务字典、持久化、事件流水、工作区复制与汇总。

不 import Flask / 不启动线程；只做状态与文件系统。

批 PS2（削峰）：append_event 不再每事件 save——seq/lastActiveAt/lastText
等非关键变更只置脏，由 1 秒粒度的后台 flush 线程合并落盘（有变更才写），
进程退出（atexit）做最后一次 flush；关键变迁（status/pendingPermission/
messageQueue/closedByUser/lead/title）仍走 save() 立即落盘。
"""
import atexit
import datetime
import json
import logging
import os
import re
import shutil
import threading
import time
import weakref

import config
from modules import contracts, fsutil, store_io, state_owner, task_state, task_cwd, task_node, task_events, task_title
from modules import sensitive_values
from modules.staging import safeAttachmentName
from modules.task_events import MAX_EVENTS, MAX_TEXT, MAX_TOOL_RESULT  # noqa: F401（store.MAX_* 兼容旧引用）

log = logging.getLogger('tasks')

MAX_FILE_MB = 200
MAX_TASK_MB = 500
MAX_TASK_FILES = 3000
MAX_UPLOAD_MB = 32        # /api/tasks/files 单文件上限（§7b 过渡：智能体没有
                          # exec/sftp 之前的文件投递通道）

FLUSH_INTERVAL_SEC = 1.0  # 批 PS2：非关键元数据的 flush 粒度（每秒一拍）

# 终态任务的 jsonl 句柄可以关（Windows 上句柄不关，日志不能改名/删除）；
# 之后若再追加（续跑 failed/interrupted）会重开，不影响正确性
_JSONL_DONE = ('closed', 'cancelled', 'failed', 'interrupted')


# ---- 批 PS2：1 秒粒度的后台 flush（削峰） ----
# 进程内所有仓库共用一条 flush 线程（弱引用登记，仓库被回收即退出登记）：
# 测试进程会建上百个仓库，每库一条常驻线程会拖住仓库与 jsonl 句柄不释放。
_flushRepos = []            # weakref.ref(TaskRepository)，atexit 退出也冲这张表
_flushLock = threading.Lock()
_flushThread = None


def _flushTick():
    while True:
        time.sleep(FLUSH_INTERVAL_SEC)
        with _flushLock:
            live = [r for r in (ref() for ref in _flushRepos) if r is not None]
            if len(live) != len(_flushRepos):
                _flushRepos[:] = [weakref.ref(r) for r in live]
        for repo in live:
            try:
                repo.flush()
            except Exception:
                log.warning('flush tasks failed', exc_info=True)


def _ensureFlushThread():
    global _flushThread
    if _flushThread is None or not _flushThread.is_alive():
        _flushThread = threading.Thread(target=_flushTick, name='tasks-flush',
                                        daemon=True)
        _flushThread.start()


def _flushAtExit():
    """进程退出（atexit）：冲全部已注册仓库的最后一次 flush + 关 jsonl 句柄
    （批 PS2：不只模块级单例；幂等，已 shutdown 的仓库重复冲是空操作）。"""
    for repo in [r for r in (ref() for ref in _flushRepos) if r is not None]:
        try:
            repo.shutdown()
        except Exception:
            log.warning('flush tasks failed', exc_info=True)


atexit.register(_flushAtExit)


def _saveAttachments(attachments, workspace):
    """把任务请求里的 attachments 写进 inputs/，返回清单（§7b 过渡）。"""
    inputsDir = os.path.join(workspace, 'inputs')
    os.makedirs(inputsDir, exist_ok=True)
    manifest = []
    total = 0
    for item in attachments or []:
        if not isinstance(item, dict):
            raise ValueError('attachments 条目必须是 {name, contentB64} 对象')
        name = safeAttachmentName(item.get('name'))
        if not name:
            raise ValueError('附件名不合法（需纯文件名，≤120 字符）')
        try:
            raw = fsutil.decodeBase64(item.get('contentB64'))
        except ValueError:
            raise ValueError('附件 %s 的 contentB64 不是合法 base64' % name)
        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError('附件 %s 超过 %dMB 上限' % (name, MAX_UPLOAD_MB))
        total += len(raw)
        if total > MAX_TASK_MB * 1024 * 1024:
            raise ValueError('附件总量超过 %dMB' % MAX_TASK_MB)
        target = _uniqueName(inputsDir, name)
        with open(target, 'wb') as f:
            f.write(raw)
        manifest.append({'source': '(智能体上传)',
                         'input': os.path.relpath(target, os.path.dirname(inputsDir)),
                         'size': len(raw)})
    return manifest


def taskRoot():
    return os.path.join(config.getFallbackDir(), '_agent_tasks')


def storePath():
    return config.statePath('agent_tasks.json')


def logDir():
    return os.path.join(os.path.dirname(storePath()), 'agent_logs')

def _now():
    return time.time()


def _iso(ts):
    if not ts:
        return ''
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


def _lastAssistantText(task):
    for e in reversed(task.get('events') or []):
        if e.get('type') == 'assistant':
            return e.get('text') or ''
    return ''


def _detectQuestion(text):
    t = (text or '').rstrip()
    return bool(t) and (t.endswith('?') or t.endswith('？'))


class TaskRepository:
    """任务状态仓储：拥有任务字典、锁、运行中客户端与日志目录。"""

    def __init__(self, store_path, log_dir):
        self.store_path = store_path
        self.log_dir = log_dir
        # 批 PS1：tasks 直接按下标/.get 取值也触发事件回填（见 HydratingMap）
        self.tasks = task_events.HydratingMap(
            lambda task: task_events.hydrateEvents(task, self.log_path(task)))
        self.lock = threading.RLock()
        # 阻塞等待（R4-3）：以 repo.lock 为条件锁——append_event 在锁内
        # notify_all，等待方 wait() 期间整体放锁，追加方照常入队
        self.evCond = threading.Condition(self.lock)
        self.running = {}
        self.agents = {}
        self.loaded = False
        self._saveSeq = 0        # 落盘快照序号（捕获时发号，防旧快照覆新）
        self._savedGate = store_io.SeqGate()   # 已落盘序号（锁内持有，P1）
        self._keepLegacyShape = False   # 旧格式留原件失败：本进程按旧格式写，不剥离
        # 批 PS2：非关键变更只置脏，等 flush 线程 1 秒一拍合并落盘
        self._dirty = False
        self._dirtySeq = 0       # 变更代号：flush 捕获后到达的新变更不丢
        self._jsonl = task_events.JsonlWriter(self.log_dir)
        with _flushLock:
            _flushRepos.append(weakref.ref(self))
        _ensureFlushThread()

    # ---- 公开访问器：上层不再直接摸私有容器 ----
    def get(self, taskId):
        with self.lock:
            return self.tasks.get(taskId)

    def list(self):
        with self.lock:
            return [self.tasks[k] for k in self.tasks]   # 逐键取值：__getitem__ 触发回填

    def put(self, task):
        with self.lock:
            self.tasks[task['id']] = task
            return task

    def runningCount(self):
        with self.lock:
            return len(self.running)

    def runningClient(self, taskId):
        with self.lock:
            return self.running.get(taskId)

    def setRunning(self, taskId, client):
        with self.lock:
            self.running[taskId] = client

    def dropRunning(self, taskId):
        with self.lock:
            self.running.pop(taskId, None)

    def runningClients(self):
        with self.lock:
            return list(self.running.values())

    def agentOf(self, taskId):
        with self.lock:
            return self.agents.get(taskId)

    def setAgent(self, taskId, agent):
        with self.lock:
            self.agents[taskId] = agent

    def dropAgent(self, taskId):
        with self.lock:
            self.agents.pop(taskId, None)

    def load(self):
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
            # L6：状态目录所有者锁——web 之外的进程探测身份（持锁者存活 →
            # 只读；锁空闲照常）。判定在读之前，后面 save/append 才有闸门
            state_owner.adopt()
            # 瞬时共享冲突（并发 save 正在 replace）由 store_io 重试；文件
            # 真缺失/损坏才按空处理——不能把锁冲突当"无状态"丢全部任务
            raw = store_io.loadSnapshot(self.store_path)
            if isinstance(raw, dict):
                self.tasks.update(raw)
                if any('events' in t for t in raw.values() if isinstance(t, dict)):
                    # 旧格式快照（批 PS1）：第一次 save 前留 .pre-migration-*.bak 原件
                    self._keepLegacyShape = not task_events.backupLegacyStore(self.store_path)
            dirty = False
            for task in self.tasks.values():
                # P0-3（外部智能体反馈 2026-11-14，已复现）：审批记录只在内存，
                # 进程一死盘上的 pendingPermission 就是死标记——/wait 把它当活的
                # 回报，driver 一 POST 就 409 再误判 STALE。启动时一律清掉。
                if task.get('pendingPermission'):
                    task['pendingPermission'] = ''
                    dirty = True
                # L 返工（主控追加·必须修）：resuming 唤醒标记同样会落盘——
                # 重启正好落在「唤醒中」（如等节点上一轮结束）时，标记活过重启，
                # 之后任何 message 都被 _resumeWithMessage 挡在 409「会话正在
                # 唤醒，请稍候」（线上实测 5 个任务全卡死）。启动恢复一律清掉；
                # L3 的自动续接在 60s 后的巡检里跑，那时标记早已清干净
                if task.get('resuming'):
                    task['resuming'] = False
                    dirty = True
                if task.get('status') in ('queued', 'running', 'waiting'):
                    canResume = bool(task.get('sessionId'))
                    task_state.transition(
                        task,
                        task_state.TaskState.PAUSED if canResume else task_state.TaskState.INTERRUPTED,
                        'service restart')
                    task['resume'] = canResume
                    if not canResume:
                        task['error'] = '服务重启，任务中断'
                        task['endedAt'] = _now()
                    else:
                        # L3：只有启动恢复路径设这个标记——重启 60s 后的自动
                        # 续接认它，用户主动暂停/线程猝停都不设（不续）
                        task['pausedBy'] = 'restart'
                    dirty = True
            if dirty and state_owner.writable():
                # 只读进程（L6）：状态迁移只活在内存，不落盘也不抛——读要能用
                self.save()

    def save(self):
        """立即落盘（关键变迁与用户动作路径调用；批 PS2 起调了就写）。

        序列化必须在锁内完成：json.dump 遍历活字典，任何无锁写入都会触发
        "dictionary changed size during iteration"。锁内只生成字符串并发
        序号；读 savedSeq→比较→写盘→更新 savedSeq 整段收在 store_io 的
        _IO_LOCK 内完成（SeqGate），锁外不再读写 savedSeq——否则旧序号
        save 拿锁外读到的旧值进门，把已落盘的新快照盖掉（验收-R P1）。
        批 PS1：快照只存元数据（events/_ 键不落盘）；旧格式已由 load 留原件。
        批 PS2：写盘漏斗收敛到 _writeNow；append_event 不再走这里（见 touch）。
        L6：只读进程（状态目录属于 web）的任何快照写入一律明确异常——闸门
        在 _writeNow 漏斗入口，save 与 flush 两条路都盖住。
        """
        self._writeNow()

    def touch(self):
        """标记一次非关键元数据变更（批 PS2）：不写盘，等 1 秒粒度的 flush
        合并落盘。append_event 的 seq/lastActiveAt/lastText、runner 的
        lastTurn/commands/contextHigh 等杂项字段都走这里；关键变迁
        （status/pendingPermission/messageQueue/closedByUser/lead/title）
        仍走 save() 立即落盘。"""
        with self.lock:
            self._dirty = True
            self._dirtySeq += 1

    def _writeNow(self):
        """捕获快照并原子落盘（save 与 flush 的唯一写盘漏斗）；返回是否真写。

        锁内只做捕获（序列化 + 发序号 + 记变更代号），写盘在锁外经 store_io
        的 _IO_LOCK 串行（锁序固定为 调用方锁→_IO_LOCK，无反向等待）。
        落盘成功且捕获后没有新变更到达，才清脏标记——捕获后来的新变更留给
        下一拍，不会悄悄留在内存里（_dirtySeq 代号比对）。
        L6：只读进程先抛（规格原文口径：状态目录属于 web 时本进程只读）。
        """
        state_owner.ensureWritable('agent_tasks.json')
        with self.lock:
            try:
                payload = task_events.snapshotPayload(self.tasks,
                                                       self._keepLegacyShape)
            except (TypeError, ValueError) as e:
                log.warning('serialize tasks failed: %s', e)
                return False
            self._saveSeq += 1
            seq = self._saveSeq
            epoch = self._dirtySeq
        if store_io.saveSnapshot(self.store_path, payload, seq,
                                 self._savedGate) != seq:
            return False        # 序号门拦下（更快的快照已落）：这一份作废
        with self.lock:
            if self._dirtySeq == epoch:
                self._dirty = False
            self._closeIdleJsonl()
        return True

    def flush(self):
        """批 PS2：把累积的非关键变更合并落盘（后台 1 秒一拍 + 退出前一次）。
        有变更才写；没脏直接返回。"""
        with self.lock:
            if not self._dirty:
                return False
        return self._writeNow()

    def shutdown(self):
        """进程退出：最后一次 flush，然后关掉全部 jsonl 句柄。"""
        try:
            self.flush()
        finally:
            self._jsonl.closeAll()

    def _closeIdleJsonl(self):
        """关掉终态/已删任务的 jsonl 句柄（批 PS2，规格例 5）。

        只核对开着句柄的任务：Windows 上句柄不关，日志文件不能改名/删除
        （leads_hosted 的建卡回滚要 unlink 刚建任务的流水）。dict.get 直取
        不触发回填——有句柄的任务不一定被读过，回填要读 jsonl 尾部。
        """
        for taskId in self._jsonl.openTaskIds():
            task = dict.get(self.tasks, taskId)
            if task is None or task.get('status') in _JSONL_DONE:
                self._jsonl.close(taskId)

    def log_path(self, task):
        return os.path.join(self.log_dir, task['id'] + '.jsonl')

    def log_write(self, task, entry):   # 重试与失败计数见 task_events.JsonlWriter
        # L6：只读进程的事件追加（append）同样明确异常（规格原文口径）
        state_owner.ensureWritable('agent_logs/%s.jsonl' % task['id'])
        # 批 PS2：长开句柄追加（锁内写 + flush）；写完即关终态任务的句柄
        line = json.dumps(entry, ensure_ascii=False) + '\n'
        if self._jsonl.append(task['id'], line) \
                and task.get('status') in _JSONL_DONE:
            self._jsonl.close(task['id'])

    def append_event(self, task, etype, **payload) -> contracts.TaskEvent:
        full = payload.pop('full', None)
        payload, full = sensitive_values.redactEvent(etype, payload, full)
        with self.lock:
            task['seq'] = task.get('seq', 0) + 1
            task['lastActiveAt'] = _now()   # 每次事件都刷新（R4-8：轮中可见活跃）
            event = {'seq': task['seq'], 'at': _now(), 'type': etype}
            event.update(payload)
            entry = dict(event)
            if full is not None:
                entry['text'] = full
            self.log_write(task, entry)   # 批 PS1：先写 jsonl（真相先落地），再进窗口/置脏
            events = task.setdefault('events', [])
            events.append(event)
            if len(events) > MAX_EVENTS:
                del events[:len(events) - MAX_EVENTS]
            if etype == 'assistant' and payload.get('text'):
                task['lastText'] = payload['text'][:300]
            # 批 PS2：不再每事件 save——seq/lastActiveAt/lastText 是非关键
            # 变更，等 1 秒粒度的 flush 合并落盘（154/s 爆发下写盘从每事件
            # 一次降到约每秒一次）；关键变迁在各自 save 点立即落盘
            self.touch()
            self.evCond.notify_all()   # 唤醒 /wait 阻塞订阅者（R4-3）
        return event

    def summaryBrief(self, task):
        """列表精简摘要（R4-0）：调用方读到的每个字节都占上下文，
        默认只给索引/判态所需字段，完整摘要用 summary()。"""
        lastText = task.get('lastText') or ''
        return {
            'id': task['id'], 'title': task_title.titleFor(task),
            'status': task.get('status', ''),
            'owner': task.get('owner') or '',
            'lead': task.get('lead') or '',   # 批次 Q：派活的主管（老任务没有这个键时为 ''）
            'node': task_node.of(task),
            'model': task.get('model') or '',
            'modelEffective': task.get('modelEffective') or '',
            'kimiSessionId': task.get('kimiSessionId') or '',
            'kimiSessionFork': bool(task.get('kimiSessionFork')),
            'workline': (task.get('workline') or {}).get('id') or '',
            'workItemId': task.get('workItemId') or '',   # 批次 G2：所属工作项
            'createdAt': task.get('createdAt', 0), 'endedAt': task.get('endedAt', 0),
            'error': task.get('error', ''),
            'lastText': lastText[:200], 'lastTextTruncated': len(lastText) > 200,
            # 上一轮形态（2026-11-13 中招验收方三次早夭）：emptyTurn=0 工具 0 文本
            # 就结束轮次；silentTurn=调了工具却一句终稿都没落（2026-11-14 P1-4）
            'emptyTurn': bool((task.get('lastTurn') or {}).get('empty')),
            'silentTurn': bool((task.get('lastTurn') or {}).get('silent')),
        }

    def summary(self, task) -> contracts.TaskSummary:
        status = task.get('status', '')
        node = task_node.of(task)
        remote = not task_node.isLocal(node)
        cwd = task_node.cwdOf(task)
        return {
            'id': task['id'], 'title': task_title.titleFor(task), 'prompt': task.get('prompt', ''),
            'status': status, 'createdAt': task.get('createdAt', 0),
            'owner': task.get('owner') or '',
            'lead': task.get('lead') or '',   # 批次 Q：派活的主管（老任务没有这个键时为 ''）
            'createdLabel': _iso(task.get('createdAt', 0)),
            'startedAt': task.get('startedAt', 0), 'endedAt': task.get('endedAt', 0),
            # 非本机任务展示执行目录（本机 workspace 目录对调用方毫无意义）
            'workspace': cwd if remote else task.get('workspace', ''),
            # cwd = 执行进程的工作目录（本机可指定；非本机即远端目录）
            'cwd': cwd,
            'lastText': task.get('lastText', ''),
            'resultFiles': task.get('resultFiles', []),
            'error': task.get('error', ''),
            'sessionId': task.get('sessionId') or '',
            'mode': task.get('mode') or 'auto',
            'model': task.get('model') or '',
            'modelEffective': task.get('modelEffective') or '',
            'kimiSessionId': task.get('kimiSessionId') or '',
            'kimiSessionFork': bool(task.get('kimiSessionFork')),
            'node': node,
            'workline': task.get('workline') or {},
            'resume': bool(task.get('resume')),
            'canContinue': task_state.canContinue(task),
            'canResume': task_state.canResume(task),
            'awaitingAnswer': bool(task.get('awaitingAnswer')),
            'pendingPermission': task.get('pendingPermission') or '',
            'lastActiveAt': task.get('lastActiveAt', 0),
            'lastActiveLabel': _iso(task.get('lastActiveAt', 0)),
            'policy': task.get('policy') or {},
        }


class _LazyRepo:
    """首次访问时才创建 TaskRepository，避免 import 即建状态目录。"""

    def __init__(self, factory):
        self._factory = factory
        self._lock = threading.Lock()
        self._value = None

    def _target(self):
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = self._factory()
        return self._value

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def flush(self):
        """仓库还没物化（import 即退出的脚本）时是空操作——不能为一次 flush
        把仓库建出来；atexit 退出 flush 走 _flushRepos 登记表，不经这里。"""
        if self._value is not None:
            self._value.flush()

    def shutdown(self):
        if self._value is not None:
            self._value.shutdown()


repo = _LazyRepo(lambda: TaskRepository(storePath(), logDir()))


def _ensureRoot():
    os.makedirs(taskRoot(), exist_ok=True)


def _slug(text):
    text = (text or '').strip().splitlines()[0] if text else ''
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text).strip(' .')[:24]
    return text or 'task'


def _uniqueName(folder, name):
    return fsutil.uniquePath(folder, name)


def _measure(path):
    """返回 (bytes, files)，用于复制上限检查。"""
    total = 0
    count = 0
    if os.path.isfile(path):
        return os.path.getsize(path), 1
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                pass
            if count > MAX_TASK_FILES:
                return total, count
    return total, count


def _copyEntry(src, inputsDir):
    """复制一个文件/文件夹到 inputs，返回相对工作区的路径。"""
    name = os.path.basename(src.rstrip('\\/'))
    if not name:
        raise ValueError('不能选择磁盘根目录: %s' % src)
    target = _uniqueName(inputsDir, name)
    if os.path.isdir(src):
        shutil.copytree(src, target, symlinks=False,
                        ignore=shutil.ignore_patterns('.git', '__pycache__'))
    else:
        shutil.copy2(src, target)
    return os.path.relpath(target, os.path.dirname(inputsDir))


def _collectSources(paths, workspace):
    inputsDir = os.path.join(workspace, 'inputs')
    os.makedirs(inputsDir, exist_ok=True)
    manifest = []
    totalBytes = 0
    totalFiles = 0
    for raw in paths or []:
        if not isinstance(raw, str):
            continue
        if not os.path.exists(raw):
            raise ValueError('文件不存在: %s' % raw)
        norm = os.path.abspath(raw)
        if not re.match(r'^[A-Za-z]:[\\/]', norm):
            raise ValueError('只允许本机磁盘路径: %s' % raw)
        if re.match(r'^[A-Za-z]:\\?$', norm):
            raise ValueError('不能选择整个磁盘: %s' % norm)
        size, count = _measure(norm)
        totalBytes += size
        totalFiles += count
        if totalBytes > MAX_TASK_MB * 1024 * 1024:
            raise ValueError('输入文件超过 %dMB，请先压缩或分开处理' % MAX_TASK_MB)
        if totalFiles > MAX_TASK_FILES:
            raise ValueError('输入文件数量超过 %d，请先压缩或分开处理' % MAX_TASK_FILES)
        rel = _copyEntry(norm, inputsDir)
        manifest.append({'source': norm, 'input': rel, 'size': size})
    return manifest


def _promptFor(task, manifest, block=''):
    """只补充 Kimi 不可能自己知道的信息：输入副本清单与写入边界。

    block（R5-1）：工作线接力棒段落，插在平台约束之后、任务描述之前。
    """
    lines = []
    if manifest:
        lines.append('输入文件副本（只读）：')
        for item in manifest:
            lines.append('- %s ← %s' % (task_cwd.inputLabel(task, item['input']),
                                        item['source']))
        lines.append('')
    lines.append('产出放当前工作目录；工作目录外只读不写。')
    note = task_cwd.notice(task) if task_node.isLocal(task_node.of(task)) else ''
    if note:
        lines.append(note)
    lines.append('关键信息不足、需要用户拍板时，先停下来用一句话问，不要猜。')
    if task.get('expects'):
        # 批次 G1：有 expects 的任务，交付 = 一次 submit_result 工具调用
        lines.append('本任务要求结构化交付：完成后必须调用 submit_result 工具提交'
                     '（字段见工具入参说明，expects=%s）；自由文本报告只作补充，'
                     '不算交付。' % task.get('expects'))
    lines.append('')
    if block:
        lines.append(block)
        lines.append('')
    lines.append(task['prompt'])
    return '\n'.join(lines)


def _scanResults(workspace):
    results = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'inputs']
        for name in files:
            if name.startswith('.'):
                continue
            if name == 'WORKLINE.md':
                # 工作线接力棒副本（R5-1）：是执行方的读写目标，不是产出
                continue
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, workspace)
            results.append({'path': rel, 'size': st.st_size, 'mtime': int(st.st_mtime)})
    results.sort(key=lambda x: -x['mtime'])
    return results[:200]
