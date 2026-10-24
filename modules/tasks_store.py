# -*- coding: utf-8 -*-
"""任务状态层：任务字典、持久化、事件流水、工作区复制与汇总。

不 import Flask / 不启动线程；只做状态与文件系统。
"""
import datetime
import json
import logging
import os
import re
import shutil
import threading
import time

import config
from modules import contracts, fsutil, task_state

log = logging.getLogger('tasks')

MAX_FILE_MB = 200
MAX_TASK_MB = 500
MAX_TASK_FILES = 3000
MAX_EVENTS = 2000
MAX_TEXT = 8000
MAX_TOOL_RESULT = 4000
MAX_RUNNING = 2
PERMISSION_TIMEOUT = 300


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
        self.tasks = {}
        self.lock = threading.RLock()
        self.running = {}
        self.agents = {}
        self.loaded = False

    # ---- 公开访问器：上层不再直接摸私有容器 ----
    def get(self, taskId):
        with self.lock:
            return self.tasks.get(taskId)

    def list(self):
        with self.lock:
            return list(self.tasks.values())

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
            try:
                with open(self.store_path, encoding='utf-8') as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                self.tasks.update(raw)
            dirty = False
            for task in self.tasks.values():
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
                    dirty = True
            if dirty:
                self.save()

    def save(self):
        # 序列化必须在锁内完成：json.dump 遍历活字典，任何无锁写入都会触发
        # "dictionary changed size during iteration"。锁内只生成字符串，写盘放锁外。
        with self.lock:
            try:
                payload = json.dumps(self.tasks, ensure_ascii=False)
            except (TypeError, ValueError) as e:
                log.warning('serialize tasks failed: %s', e)
                return
        try:
            os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
            tmp = self.store_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
            os.replace(tmp, self.store_path)
        except OSError as e:
            log.warning('save tasks failed: %s', e)

    def log_path(self, task):
        return os.path.join(self.log_dir, task['id'] + '.jsonl')

    def log_write(self, task, entry):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_path(task), 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError as e:
            log.warning('append task log failed: %s', e)

    def append_event(self, task, etype, **payload) -> contracts.TaskEvent:
        full = payload.pop('full', None)
        with self.lock:
            task['seq'] = task.get('seq', 0) + 1
            event = {'seq': task['seq'], 'at': _now(), 'type': etype}
            event.update(payload)
            events = task.setdefault('events', [])
            events.append(event)
            if len(events) > MAX_EVENTS:
                del events[:len(events) - MAX_EVENTS]
            if etype == 'assistant' and payload.get('text'):
                task['lastText'] = payload['text'][:300]
            self.save()
        entry = dict(event)
        if full is not None:
            entry['text'] = full
        self.log_write(task, entry)
        return event

    def summary(self, task) -> contracts.TaskSummary:
        status = task.get('status', '')
        return {
            'id': task['id'], 'title': task.get('title', ''), 'prompt': task.get('prompt', ''),
            'status': status, 'createdAt': task.get('createdAt', 0),
            'createdLabel': _iso(task.get('createdAt', 0)),
            'startedAt': task.get('startedAt', 0), 'endedAt': task.get('endedAt', 0),
            'workspace': task.get('workspace', ''), 'engine': task.get('engine', ''),
            'lastText': task.get('lastText', ''),
            'resultFiles': task.get('resultFiles', []),
            'error': task.get('error', ''),
            'mode': task.get('mode') or 'auto',
            'resume': bool(task.get('resume')),
            'canContinue': task_state.canContinue(task),
            'canResume': task_state.canResume(task),
            'awaitingAnswer': bool(task.get('awaitingAnswer')),
            'pendingPermission': task.get('pendingPermission') or '',
            'lastActiveAt': task.get('lastActiveAt', 0),
            'lastActiveLabel': _iso(task.get('lastActiveAt', 0)),
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


def _promptFor(task, manifest):
    """只补充 Kimi 不可能自己知道的信息：输入副本清单与写入边界。"""
    lines = []
    if manifest:
        lines.append('输入文件副本（只读）：')
        for item in manifest:
            lines.append('- %s ← %s' % (item['input'], item['source']))
        lines.append('')
    lines.append('产出放当前工作目录；工作目录外只读不写。')
    lines.append('关键信息不足、需要用户拍板时，先停下来用一句话问，不要猜。')
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
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, workspace)
            results.append({'path': rel, 'size': st.st_size, 'mtime': int(st.st_mtime)})
    results.sort(key=lambda x: -x['mtime'])
    return results[:200]
