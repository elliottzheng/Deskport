# -*- coding: utf-8 -*-
"""工作线（R5-1a）：接力棒正文/元数据读写、首条消息组装、轮末写回。

工作线 ≠ 会话：状态放在会话之外（接力棒文件 + 元数据），会话只是
"一段时间里的执行者"。存储刻意与任务卡分开——agent_tasks.json 每个事件
全量重写，把正文塞进去等于每个 chunk 都写一遍大字段：

    statePath('worklines.json')      元数据（小、原子写）
    statePath('worklines/<id>.md')   接力棒正文（人工可直接改，按 hash 计 rev）

不 import Flask，不 import tasks_*；任务字典由调用方传入。
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid

import config
from modules import state, task_node

BATON_FILE = 'WORKLINE.md'     # 工作区里的副本，也是执行方的写回目标
PROMPT_LIMIT = 4000            # 首条消息总长预算（与建任务 prompt 上限一致）
DEFAULT_CONTEXT_WARN = 0.75
EXCERPT_MIN = 240              # 摘要模式至少留这么多正文才值得内联
ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')


class RevConflict(Exception):
    """主控手里的 baseRev 已过期（有人先改了正文）。"""


def storePath():
    return config.statePath('worklines.json')


def docDir():
    return os.path.join(os.path.dirname(storePath()), 'worklines')


def docPath(wid):
    return os.path.join(docDir(), wid + '.md')


def hashText(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _now():
    return time.time()


def _writeText(path, text):
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.md')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text or '')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _readText(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def slugId(title):
    base = re.sub(r'[^a-z0-9]+', '-', (title or '').lower()).strip('-')[:24]
    return (base + '-' if base else 'wl-') + uuid.uuid4().hex[:6]


def policySummary(policy):
    """一段话说明服务端会怎么放行（首条消息里给执行方看）。"""
    policy = policy or {}
    if not policy:
        return '未设置 policy：逐项人工审批（default=ask）。'
    parts = ['默认 %s' % (policy.get('default') or 'ask')]
    paths = policy.get('allowPaths') or []
    if paths:
        shown = '、'.join(paths[:3]) + ('…' if len(paths) > 3 else '')
        parts.append('允许路径 %d 项（%s）' % (len(paths), shown))
    allow = policy.get('allowCommands') or []
    if allow:
        parts.append('允许命令 %d 条' % len(allow))
    deny = policy.get('denyCommands') or []
    if deny:
        parts.append('需人工确认的命令 %d 条' % len(deny))
    parts.append('后台任务：%s' % ('允许' if policy.get('allowBackground') else '禁用'))
    return '；'.join(parts) + '。'


def composeBlock(wl, doc, room):
    """首条消息里的接力棒段落；room 是留给这一段的字符预算。

    标题、边界、写回约定永远在（它们是"接得上"的前提）；正文放不下就
    只放摘要并指路工作区的 WORKLINE.md（R5-1a 的降级路径）。
    返回 (block, mode)，mode ∈ full / excerpt / pointer。
    """
    wid = wl.get('id') or ''
    title = wl.get('title') or wid
    rev = (wl.get('rev') or '')[:8]
    head = '## 工作线接力棒（%s · %s · rev %s）\n' % (title, wid, rev)
    tail = ('\n交接约定：本轮结束前把更新后的接力棒**整篇覆盖**写到当前工作目录的 '
            '%s；本轮没有新进展就保持原样，不要追加。\n' % BATON_FILE)
    edges = ('## 边界（服务端执行）\n'
             '- 不要使用后台任务（后台调用会被服务端直接拒绝）。\n'
             '- 审批边界：%s\n' % policySummary(wl.get('policy')))
    body = (doc or '').strip()
    if not body:
        return (head + '（接力棒还没有内容：按任务描述开工，结束时写出第一版。）\n'
                + tail + edges, 'empty')
    fixed = len(head) + len(tail) + len(edges) + 2
    budget = int(room) - fixed
    if len(body) <= budget:
        return head + body + '\n' + tail + edges, 'full'
    if budget >= EXCERPT_MIN:
        cut = body[:budget].rsplit('\n', 1)[0] or body[:budget]
        part = (cut + '\n…（正文已截断，完整接力棒见 %s，开工前先读它。）\n'
                % BATON_FILE)
        return head + part + tail + edges, 'excerpt'
    return (head + '（正文较长，已省略；完整接力棒见 %s，开工前先读它。）\n'
            % BATON_FILE + tail + edges, 'pointer')


def reminder(wl):
    """续聊时 prompt 尾部的约定行（不重贴正文，省上下文）。"""
    return ('（工作线 %s：本轮结束前把更新后的接力棒整篇覆盖写到 %s；'
            '无进展则保持原样。不要使用后台任务。）'
            % (wl.get('id') or '', BATON_FILE))


class WorklineRepository:
    """工作线元数据 + 接力棒正文的仓储（与 TaskRepository 同构）。"""

    def __init__(self, store_path, docs_dir):
        self.store_path = store_path
        self.docs_dir = docs_dir
        self.items = {}
        self.lock = threading.RLock()
        self.loaded = False

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
                self.items.update(raw)

    def save(self):
        with self.lock:
            try:
                state.atomicWriteJson(self.store_path, self.items)
            except OSError:
                pass

    def get(self, wid):
        self.load()
        with self.lock:
            return self.items.get(wid)

    def list(self):
        self.load()
        with self.lock:
            return list(self.items.values())

    def docPath(self, wl):
        return os.path.join(self.docs_dir, (wl.get('id') or '') + '.md')

    def readDoc(self, wl):
        return _readText(self.docPath(wl))

    def writeDoc(self, wl, text, by=''):
        rev = hashText(text)
        _writeText(self.docPath(wl), text)
        with self.lock:
            wl['rev'] = rev
            wl['docBytes'] = len((text or '').encode('utf-8'))
            wl['updatedAt'] = _now()
            wl['updatedBy'] = by or 'master'
            self.save()
        return rev

    def create(self, data):
        title = str(data.get('title') or '').strip()[:80]
        wid = str(data.get('id') or '').strip().lower()
        if not wid:
            wid = slugId(title)
        if not ID_RE.match(wid):
            raise ValueError('工作线 id 只能是小写字母/数字/-/_（≤40 字符）')
        self.load()
        with self.lock:
            if wid in self.items:
                raise ValueError('工作线 %s 已存在' % wid)
            wl = {'id': wid, 'title': title or wid, 'rev': '', 'docBytes': 0,
                  'createdAt': _now(), 'updatedAt': _now(),
                  'updatedBy': str(data.get('by') or 'master'),
                  'tasks': [], 'currentTask': '',
                  'policy': data.get('policy') or {},
                  'node': str(data.get('node') or '').strip(),
                  'cwd': str(data.get('cwd') or '').strip(),
                  'repo': str(data.get('repo') or '').strip(),
                  'branch': str(data.get('branch') or '').strip(),
                  'defaults': data.get('defaults') or {},
                  'autoRotate': bool(data.get('autoRotate')),
                  'contextWarn': _warnValue(data.get('contextWarn'))}
            self.items[wid] = wl
            self.save()
            return wl

    def update(self, wid, patch, baseRev=''):
        self.load()
        with self.lock:
            wl = self.items.get(wid)
            if not wl:
                raise KeyError(wid)
            if baseRev and baseRev != (wl.get('rev') or ''):
                raise RevConflict(wl.get('rev') or '')
            if 'title' in patch:
                wl['title'] = str(patch.get('title') or '').strip()[:80] or wl['title']
            for key in ('policy', 'defaults'):
                if key in patch:
                    wl[key] = patch.get(key) or {}
            for key in ('node', 'cwd', 'repo', 'branch'):
                if key in patch:
                    wl[key] = str(patch.get(key) or '').strip()
            if 'node' in patch:
                task_node.forgetLegacyEngine(wl)
            if 'autoRotate' in patch:
                wl['autoRotate'] = bool(patch.get('autoRotate'))
            if 'contextWarn' in patch:
                wl['contextWarn'] = _warnValue(patch.get('contextWarn'))
            wl['updatedAt'] = _now()
            wl['updatedBy'] = str(patch.get('by') or 'master')
            if 'doc' in patch:
                self.writeDoc(wl, str(patch.get('doc') or ''), wl['updatedBy'])
            self.save()
            return wl

    def addTask(self, wid, taskId):
        with self.lock:
            wl = self.items.get(wid)
            if not wl or not taskId:
                return
            tasks = wl.setdefault('tasks', [])
            if taskId not in tasks:
                tasks.append(taskId)
            wl['currentTask'] = taskId
            wl['updatedAt'] = _now()
            self.save()

    def setCurrent(self, wid, taskId):
        with self.lock:
            wl = self.items.get(wid)
            if not wl:
                return
            wl['currentTask'] = taskId or ''
            wl['updatedAt'] = _now()
            self.save()

    def remove(self, wid):
        self.load()
        with self.lock:
            wl = self.items.pop(wid, None)
            if wl is None:
                raise KeyError(wid)
            self.save()
        try:
            os.unlink(self.docPath(wl))
        except OSError:
            pass


def _warnValue(value):
    try:
        ratio = float(value if value is not None else DEFAULT_CONTEXT_WARN)
    except (TypeError, ValueError):
        ratio = DEFAULT_CONTEXT_WARN
    return round(min(max(ratio, 0.1), 0.99), 2)


class _LazyRepo:
    """首次访问才建仓储，避免 import 即建状态目录。"""

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


repo = _LazyRepo(lambda: WorklineRepository(storePath(), docDir()))


# ---------------------------------------------------------------- 任务侧

def _wlOf(task):
    """(workline 元数据, 任务上的引用)；工作线被删时 wl 为 None。"""
    info = (task or {}).get('workline') or {}
    wid = info.get('id') or ''
    return (repo.get(wid) if wid else None), info


def attach(task, wl, doc, baseLen):
    """建任务时把接力棒挂上去：预算内联、写工作区副本、登记到工作线。"""
    block, mode = composeBlock(wl, doc, PROMPT_LIMIT - int(baseLen))
    task['workline'] = {'id': wl['id'], 'title': wl.get('title') or wl['id'],
                        'rev': wl.get('rev') or '', 'injected': mode}
    task['worklineBlock'] = block
    workspace = task.get('workspace') or ''
    if workspace:
        _writeText(os.path.join(workspace, BATON_FILE), doc or '')
    repo.addTask(wl['id'], task.get('id'))
    return block, mode


def turnTail(task):
    """续聊前：主控改过正文就同步工作区副本，并给出尾部约定行。"""
    wl, _info = _wlOf(task)
    if not wl:
        return ''
    note = ''
    workspace = task.get('workspace') or ''
    if task_node.isLocal(task_node.of(task)) and workspace:
        doc = repo.readDoc(wl)
        path = os.path.join(workspace, BATON_FILE)
        if os.path.isdir(workspace) and _readText(path) != doc:
            _writeText(path, doc)
            note = ('（接力棒已被主控更新到 rev %s，%s 已同步，本轮以文件为准。）\n'
                    % ((wl.get('rev') or '')[:8], BATON_FILE))
    return note + reminder(wl)


def syncFromWorkspace(task):
    """轮末写回：执行方改过 WORKLINE.md 才落库。

    返回 {'rev': ...} 表示已更新；{'skipped': 'conflict'} 表示主控轮中改过，
    本轮写回放弃（单写者，不覆盖）；None 表示没有变化或不可读回。
    """
    wl, info = _wlOf(task)
    if not wl or not task_node.isLocal(task_node.of(task)):
        return None
    path = os.path.join(task.get('workspace') or '', BATON_FILE)
    if not os.path.isfile(path):
        return None
    text = _readText(path)
    if hashText(text) == hashText(repo.readDoc(wl)):
        return None
    if (info.get('rev') or '') != (wl.get('rev') or ''):
        return {'skipped': 'conflict', 'rev': wl.get('rev') or ''}
    rev = repo.writeDoc(wl, text, 'executor:%s' % (task.get('id') or ''))
    info['rev'] = rev
    return {'rev': rev}


def contextInfo(task):
    usage = (task or {}).get('usage') or {}
    used, size = usage.get('used'), usage.get('size')
    if not isinstance(used, (int, float)) or not isinstance(size, (int, float)):
        return None
    if isinstance(used, bool) or isinstance(size, bool) or size <= 0:
        return None
    return {'used': int(used), 'max': int(size),
            'ratio': round(float(used) / float(size), 3)}


def warnRatio(wl):
    return _warnValue((wl or {}).get('contextWarn'))