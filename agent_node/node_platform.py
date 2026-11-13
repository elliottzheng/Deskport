# -*- coding: utf-8 -*-
"""节点自报平台（批次 NODE-OS）：os / shells / defaultCwd。

hub 派活前按 os 校验远端 cwd 语法（windows 要盘符/UNC，linux/darwin 要
根斜杠）、往本机投附件按 os 选路径分隔符、前端占位符跟着变。os 未知的
老 node 不校验——向后兼容。零第三方依赖，与 agent_node 其它模块一样
拷到目标机即可运行。
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
