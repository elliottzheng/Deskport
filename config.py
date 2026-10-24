# -*- coding: utf-8 -*-
r"""配置模块 — 端口/密码 + 状态目录

设计（v2 环境实测教训）：
    - config.json 在项目根：字段 port / password / stateDirName(可选)
    - SSH 运行时状态（主机/凭据/known_hosts）在 %APPDATA%\<stateDirName>\：
      项目目录会被本机安全/清理进程周期性删文件（.run/ssh_hosts.json/
      run.log 均消失过），用户数据目录不受影响
    - 不用环境变量
    - stateDirName 用于 dev/prod 双实例隔离（dev 设 web-platform-dev），
      缺省 web-platform 保持单实例行为不变
"""
import json
import os
import secrets
import threading
import time
from urllib.parse import urlparse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DEFAULTS = {'port': 6789, 'password': '', 'stateDirName': 'web-platform',
            'inboxEnabled': False}
_cache = {'mtime': None, 'data': None}
_cacheLock = threading.Lock()


def load():
    """读取配置; 缺失/损坏回退默认值。按 mtime 缓存, 改 config.json 仍即时生效。"""
    try:
        mtime = os.stat(CONFIG_PATH).st_mtime_ns
    except OSError:
        mtime = None
    with _cacheLock:
        if mtime is not None and _cache['mtime'] == mtime and _cache['data'] is not None:
            return dict(_cache['data'])
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg = dict(DEFAULTS)
            cfg.update(data)
            with _cacheLock:
                _cache['mtime'] = mtime
                _cache['data'] = cfg
            return dict(cfg)
    except (OSError, ValueError):
        pass
    return dict(DEFAULTS)


def getPort():
    try:
        port = int(load().get('port'))
    except (TypeError, ValueError):
        return DEFAULTS['port']
    return port if 1 <= port <= 65535 else DEFAULTS['port']


def getPassword():
    return load().get('password') or ''


def setPassword(newPassword):
    """原子写回 config.json (保留其他键)。"""
    from modules import state
    cfg = load()
    cfg['password'] = newPassword
    state.atomicWriteJson(CONFIG_PATH, cfg)
    with _cacheLock:
        _cache['mtime'] = None
        _cache['data'] = None


def getIdleTimeoutSeconds():
    r"""无人附着会话的回收宽限期(秒)。terminalIdleTimeoutHours=0 或缺失 = 永不回收。"""
    try:
        hours = float(load().get('terminalIdleTimeoutHours', 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0.0, hours) * 3600


def _dirList(value):
    if not isinstance(value, list):
        return []
    return [p for p in value if isinstance(p, str) and p.strip()]


def getWorkDirs():
    r"""AI 落点判断的常用工作目录; 缺省用用户主目录与 Documents（存在才纳入）。"""
    dirs = _dirList(load().get('workDirs'))
    if not dirs:
        home = os.path.expanduser('~')
        dirs = [home, os.path.join(home, 'Documents')]
    return [p for p in dirs if os.path.isdir(p)]


def getFallbackDir():
    r"""低置信度兜底目录; 缺失 = <第一个工作目录>\默认工作空间。"""
    path = load().get('fallbackDir')
    if isinstance(path, str) and path.strip():
        return path
    work = getWorkDirs()
    return os.path.join(work[0], '默认工作空间') if work else ''


def getDirNotes():
    notes = load().get('dirNotes')
    return notes if isinstance(notes, dict) else {}


def getAiCommand():
    return str(load().get('aiCommand') or '').strip()


def getDecisionApiKey():
    return str(load().get('decisionApiKey') or '').strip()


def getSessiondUrl(kind='local'):
    r"""会话守护进程地址。local=本机终端, ssh=远程 SSH; 空 = 留在 web 进程内。
    配置键: sessiondUrl / sshSessiondUrl。"""
    key = 'sessiondUrl' if kind == 'local' else 'sshSessiondUrl'
    return str(load().get(key) or '').strip().rstrip('/')


def getSessiondPort(kind='local'):
    """守护进程监听端口：配置 URL 的端口优先，否则用默认 6791/6792。"""
    default = 6791 if kind == 'local' else 6792
    try:
        return urlparse(getSessiondUrl(kind) or '').port or default
    except ValueError:
        return default


def getSessiondToken(kind='local'):
    r"""web 与对应守护进程的共享令牌; 首次调用原子创建, 两个服务各自独立。"""
    path = statePath('sessiond_token_' + kind)
    if not os.path.isfile(path):
        try:
            with open(path, 'x', encoding='utf-8') as f:
                f.write(secrets.token_hex(24))
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            pass
        except OSError:
            return ''
    for _ in range(20):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                token = f.read().strip()
            if token:
                return token
        except OSError:
            pass
        time.sleep(0.02)
    return ''


def isInboxEnabled():
    r"""收件箱扫描开关；缺省关闭，需在设置页显式开启。"""
    return bool(load().get('inboxEnabled'))


def defaultInboxDirs():
    r"""软件默认的收件箱目录：只使用用户主目录下的常见位置，不扫描本机其他盘。"""
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, 'Downloads'),
        os.path.join(home, 'Desktop'),
        os.path.join(home, 'Documents', 'WeChat Files'),
        os.path.join(home, 'Documents', 'xwechat_files'),
        os.path.join(home, 'Documents', 'Tencent Files'),
        os.path.join(home, 'QQ_FILES'),
    ]
    out = []
    for path in candidates:
        if os.path.isdir(path) and path not in out:
            out.append(path)
    return out


def getInboxDirs():
    r"""收件箱观察目录。关闭时返回空列表；开启且未配置时用软件默认目录。"""
    if not isInboxEnabled():
        return []
    dirs = _dirList(load().get('inboxDirs'))
    if dirs:
        return [p for p in dirs if os.path.isdir(p)]
    return defaultInboxDirs()


def setInboxSettings(enabled, dirs):
    """写回收件箱开关与目录（保留其他配置键）。"""
    from modules import state
    cfg = load()
    cfg['inboxEnabled'] = bool(enabled)
    cfg['inboxDirs'] = list(dirs)
    state.atomicWriteJson(CONFIG_PATH, cfg)
    with _cacheLock:
        _cache['mtime'] = None
        _cache['data'] = None


def stateDir():
    r"""SSH 运行时状态目录: %APPDATA%\<stateDirName> (自动创建; 名字取自 config.json)"""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    name = load().get('stateDirName') or 'web-platform'
    path = os.path.join(base, str(name))
    os.makedirs(path, exist_ok=True)
    return path


def statePath(filename):
    return os.path.join(stateDir(), filename)
