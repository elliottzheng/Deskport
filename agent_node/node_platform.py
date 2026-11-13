# -*- coding: utf-8 -*-
"""节点自报平台（批次 NODE-OS）：os / shells / defaultCwd / cwdRoots。

hub 派活前按 os 校验远端 cwd 语法（windows 要盘符/UNC，linux/darwin 要
根斜杠）、按 cwdRoots 校验远端 cwd 是否越界、往本机投附件按 os 选路径
分隔符、前端占位符跟着变。os/cwdRoots 未知的老 node 不校验——向后兼容。
零第三方依赖，与 agent_node 其它模块一样拷到目标机即可运行。
"""
import os
import platform

_SHELLS = {'windows': ['bash', 'powershell', 'pwsh'],
           'linux': ['bash', 'sh'],
           'darwin': ['bash', 'zsh', 'sh']}


def nodeOs():
    """本机 OS 声明：'windows' | 'linux' | 'darwin'（认不出的给空串）。"""
    name = (platform.system() or '').strip().lower()
    return name if name in _SHELLS else ''


def nodeShells():
    """本机可用 shell 清单（第一项 = 默认）。"""
    return list(_SHELLS.get(nodeOs(), []))


def defaultCwd():
    """会话的缺省工作目录（kimi 会话不传 cwd 时的落点）。"""
    return os.path.expanduser('~')


def cwdRoots():
    """本机允许的任务 cwd 根（workDirs + fallbackDir），与 hub 同口径。

    节点没有配置文件：按 hub config 的缺省约定算（home、home/Documents
    存在才纳入、fallback = home/默认工作空间）。hub 派活前拿它校验远端
    cwd 是否越界；空数组 = 未知（老 node 不报，hub 只查语法）。
    """
    home = os.path.expanduser('~')
    roots = [home]
    documents = os.path.join(home, 'Documents')
    if os.path.isdir(documents):
        roots.append(documents)
    roots.append(os.path.join(home, '默认工作空间'))
    out = []
    for path in roots:
        if path and path not in out:
            out.append(path)
    return out
