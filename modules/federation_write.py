# -*- coding: utf-8 -*-
"""联邦写能力的两条边界：根白名单 + 上传上限（不含转发逻辑）。

只在成员端、只对联邦请求生效（``X-Deskport-From`` 存在）。本机文件页
不走这里，仍保持全盘能力。空根 = 一律拒绝，不是"不校验"。

归一化要点（Windows）:
    - 先剥 ``\\\\?\\`` / ``\\\\?\\UNC\\`` 长路径前缀，防前缀绕过；
    - 本地路径走 realpath：解析软链与 ``..``，软链逃逸在比较时自动出界；
    - **UNC 只做文本归一**，绝不 realpath：离线共享上一次 realpath 实测 41 秒，
      而 roots() 每个设置页请求 / 每个联邦写请求都要跑一遍，等于把网络超时
      放大成全站卡顿。代价是 UNC 共享内部的软链/junction 不再解析，判界靠
      文本前缀（normpath 折叠 ``..``，折叠后越过共享边界的路径自然出界）；
    - 比较用 normcase + normpath，盘符与 UNC 共享名大小写归一；
    - 盘符相对路径（``C:foo``）、设备路径（``\\\\.\\``）、相对路径一律非法。

    normcase/normpath 是**成员自己**的路径语义：本模块只跑在成员进程里
    （``X-Deskport-From`` 存在时的本机请求），按成员自己的 OS 折叠——
    hub 是 Windows、成员是 Linux 时不按 hub 的规则归一。中枢侧不 import
    本模块判断对端路径（往成员投附件按节点自报 os 选分隔符，见
    modules/node_path.py）。
"""
import os
import re

import config

DEVICE_PREFIX = re.compile(r'^\\\\\.\\')
_DRIVE_RELATIVE = re.compile(r'^[A-Za-z]:(?![\\/])')
_LONG_UNC = re.compile(r'^\\\\\?\\UNC\\', re.I)
_LONG_PREFIX = re.compile(r'^\\\\\?\\')

DEFAULT_UPLOAD_MAX_MB = 2048
MAX_ROOTS = 20


def _stripLong(text):
    if _LONG_UNC.match(text):
        return '\\\\' + text[len('\\\\?\\UNC\\'):]
    if _LONG_PREFIX.match(text):
        return text[4:]
    return text


def _absolute(text):
    return os.path.isabs(text) or text.startswith('\\\\')


def _isUnc(text):
    # Windows 同时接受反斜杠与正斜杠两种 UNC 写法（\\nas\team 与 //nas/team）
    return text.startswith('\\\\') or text.startswith('//')


def _normalizeUnc(text):
    """UNC 的文本归一：normpath 折叠 ``..`` / 多余分隔符，重建 ``\\\\server\\share\\...``。

    不调 realpath（见模块说明）。共享名缺失（``\\\\server``）或等价于设备路径
    （``//./PhysicalDrive0``）视为非法。
    """
    norm = os.path.normpath(text)
    if _LONG_PREFIX.match(norm):
        if not _LONG_UNC.match(norm):
            return ''
        norm = os.path.normpath(_stripLong(norm))
    if DEVICE_PREFIX.match(norm) or not norm.startswith('\\\\'):
        return ''
    parts = [p for p in norm.split('\\') if p]
    if len(parts) < 2:
        return ''
    return '\\\\' + '\\'.join(parts)


def normalizePath(raw):
    """入参 → 绝对路径（UNC 为文本归一，其余 realpath）；非法返回空串。"""
    text = str(raw or '').strip().strip('"')
    if not text:
        return ''
    text = os.path.expanduser(text)
    if DEVICE_PREFIX.match(text):
        return ''
    text = _stripLong(text)
    if not _absolute(text) or _DRIVE_RELATIVE.match(text):
        return ''
    if _isUnc(text):
        return _normalizeUnc(text)
    try:
        real = os.path.realpath(text)
    except (OSError, ValueError):
        return ''
    return _stripLong(real)


def isExistingFile(path):
    """存在但不是目录（本地路径）；不存在或 UNC 返回 False。

    UNC 不做存在性探测：离线共享一次 stat 同样要等网络超时。不存在的路径
    放行，是为了支持「目录还没建」和临时离线的共享。
    """
    text = str(path or '')
    if not text or _isUnc(text):
        return False
    try:
        return os.path.exists(text) and not os.path.isdir(text)
    except OSError:
        return False


def isUnder(path, root):
    """大小写不敏感的前缀判断；UNC / 盘符根都适用（不依赖 commonpath）。"""
    p = os.path.normcase(os.path.normpath(str(path or '')))
    r = os.path.normcase(os.path.normpath(str(root or '')))
    if not p or not r:
        return False
    if p == r:
        return True
    return p.startswith(r.rstrip('\\/') + os.sep)


def roots():
    """config.federationRoots 归一，去空、去重、截断。

    UNC 项只做文本归一（不 realpath），因此重复调用不会触碰网络。
    """
    raw = config.load().get('federationRoots')
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        root = normalizePath(item) if isinstance(item, str) else ''
        if root and root not in out:
            out.append(root)
        if len(out) >= MAX_ROOTS:
            break
    return out


def uploadLimitMB():
    """成员端对联邦上传声明的单文件上限(MB)；缺省 2048，非法回退缺省。"""
    value = config.load().get('federationUploadMaxMB')
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_MAX_MB
    return value if value > 0 else DEFAULT_UPLOAD_MAX_MB


def checkPaths(paths):
    """校验一组路径；返回 (ok, 说明)。空列表也拒绝（宁缺毋滥）。

    UNC 与本地路径走同一条比较，区别只在归一化：UNC 不 realpath，
    因此对离线共享做判定也不会阻塞。
    """
    allowed = roots()
    if not allowed:
        return False, '成员端未配置 federationRoots（联邦写操作一律 403）'
    if not paths:
        return False, '缺少可校验的路径'
    for raw in paths:
        path = normalizePath(raw)
        if not path:
            return False, '路径不合法: %s' % (raw or '(空)')
        if not any(isUnder(path, root) for root in allowed):
            return False, '路径不在 federationRoots 内: %s' % path
    return True, ''