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
import re
import secrets
import threading
import time
from urllib.parse import urlparse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DEFAULTS = {'port': 6789, 'password': '', 'stateDirName': 'web-platform',
            'inboxEnabled': False, 'brandName': 'Deskport'}


def getRemotes():
    """联邦远端 Deskport 列表: [{id, name, base, token, terminal, apps}]。

    base 指向远端 web 根 (如 http://127.0.0.1:6790)；token 须与远端
    config 的 federationToken 一致。消费策略由 A 机自己决定:
    - terminal (缺省 true): 是否合并该机终端会话 (拉取/合并/WS 路由)
    - apps (缺省 [] = 一律不接): 接受该机哪些外部应用, ["*"] 全收;
      接受/拒绝某个 app 就是有没有它的 id
    - files (缺省 false): 是否允许在文件页浏览该机的文件 (只读转发)
    """
    raw = load().get('remotes')
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rid = str(item.get('id') or '').strip()
        base = str(item.get('base') or '').strip().rstrip('/')
        if not re.match(r'^[a-z0-9_-]{1,20}$', rid) or not base.startswith(('http://', 'https://')):
            continue
        apps = item.get('apps')
        apps = [str(a).strip() for a in apps if str(a).strip()] if isinstance(apps, list) else []
        models = item.get('models')
        models = [str(a).strip() for a in models if str(a).strip()] if isinstance(models, list) else []
        out.append({'id': rid,
                    'name': str(item.get('name') or rid)[:24],
                    'base': base,
                    'token': str(item.get('token') or ''),
                    'terminal': item.get('terminal', True) is not False,
                    'apps': apps,
                    'files': item.get('files', False) is True,
                    'models': models,
                    'model': str(item.get('model') or '').strip()})
    return out


def getFederationToken():
    r"""联邦模式: 持有此 token 的调用方 (其他 Deskport) 免登录访问本机 API。"""
    return str(load().get('federationToken') or '')
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


def getAgentTokens():
    r"""智能体 API 令牌列表; 每项 {id, name, tokenHash(SHA-256), ...}。缺省空。"""
    tokens = load().get('agentTokens')
    if not isinstance(tokens, list):
        return []
    return [t for t in tokens if isinstance(t, dict)]


def getBrandName():
    r"""界面品牌名；缺省 Deskport，最长 24 字符。"""
    name = str(load().get('brandName') or 'Deskport').strip()
    return name[:24] or 'Deskport'


def setBrandName(name):
    """写回品牌名（保留其他配置键）。"""
    from modules import state
    cfg = load()
    cfg['brandName'] = name
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


def getAgentNodes():
    """远端智能体节点列表: [{name, url, token, via, remote}]。

    name 显示用；url 是 agent node 的 base；token 为 node 的 X-Agent-Token。
    via 是 SSH 主机 id：给出时请求经 SSH 隧道转发到 remote（缺省
    127.0.0.1:<url端口>），用于 node 端口被防火墙过滤的内网机器。
    """
    raw = load().get('agentNodes')
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        url = str(item.get('url') or '').strip().rstrip('/')
        if not name or len(name) > 24 or not url.startswith(('http://', 'https://')):
            continue
        out.append({'name': name,
                    'url': url,
                    'token': str(item.get('token') or ''),
                    'via': str(item.get('via') or '').strip(),
                    'remote': str(item.get('remote') or '').strip(),
                    'models': _strList(item.get('models')),
                    'model': str(item.get('model') or '').strip()})
    return out


def _strList(raw):
    """配置里的模型白名单：字符串数组，去空去重。"""
    if not isinstance(raw, list):
        return []
    seen = []
    for item in raw:
        text = str(item or '').strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def getSessiondPort(kind='local'):
    """守护进程监听端口：配置 URL 的端口优先，否则用默认 6791/6792。"""
    default = 6791 if kind == 'local' else 6792
    try:
        return urlparse(getSessiondUrl(kind) or '').port or default
    except ValueError:
        return default


def _sharedToken(path):
    """令牌文件原子创建 + 重试读取；web 与对应服务各自独立持有。"""
    if not os.path.isfile(path):
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'x', encoding='utf-8') as f:
                f.write(secrets.token_hex(24))
                f.flush()
                os.fsync(f.fileno())
        except (FileExistsError, OSError):
            pass
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


def getSessiondToken(kind='local'):
    r"""web 与对应守护进程的共享令牌; 首次调用原子创建, 两个服务各自独立。"""
    return _sharedToken(statePath('sessiond_token_' + kind))


def getAgentNodeToken():
    r"""本机智能体节点共享令牌；与 agent_node --token-file 读同一文件。

    路径固定在 node 自己的数据目录（~/.deskport-agent-node/token），
    不随 stateDirName 变化 —— 本机 node 是 dev/prod 共享的单例服务。
    """
    return _sharedToken(os.path.join(os.path.expanduser('~'),
                                     '.deskport-agent-node', 'token'))


def getLocalAgentNode():
    """本机智能体节点（engine=acp 任务的托管者）；localAgentNodePort=0 表示未启用。

    返回与 agentNodes 条目同构的 dict（无 via，本机直连），None = 未启用。
    """
    try:
        port = int(load().get('localAgentNodePort') or 0)
    except (TypeError, ValueError):
        return None
    if port <= 0:
        return None
    return {'name': 'local', 'url': 'http://127.0.0.1:%d' % port,
            'token': getAgentNodeToken(), 'via': '', 'remote': '',
            'models': _strList(load().get('localAgentNodeModels')),
            'model': str(load().get('localAgentNodeModel') or '').strip()}


def getLocalAgentNodePort():
    try:
        return int(load().get('localAgentNodePort') or 0)
    except (TypeError, ValueError):
        return 0


def getTaskPermissionTimeoutSec():
    """任务审批挂起的超时秒数（反馈 R4-2：默认 ≥30 分钟，0=不超时）。"""
    value = load().get('taskPermissionTimeoutSec')
    if value is None:
        return 1800
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 1800


def getTaskMaxRunning():
    """并发 running 任务上限（R4-9）：缺省 8，0=不限；load 按 mtime 热读，
    改 config.json 即时生效无需重启。负数视为非法回退缺省（避免手滑 -1
    变成"不限"）。"""
    value = load().get('taskMaxRunning')
    if value is None:
        return 8
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 8
    if value < 0:
        return 8
    return value


def getPhoneUploadDir():
    r"""手机分享上传的落盘根目录; 缺失 = <fallbackDir>\手机上传。"""
    path = load().get('phoneUploadDir')
    if isinstance(path, str) and path.strip():
        return path
    fallback = getFallbackDir()
    return os.path.join(fallback, '手机上传') if fallback else ''


def getPhoneUploadMaxMB():
    r"""手机上传单文件上限(MB); 缺省 2048, 非法/非正值回退缺省。"""
    value = load().get('phoneUploadMaxMB')
    if value is None:
        return 2048
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 2048
    return value if value > 0 else 2048


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
    r"""收件箱观察目录。关闭时返回空列表；开启且未配置时用软件默认目录。
    手机上传目录自动并入——手机分享的文件直接进收件箱（评估 §5）。"""
    if not isInboxEnabled():
        return []
    dirs = _dirList(load().get('inboxDirs'))
    if not dirs:
        dirs = defaultInboxDirs()
    phone = getPhoneUploadDir()
    if phone and os.path.isdir(phone) and phone not in dirs:
        dirs.append(phone)
    return [p for p in dirs if os.path.isdir(p)]


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


def setFederationToken(token):
    """写回本机联邦令牌; 空串 = 不允许被其他 Deskport 联邦接入。"""
    from modules import state
    cfg = load()
    cfg['federationToken'] = (token or '').strip()
    state.atomicWriteJson(CONFIG_PATH, cfg)
    with _cacheLock:
        _cache['mtime'] = None
        _cache['data'] = None


def setRemotes(remotes):
    """写回远端机器列表（保留其他配置键）; remotes 为 [{id,name,base,token}]。"""
    from modules import state
    cfg = load()
    cfg['remotes'] = list(remotes)
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


def getExternalApps():
    r"""外部应用反代挂载列表 (modules/external_proxy 用)。

    config.json 形如:
        "externalApps": {
          "yuanchuang": {
            "upstream": "http://127.0.0.1:8765",
            "prefix": "/yuanchuang",
            "mode": "namespace",
            "nav": {"label": "远窗", "icon": "plug"}
          }
        }
    mode: full (上游已配 web_prefix, 整站原样透传) | namespace (页面剥前缀,
    /_x/* 原样透传)。字段非法的条目跳过; 路由冲突在装配期由
    external_proxy.validatePrefix 拒绝。
    """
    raw = load().get('externalApps')
    if not isinstance(raw, dict):
        return []
    out = []
    for appId, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        appId = str(appId).strip()
        upstream = str(entry.get('upstream') or '').strip().rstrip('/')
        prefix = str(entry.get('prefix') or '').strip()
        mode = str(entry.get('mode') or 'namespace').strip().lower()
        if not appId or not prefix.startswith('/') or mode not in ('full', 'namespace'):
            continue
        if not upstream.startswith(('http://', 'https://')):
            continue
        nav = entry.get('nav') if isinstance(entry.get('nav'), dict) else {}
        label = str(nav.get('label') or appId).strip()[:24]
        icon = str(nav.get('icon') or 'plug').strip() or 'plug'
        out.append({'id': appId, 'upstream': upstream, 'prefix': prefix,
                    'mode': mode, 'label': label, 'icon': icon})
    return out
