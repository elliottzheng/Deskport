# -*- coding: utf-8 -*-
"""SSH SFTP 基座：蓝图、错误类型、SFTP 通道会话、远程条目转换。

从 ssh_sftp 拆出（2026-11-05）：原 503 行混了"远程文件 CRUD"与"批量传输打包"
两个变化理由。本文件只放两者共用的底层，不注册任何路由。

ops / transfer 两个模块都 import 本模块的 _sftpSession / _ensureRemoteDir。
"""
import posixpath
import stat as statMod
from contextlib import contextmanager

import paramiko

from modules import ssh_hosts as hosts
from modules import fsutil
from modules.ssh_base import SftpAuthError


def _openSftp(hostId):
    hostEntry = hosts.findHost(hostId)
    if hostEntry is None:
        raise SftpAuthError('主机不存在')
    try:
        client = hosts.getConnection(hostEntry)
    except paramiko.AuthenticationException:
        raise SftpAuthError('SSH 认证失败 (该主机可能需要键盘交互认证, SFTP 不支持)')
    return client.open_sftp()


@contextmanager
def _sftpSession(hostId):
    """一次请求一个 SFTP 通道，退出时必然关闭。"""
    sftp = _openSftp(hostId)
    try:
        yield sftp
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def remoteEntry(attr, path):
    """SFTPAttributes → 前端条目字典。"""
    isDir = statMod.S_ISDIR(attr.st_mode)
    return {'name': posixpath.basename(path.rstrip('/')) or '/',
            'path': path, 'type': 'folder' if isDir else 'file',
            'size': attr.st_size, 'mtime': int(attr.st_mtime),
            'perms': oct(statMod.S_IMODE(attr.st_mode))}


def ensureRemoteDir(sftp, path):
    """逐级创建远程目录，已存在的层级直接跳过（mkdir -p 语义）。"""
    path = posixpath.normpath(path or '')
    if path in ('', '.', '/'):
        return
    current = ''
    for part in path.strip('/').split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            current = posixpath.dirname(current)
            continue
        current += '/' + part
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def remoteExists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def uniqueRemotePath(sftp, path, isDir=False):
    """同名目标自动加 (1)/(2)，不覆盖。"""
    parent, name = posixpath.split(path)
    return fsutil.uniquePath(parent or '/', name, isDir=isDir, start=1,
                             pathmod=posixpath,
                             exists=lambda p: remoteExists(sftp, p))
