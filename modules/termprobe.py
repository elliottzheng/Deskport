# -*- coding: utf-8 -*-
"""终端会话的前台进程与工作目录探测（纯查询，无状态、无线程）。

从 termcore 拆出（2026-11-05）：termcore 原本同时承担 IO 后端、注册表、
生命周期线程、Win32 探测与 WS 帧协议五个变化理由，这里只留"探针"这一件。

探针只读 session 对象（duck typing），不 import termcore，因此 ssh/local
两侧与 wsproto 都可安全使用。
"""
import os

import psutil


def cwdLabelFor(path):
    """路径短名: 家目录返回空, 其余取末级目录 (盘根返回盘符)。"""
    if not path:
        return ''
    try:
        path = os.path.abspath(path)
        home = os.path.abspath(os.path.expanduser('~'))
        if os.path.normcase(path) == os.path.normcase(home):
            return ''
        base = os.path.basename(path.rstrip('\\/'))
        return base or path
    except (OSError, ValueError):
        return ''


def cwdLabel(session):
    return cwdLabelFor(getattr(session, 'cwd', None))


def foregroundInfo(session):
    """本地会话前台子进程: {name, cwd}; 空闲或 SSH 返回空字典。

    cwd 用于把"kimi/omp 在哪个项目里跑"显示到会话名上, 比 shell 名有意义。
    """
    if session.kind != 'local':
        return {}
    backend = getattr(session, 'backend', None)
    proc = getattr(backend, 'proc', None)
    if proc is None:
        return {}
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except (psutil.Error, OSError):
        return {}
    if not children:
        return {}

    def _ctime(p):
        try:
            return p.create_time()
        except (psutil.Error, OSError):
            return 0.0

    children.sort(key=_ctime)
    for child in reversed(children):
        try:
            name = child.name()
        except (psutil.Error, OSError):
            continue
        if not name or name.lower() in ('conhost.exe', 'openconsole.exe'):
            continue
        try:
            cwd = child.cwd()
        except (psutil.Error, OSError):
            cwd = ''
        return {'name': name, 'cwd': cwd or ''}
    return {}
