# -*- coding: utf-8 -*-
"""执行点平台与路径语义：OS 声明解析、cwd 语法校验、按目标 OS 拼路径。

两个真实故障（2026-11-13）的根因都落在这里：
  1. 给 Windows 执行点 i7-4710MQ 派 cwd="/tmp"，Popen(cwd=...) 抛的
     FileNotFoundError 被上层归成「找不到 kimi 命令」——cwd 没有按执行点
     平台做前置校验；
  2. 往远端投附件拿本机 os.path 拼接：hub 是 Windows、成员是 Linux 时
     /home/me/project 被拼成 /home/me/project\\.deskport-inputs\\<taskId>。

约定:
    - os 取值 'windows' | 'linux' | 'darwin'；'' = 未知（老 node / 老成员
      还没上报）。未知时 cwd 不校验、拼接用调用方给的 fallback 模块——
      保持向后兼容，不替对端猜；
    - os 只是声明，不据此放宽任何权限（投递仍受 federationRoots 等约束）；
    - cwd 只查语法（盘符 / UNC / 根斜杠），不查存在性：远端目录在不在，
      执行点自己答，hub 探不到对端的盘；
    - cwdRoots（批次 B3）= 执行点自报的允许根。非空时 cwd 必须落在某个根
      之下（按目标 OS 的路径语义比）；空数组 = 未知，沿用只查语法。
"""
import os
import platform
import posixpath
import re

WINDOWS = 'windows'
POSIX_OS = ('linux', 'darwin')
KNOWN_OS = ('windows', 'linux', 'darwin')

_DRIVE_ABS = re.compile(r'^[A-Za-z]:[\\/]')
_UNIX_ABS = re.compile(r'^/')

# 各平台可用 shell（第一项 = 默认）。agent node 只做声明（它自己没有 exec
# 端点）；hub 本机 exec 的可用清单仍以 agent_local.localHostEntry 为准。
SHELLS = {'windows': ['bash', 'powershell', 'pwsh'],
          'linux': ['bash', 'sh'],
          'darwin': ['bash', 'zsh', 'sh']}


def normOs(value):
    """上报值 → 受支持的 os 名；不认识的一律 ''（未知）。"""
    text = str(value or '').strip().lower()
    return text if text in KNOWN_OS else ''


def thisOs():
    """本进程所在机器的 OS 声明（agent node 自报与 local 执行点同源）。"""
    return normOs(platform.system())


def shellsFor(osName):
    """该 os 的 shell 清单；未知给空列表（不猜）。"""
    return list(SHELLS.get(normOs(osName), []))


def pathModule(osName, fallback=posixpath):
    """按目标 OS 选 path 模块；未知（''）用调用方的 fallback（原行为）。"""
    name = normOs(osName)
    if name == WINDOWS:
        return os.path
    if name in POSIX_OS:
        return posixpath
    return fallback


def placeholder(osName):
    """UI 占位符：windows → C:\\work；linux/darwin → /home/me/project；未知 → /path。"""
    name = normOs(osName)
    if name == WINDOWS:
        return 'C:\\work'
    if name in POSIX_OS:
        return '/home/me/project'
    return '/path'


# ---------------------------------------------------------------- 执行点解析

def _cachedPlatform(name):
    """node /health 或成员 /api/tasks/nodes 上报的平台字段（health 缓存）。

    只读、不触发刷新：刷新由 nodeModelsFor（模型清单同源）的调用方发起
    ——凡是要看平台的请求（ nodes 列表 / 建任务 / 投附件）上游都已经问过
    模型，缓存必然已被刷过一轮。
    """
    from modules import agent_nodes
    with agent_nodes._healthModelsLock:
        info = (agent_nodes._healthModels.get('data') or {}).get(
            str(name or '').strip()) or {}
    return info if isinstance(info, dict) else {}


def _localEntry():
    """本机执行点的平台声明源（与 /api/agent/hosts 的 local 条目同源）。"""
    from modules.agent_local import localHostEntry
    return localHostEntry()


def _resolveName(name):
    """执行点名 → 平台缓存键：联邦成员按 config 条目解析到它的 name。

    调用方可以传成员的 id 或 name（findFederationMember 两种都认），而
    /health 与成员 nodes 列表的上报按 name 存进缓存——读之前先归一。
    """
    from modules import agent_nodes, task_node
    wanted = str(name or '').strip()
    if task_node.isLocal(wanted):
        return wanted
    member = agent_nodes.findFederationMember(wanted)
    if member is not None:
        return member.get('name') or wanted
    return wanted


def nodeOs(name):
    """执行点的 OS 声明；'' = 未知（cwd 不校验的那个向后兼容态）。

    local = 本机声明（agent_local.localHostEntry）；agent node / 联邦成员
    = 它自己上报（agent_nodes._refreshHealthModels 拉进缓存），没报到就是
    未知——老 node / 老成员照常放行。
    """
    from modules import task_node
    if task_node.isLocal(name):
        return normOs(_localEntry().get('os'))
    return normOs(_cachedPlatform(_resolveName(name)).get('os'))


def nodeShells(name):
    """执行点声明的 shell 清单（未知 = 空列表）。"""
    from modules import task_node
    if task_node.isLocal(name):
        entry = _localEntry()
        return [str(s) for s in entry.get('shells') or []]
    return [str(s) for s in _cachedPlatform(_resolveName(name)).get('shells') or []]


def nodeDefaultCwd(name):
    """给 UI 当占位符的建议目录；拿不到就不给（返回 ''）。

    local 取本机 config.workDirs[0] / fallbackDir（本机 cwd 本来就必须在
    这些根之下）；远端取它自己的上报。
    """
    from modules import task_node
    if task_node.isLocal(name):
        import config
        dirs = config.getWorkDirs()
        if dirs:
            return dirs[0]
        return config.getFallbackDir() or ''
    return str(_cachedPlatform(_resolveName(name)).get('defaultCwd') or '')


def localCwdRoots():
    """本机执行点声明的允许根（config.workDirs + fallbackDir，去空去重）。

    与 task_cwd.resolve 同一口径（本机 cwd 的越界判定就在这里）；顺序 =
    声明顺序，重复项只留第一个。
    """
    import config
    roots = []
    for item in list(config.getWorkDirs()) + [config.getFallbackDir()]:
        text = str(item or '').strip()
        if text and text not in roots:
            roots.append(text)
    return roots


def nodeCwdRoots(name):
    """执行点声明的允许根（字符串数组）；[] = 未知（不校验，只查语法）。

    local 取本机 config；agent node / 联邦成员取它自己上报（health 缓存，
    经 platformEntry 归一）。
    """
    from modules import task_node
    if task_node.isLocal(name):
        return localCwdRoots()
    return [str(r) for r in _cachedPlatform(_resolveName(name)).get('cwdRoots') or []]


def nodePlatform(name):
    """执行点平台声明 {os, shells, defaultCwd, cwdRoots}（后两者可能为空）。"""
    return {'os': nodeOs(name), 'shells': nodeShells(name),
            'defaultCwd': nodeDefaultCwd(name), 'cwdRoots': nodeCwdRoots(name)}


def platformEntry(data):
    """node /health 与成员 /api/tasks/nodes 上报 → health 缓存条目。

    两边字段同名（models / defaultModel / modelStatus / usableModels），
    平台三件套（os / shells / defaultCwd）与允许根 cwdRoots 也走同一套键
    ——agent_nodes 的刷新线程把两类上报存进同一个缓存，nodeOs/nodeShells/
    nodeCwdRoots 直接读。
    """
    data = data if isinstance(data, dict) else {}
    cwd = data.get('defaultCwd')
    roots = data.get('cwdRoots')
    return {'models': data.get('models') or [],
            'default': data.get('defaultModel') or '',
            'modelStatus': data.get('modelStatus') or {},
            'usableModels': data.get('usableModels') or [],
            'os': normOs(data.get('os')),
            'shells': [str(s) for s in data.get('shells') or []],
            'defaultCwd': cwd if isinstance(cwd, str) else '',
            'cwdRoots': ([str(r) for r in roots
                          if r is not None and str(r).strip()]
                         if isinstance(roots, list) else [])}


# ---------------------------------------------------------------- cwd 校验

def cwdError(nodeName, cwd):
    """按执行点 OS 校验 cwd 语法；None = 通过（含 os 未知时的放行）。

    只查语法不查存在性——远端目录在不在由执行点自己答。os 未知（老 node /
    老成员没上报）一律放行，保持向后兼容。本机执行点不走这里（task_cwd
    .resolve 管本机路径的存在性与越界）。
    """
    osName = nodeOs(nodeName)
    text = str(cwd or '').strip()
    if not osName or not text:
        return None
    if osName == WINDOWS:
        if _DRIVE_ABS.match(text) or text.startswith('\\\\') or text.startswith('//'):
            return None
        return ('执行点 %s 是 windows，cwd 必须是盘符路径（如 C:\\work）'
                '或 UNC（如 \\\\server\\share\\work），你给的是 %s'
                % (nodeName, text))
    if _UNIX_ABS.match(text):
        return None
    return ('执行点 %s 是 %s，cwd 必须是以 / 开头的绝对路径'
            '（如 /home/me/project 或 /srv/app），你给的是 %s'
            % (nodeName, osName, text))


def _underRoot(pm, path, root):
    """path 是否在 root 之下（按目标 OS 的 normcase/normpath 比）。

    Windows 大小写不敏感（C:\\WORK\\a 命中 C:\\work）；POSIX 敏感。
    根自身算命中；前缀必须落在目录边界上（C:\\work 不命中 C:\\workx）。
    """
    p = pm.normcase(pm.normpath(str(path)))
    r = pm.normcase(pm.normpath(str(root)))
    if p == r:
        return True
    prefix = r if r.endswith(pm.sep) else r + pm.sep
    return p.startswith(prefix)


def cwdRootsError(nodeName, cwd, realpath=None):
    """cwd 是否落在执行点声明的允许根之下；None = 通过。

    cwdRoots 为空（老 node / 老成员没上报）= 未知，不校验——与 cwdError
    的向后兼容语义一致，两者叠加。本机执行点不走这里（task_cwd.resolve
    管本机路径的存在性与越界）。

    realpath: 可选的解析函数（投递时由通道给出对端 fs 上的真实路径）——
    建卡时只做词法比较，投递前再按 realpath 复核一次，防建卡后目录被换成
    软链的 TOCTOU（hub 探不到对端的盘，软链只有对端能解）。
    """
    roots = nodeCwdRoots(nodeName)
    text = str(cwd or '').strip()
    if not roots or not text:
        return None
    pm = pathModule(nodeOs(nodeName), os.path)
    resolved = str(realpath(text) if realpath else text) or text
    if any(_underRoot(pm, resolved, root) for root in roots):
        return None
    return ('cwd %s 不在执行点 %s 允许的根下（允许：%s）'
            % (text, nodeName, ', '.join(roots)))
