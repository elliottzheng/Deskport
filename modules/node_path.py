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
      执行点自己答，hub 探不到对端的盘。
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


def nodeOs(name):
    """执行点的 OS 声明；'' = 未知（cwd 不校验的那个向后兼容态）。

    local = 本机声明（agent_local.localHostEntry）；agent node / 联邦成员
    = 它自己上报（agent_nodes._refreshHealthModels 拉进缓存），没报到就是
    未知——老 node / 老成员照常放行。
    """
    from modules import agent_nodes, task_node
    wanted = str(name or '').strip()
    if task_node.isLocal(wanted):
        return normOs(_localEntry().get('os'))
    member = agent_nodes.findFederationMember(wanted)
    if member is not None:
        wanted = member.get('name') or wanted
    return normOs(_cachedPlatform(wanted).get('os'))


def nodeShells(name):
    """执行点声明的 shell 清单（未知 = 空列表）。"""
    from modules import task_node
    wanted = str(name or '').strip()
    if task_node.isLocal(wanted):
        entry = _localEntry()
        return [str(s) for s in entry.get('shells') or []]
    return [str(s) for s in _cachedPlatform(wanted).get('shells') or []]


def nodeDefaultCwd(name):
    """给 UI 当占位符的建议目录；拿不到就不给（返回 ''）。

    local 取本机 config.workDirs[0] / fallbackDir（本机 cwd 本来就必须在
    这些根之下）；远端取它自己的上报。
    """
    from modules import task_node
    wanted = str(name or '').strip()
    if task_node.isLocal(wanted):
        import config
        dirs = config.getWorkDirs()
        if dirs:
            return dirs[0]
        return config.getFallbackDir() or ''
    return str(_cachedPlatform(wanted).get('defaultCwd') or '')


def nodePlatform(name):
    """执行点平台声明 {os, shells, defaultCwd}（defaultCwd 可能为空串）。"""
    return {'os': nodeOs(name), 'shells': nodeShells(name),
            'defaultCwd': nodeDefaultCwd(name)}


def platformEntry(data):
    """node /health 与成员 /api/tasks/nodes 上报 → health 缓存条目。

    两边字段同名（models / defaultModel / modelStatus / usableModels），
    平台三件套（os / shells / defaultCwd）也走同一套键——agent_nodes
    的刷新线程把两类上报存进同一个缓存，nodeOs/nodeShells 直接读。
    """
    data = data if isinstance(data, dict) else {}
    cwd = data.get('defaultCwd')
    return {'models': data.get('models') or [],
            'default': data.get('defaultModel') or '',
            'modelStatus': data.get('modelStatus') or {},
            'usableModels': data.get('usableModels') or [],
            'os': normOs(data.get('os')),
            'shells': [str(s) for s in data.get('shells') or []],
            'defaultCwd': cwd if isinstance(cwd, str) else ''}


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
