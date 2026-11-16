# -*- coding: utf-8 -*-
"""项目配置（批次 G2）：/api/settings/projects 的存储与校验 + 缺省项目。

项目是工作项的"开发副本"描述：执行点、主仓、主干、worktree 根、门禁/测试
命令、给执行方的项目边界说明（setupNotes 原样注入提示）、备选模型与审批
边界。配置不硬编码 Deskport——任何仓库都能配；首次加载时为 Deskport 开发
副本种一条缺省（命令取 .drive/merge_branch.py 与 tools/run_tests.py 的做法）。

存储：statePath('workitem_projects.json')（照 worklines_store：原子写、
锁、lazy 物化）。不 import Flask——HTTP 面在 workitem_api。
"""
import logging
import os
import re
import threading
import time

import config
from modules import state, state_owner, task_policy

log = logging.getLogger('workitem-projects')

ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')
NAME_MAX = 40
NOTES_MAX = 4000
CMD_MAX = 500
BRANCH_MAX = 100
MAX_FALLBACKS = 5
MAX_PROJECTS = 20

DEFAULT_PROJECT_ID = 'deskport'
DESKPORT_REPO = r'E:\事项\远程\web-platform-dev'
DESKPORT_WORKTREE_ROOT = r'E:\事项\远程\.wt'
DESKPORT_DRIVE = r'E:\事项\远程\.drive'

# 缺省项目的门禁/测试命令：与 .drive/merge_branch.py 合并后的做法一致
# （verify.py --gates-only 三门禁；全量走 tools/run_tests.py）。
# {repo} / {tmp} 由集成作业替换；命令以 repoPath 为 cwd 执行。
DEFAULT_GATES_CMD = ('python -X utf8 "%s\\verify.py" --repo {repo} --gates-only'
                     % DESKPORT_DRIVE)
DEFAULT_TEST_CMD = 'python tools/run_tests.py --basetemp-root {tmp}'

DEFAULT_SETUP_NOTES = (
    '这是 Deskport 平台自己的开发副本。分支从 main 切，工作树在平台建的 '
    'worktree 里；只改自己声明过的足迹，越界先停下来问。'
    '提交信息写清做了什么、为什么；不推送、不合并、不碰线上 web-platform '
    '与 6789，不运行 deploy.ps1。测试用 '
    'E:/事项/远程/web-platform-dev/.venv/Scripts/python.exe，全量 '
    'tools/run_tests.py --basetemp-root <C: 下唯一目录>；服务器后台启动，'
    '只按进程号结束自己起的进程；含反斜杠的文本用编辑器写，不用 heredoc。'
    '需要拍板时把问题写在回复里结束这一轮，不要用提问工具。'
)

DEFAULT_POLICY = {'allowPaths': [DESKPORT_WORKTREE_ROOT, DESKPORT_REPO],
                  'allowCommands': ['^git\\b', '^python\\b', '^py\\b',
                                    '^pytest\\b', '^cat\\b', '^head\\b',
                                    '^tail\\b', '^ls\\b', '^rg\\b',
                                    '^grep\\b', '^find\\b'],
                  'denyCommands': [], 'default': 'ask',
                  'allowBackground': False}


def storePath():
    return config.statePath('workitem_projects.json')


def _now():
    return time.time()


def defaultProject():
    return {
        'id': DEFAULT_PROJECT_ID, 'name': 'Deskport 平台开发',
        'node': 'local', 'repoPath': DESKPORT_REPO,
        'mainBranch': 'main', 'worktreeRoot': DESKPORT_WORKTREE_ROOT,
        'gatesCmd': DEFAULT_GATES_CMD, 'testCmd': DEFAULT_TEST_CMD,
        'setupNotes': DEFAULT_SETUP_NOTES,
        'fallbackModels': [], 'policy': dict(DEFAULT_POLICY),
        'builtin': True,
        'createdAt': _now(), 'updatedAt': _now(),
    }


def _cleanStrList(value, cap, length, label, errors, key):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > cap:
        errors[key] = '%s 必须是数组且不超过 %d 项' % (label, cap)
        return []
    out = []
    for item in value:
        text = str(item or '').strip()
        if not text:
            continue
        if len(text) > length:
            errors[key] = '%s 单项过长（≤%d 字符）' % (label, length)
            return []
        out.append(text)
    return out


def validate(data, isNew=True):
    """项目入参校验。返回 (clean, errors)；errors 为空才收。

    node 只支持本机执行点：平台直接在工作节点上跑 git（v1 不经智能体），
    远端仓库要等 SSH 执行通道进平台作业（下批）——这里明确拒绝，别让
    工作项建到一半才发现跑不了 git。
    """
    errors = {}
    data = data if isinstance(data, dict) else {}
    pid = str(data.get('id') or '').strip()
    if isNew:
        if not ID_RE.match(pid):
            errors['id'] = ('id 需为 1-40 位小写字母/数字/-/_ 且以字母数字开头'
                            '（缺省 deskport）')
    name = str(data.get('name') or '').strip()
    if not name or len(name) > NAME_MAX:
        errors['name'] = 'name 需为 1-%d 个字符' % NAME_MAX
    node = str(data.get('node') or 'local').strip() or 'local'
    if node != 'local':
        errors['node'] = 'node 暂只支持 local（平台直接执行 git；远端仓库下批）'
    repoPath = str(data.get('repoPath') or '').strip()
    if not repoPath or len(repoPath) > 500:
        errors['repoPath'] = 'repoPath 必填（主仓绝对路径，≤500 字符）'
    elif not os.path.isdir(repoPath):
        errors['repoPath'] = 'repoPath 不是已存在的目录: %s' % repoPath
    elif not os.path.isdir(os.path.join(repoPath, '.git')):
        errors['repoPath'] = 'repoPath 不是 git 仓库（无 .git）: %s' % repoPath
    worktreeRoot = str(data.get('worktreeRoot') or '').strip()
    if not worktreeRoot or len(worktreeRoot) > 500:
        errors['worktreeRoot'] = 'worktreeRoot 必填（worktree 根目录，≤500 字符）'
    mainBranch = str(data.get('mainBranch') or 'main').strip() or 'main'
    if len(mainBranch) > BRANCH_MAX:
        errors['mainBranch'] = 'mainBranch 过长（≤%d 字符）' % BRANCH_MAX
    gatesCmd = str(data.get('gatesCmd') or '').strip()
    testCmd = str(data.get('testCmd') or '').strip()
    for key, cmd in (('gatesCmd', gatesCmd), ('testCmd', testCmd)):
        if len(cmd) > CMD_MAX:
            errors[key] = '命令过长（≤%d 字符）' % CMD_MAX
    setupNotes = str(data.get('setupNotes') or '').strip()
    if len(setupNotes) > NOTES_MAX:
        errors['setupNotes'] = 'setupNotes 过长（≤%d 字）' % NOTES_MAX
    fallbackModels = _cleanStrList(data.get('fallbackModels'), MAX_FALLBACKS, 100,
                                   'fallbackModels', errors, 'fallbackModels')
    try:
        policy = task_policy.validatePolicy(data.get('policy'))
    except ValueError as e:
        errors['policy'] = str(e)
        policy = {}
    clean = {'name': name, 'node': node, 'repoPath': repoPath,
             'mainBranch': mainBranch, 'worktreeRoot': worktreeRoot,
             'gatesCmd': gatesCmd, 'testCmd': testCmd,
             'setupNotes': setupNotes, 'fallbackModels': fallbackModels,
             'policy': policy}
    if isNew:
        clean['id'] = pid
    return clean, errors


class ProjectRepository:
    """项目配置仓储（照 worklines_store：锁 + 原子写 + lazy 物化）。"""

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
            raw = state.readJson(self.store_path, {})
            if isinstance(raw, dict):
                self.items.update(raw)
            if DEFAULT_PROJECT_ID not in self.items:
                # 首次加载种缺省项目（Deskport 开发副本）；写失败（只读进程）
                # 不影响读——缺省形状内存里照常返回
                self.items[DEFAULT_PROJECT_ID] = defaultProject()
                try:
                    self.save()
                except Exception as e:
                    log.info('seed default project skipped: %s', e)

    def save(self):
        state_owner.ensureWritable('workitem_projects.json')
        with self.lock:
            try:
                state.atomicWriteJson(self.store_path, self.items)
            except OSError:
                pass

    def get(self, pid):
        self.load()
        with self.lock:
            return self.items.get(str(pid or ''))

    def list(self):
        self.load()
        with self.lock:
            return list(self.items.values())

    def put(self, project):
        pid = project.get('id') or ''
        with self.lock:
            project['updatedAt'] = _now()
            self.items[pid] = project
            self.save()
            return project

    def remove(self, pid):
        with self.lock:
            project = self.items.pop(str(pid or ''), None)
            if project is None:
                raise KeyError(pid)
            self.save()
        return project


class _LazyRepo:
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


repo = _LazyRepo(lambda: ProjectRepository(storePath()))
