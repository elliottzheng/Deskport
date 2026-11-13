# -*- coding: utf-8 -*-
"""联邦写能力的两条边界：根白名单 + 上传上限（不含转发逻辑）。

只在成员端、只对联邦请求生效（``X-Deskport-From`` 存在）。本机文件页
不走这里，仍保持全盘能力。空根 = 一律拒绝，不是"不校验"。

归一化要点（Windows）:
    - 先剥 ``\\\\?\\`` / ``\\\\?\\UNC\\`` 长路径前缀，再 realpath，防前缀绕过；
    - realpath 解析软链与 ``..``，软链逃逸在比较时自动出界；
    - 比较用 normcase + normpath，盘符与 UNC 共享名大小写归一；
    - 盘符相对路径（``C:foo``）、设备路径（``\\\\.\\``）、相对路径一律非法。
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


def normalizePath(raw):
    """入参 → realpath 绝对路径；非法返回空串。"""
    text = str(raw or '').strip().strip('"')
    if not text:
        return ''
    text = os.path.expanduser(text)
    if DEVICE_PREFIX.match(text):
        return ''
    text = _stripLong(text)
    if not _absolute(text) or _DRIVE_RELATIVE.match(text):
        return ''
    try:
        real = os.path.realpath(text)
    except (OSError, ValueError):
        return ''
    return _stripLong(real)


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
    """config.federationRoots 经 realpath 归一，去空、去重、截断。"""
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
    """校验一组路径；返回 (ok, 说明)。空列表也拒绝（宁缺毋滥）。"""
    allowed = roots()
    if not allowed:
        return False, '成员端未配置 federationRoots（联邦写操作一律 403）'
    if not paths:
        return False, '缺少可校验的路径'
    uncRoots = [os.path.normcase(os.path.normpath(root))
                for root in allowed if _stripLong(root).startswith('\\\\')]
    for raw in paths:
        text = str(raw or '')
        if _stripLong(text).startswith('\\\\'):
            # 先做文本前缀判断，避免对任意 UNC 调 realpath 触发网络等待
            norm = os.path.normcase(os.path.normpath(_stripLong(text)))
            if not any(norm.startswith(root.rstrip('\\/') + '\\')
                       for root in uncRoots):
                return False, 'UNC 路径不在 federationRoots 内: %s' % text
        path = normalizePath(raw)
        if not path:
            return False, '路径不合法: %s' % (raw or '(空)')
        if not any(isUnder(path, root) for root in allowed):
            return False, '路径不在 federationRoots 内: %s' % path
    return True, ''