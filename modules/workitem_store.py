# -*- coding: utf-8 -*-
"""工作项仓储（批次 G2）：数据模型与持久化（规格 §1）。

照 worklines_store 的持久化规矩（PS1/PS2）：独立存储、原子写、不塞进
tasks_store——agent_tasks.json 每个事件全量重写，工作项虽不逐事件变，
但 history/集成结果是大字段，混进去等于每个 chunk 都带一遍。

    statePath('workitems.json')          工作项元数据（小、原子写）
    statePath('workitem_logs/<id>.log')  集成作业完整输出（工作项记路径）

不 import Flask、不 import tasks_*；任务 id 只以字符串存（implTask /
verifyTasks），任务实体归 tasks_store。
"""
import logging
import os
import re
import threading
import time

import config
from modules import state, state_owner, workitem_state

log = logging.getLogger('workitem-store')

ID_RE = re.compile(r'^wi-[a-z0-9]{6}$')
SPEC_PATH_MAX = 500
LEAD_MAX = 40
TASKID_RE = re.compile(r'^[a-f0-9]{8,16}$')


def storePath():
    return config.statePath('workitems.json')


def logDir():
    return os.path.join(os.path.dirname(storePath()), 'workitem_logs')


def logPath(wid):
    return os.path.join(logDir(), str(wid or '') + '.log')


def _now():
    return time.time()


def newId():
    return 'wi-' + os.urandom(3).hex()


def blank():
    """新工作项的缺省形状（流水线/API 在此基础上补字段）。"""
    return {
        'id': '', 'projectId': '', 'lead': '', 'title': '',
        'specPath': '', 'specText': '',
        'branch': '', 'worktree': '',
        'implModel': '', 'verifyModel': '',
        'state': workitem_state.QUEUED, 'attempt': 0,
        'maxAttempts': workitem_state.MAX_ATTEMPTS_DEFAULT,
        'implTask': '', 'verifyTasks': [],
        'lastImplResult': None, 'lastVerdict': None,
        'integration': {'commit': '', 'gates': '', 'tests': '', 'log': ''},
        'history': [],
        # §2b 防冲突：创建时声明，实施结束后用实际 diff 校正
        'footprint': [], 'footprintActual': [], 'forceParallel': False,
        'queuedFor': '', 'queuedShare': [],
        # 异常处理计数（§2 提前收工/上下文换手/任务起不来）
        'earlyFinish': 0, 'stalls': 0, 'startFails': 0, 'handoffs': 0,
        'resultSeen': {}, 'tmpDir': '',
        'createdAt': _now(), 'updatedAt': _now(),
    }


def normalizeTaskId(value):
    """任务 id 形状校验（防把任意字符串写进 implTask/verifyTasks）。"""
    text = str(value or '').strip()
    return text if TASKID_RE.match(text) else ''


class WorkitemRepository:
    """工作项元数据仓储（与 WorklineRepository 同构：锁 + 原子写）。"""

    def __init__(self, path):
        self.store_path = path
        self.items = {}
        self.lock = threading.RLock()
        self.loaded = False

    def load(self):
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
            try:
                raw = state.readJson(self.store_path)
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                self.items.update(raw)

    def save(self):
        # L6：只读进程（状态目录属于 web）一律明确异常（读/加载不受影响）
        state_owner.ensureWritable('workitems.json')
        with self.lock:
            try:
                state.atomicWriteJson(self.store_path, self.items)
            except OSError:
                pass

    def get(self, wid):
        self.load()
        with self.lock:
            return self.items.get(str(wid or ''))

    def list(self):
        self.load()
        with self.lock:
            return list(self.items.values())

    def create(self, data):
        """建工作项（data 已过 workitem_state.validateCreate）。

        id 缺省 wi-<6 位随机>；分支/worktree 由流水线在项目节点上建好之后
        回填（创建事务分两步：先落卡，平台执行 git，失败即删卡）。
        """
        self.load()
        with self.lock:
            wid = str(data.get('id') or '').strip()
            if wid and not ID_RE.match(wid):
                raise ValueError('工作项 id 不合法（wi-<6 位小写字母数字>）')
            if not wid:
                wid = newId()
                while wid in self.items:
                    wid = newId()
            if wid in self.items:
                raise ValueError('工作项 %s 已存在' % wid)
            wi = blank()
            wi.update({k: v for k, v in (data or {}).items() if v not in (None, '')})
            wi['id'] = wid
            wi['createdAt'] = wi['updatedAt'] = _now()
            self.items[wid] = wi
            self.save()
            return wi

    def update(self, wid, patch):
        """合并补丁（白名单外字段忽略，防把任意键写进存储）。"""
        wi = self.get(wid)
        if wi is None:
            raise KeyError(wid)
        allowed = set(blank()) | {'workItemId'}
        with self.lock:
            for key, value in (patch or {}).items():
                if key in allowed and key not in ('id', 'createdAt'):
                    wi[key] = value
            wi['updatedAt'] = _now()
            self.save()
            return wi

    def remove(self, wid):
        with self.lock:
            wi = self.items.pop(str(wid or ''), None)
            if wi is None:
                raise KeyError(wid)
            self.save()
        try:
            os.unlink(logPath(wid))
        except OSError:
            pass
        return wi


class _LazyRepo:
    """首次访问才建仓储，避免 import 即建状态目录（同 worklines_store）。"""

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


repo = _LazyRepo(lambda: WorkitemRepository(storePath()))


# ---------------------------------------------------------------- 集成日志

def appendLog(wid, text):
    """集成作业输出追加到日志文件（工作项 integration.log 记路径）。

    只追加不重写：合并/门禁/测试三段输出按序落同一个文件，主管点开就是
    完整现场。失败只记日志——日志写不进去不该拦住集成主链路。
    """
    path = logPath(wid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state_owner.ensureWritable('workitem_logs/%s.log' % (wid or ''))
        with open(path, 'a', encoding='utf-8') as f:
            f.write(str(text or ''))
            if not str(text or '').endswith('\n'):
                f.write('\n')
        return path
    except Exception as e:       # 只读进程的 StateDirReadOnly 也落这里
        log.warning('workitem log append failed %s: %s', wid, e)
        return ''


def readLog(wid, tailBytes=64 * 1024):
    """读集成日志尾部（API 展示用；大文件只给尾部）。"""
    path = logPath(wid)
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > tailBytes:
                f.seek(-tailBytes, os.SEEK_END)
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''
