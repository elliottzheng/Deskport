# -*- coding: utf-8 -*-
"""SSH SFTP 远程文件 CRUD：列表/建目录/重命名/删除/权限/读/写，共 7 个接口。

从 ssh_sftp 拆出（2026-11-05）。批量传输与打包在 ssh_sftp_transfer，
共用的通道与工具在 ssh_sftp_base。
"""
import posixpath

import paramiko
from flask import jsonify, request

from modules import textio
from modules.ssh_base import blueprint
from modules.ssh_sftp_base import _sftpSession, ensureRemoteDir, remoteEntry

READ_MAX_BYTES = 20 * 1024 * 1024       # 超过只允许下载 (FM-18)
EDIT_MAX_BYTES = 1024 * 1024            # 超过 1MB 只读查看, 不提供编辑保存


@blueprint.route('/api/ssh/sftp/list', methods=['POST'])
def sftpList():
    data = request.json or {}
    hostId = data.get('hostId')
    path = data.get('path') or '.'
    entries = []
    try:
        with _sftpSession(hostId) as sftp:
            # 统一成绝对路径: '.' / 'Downloads' 这类相对路径按登录目录解析，
            # 否则"上一级"会错误地跳到 '/', 记忆功能也会存下歧义路径
            currentPath = sftp.normalize(path)
            homePath = sftp.normalize('.')
            attrs = sftp.listdir_attr(currentPath)
            base = currentPath if currentPath.endswith('/') else currentPath + '/'
            if base == '//':
                base = '/'
            for attr in attrs:
                # normpath 保证 '/' 下子项得到 '/home' 而非相对路径 'home' (相对路径会按
                # 登录 CWD 解析, 之前导致 SFTP 目录导航 404)
                full = posixpath.normpath(posixpath.join(base, attr.filename))
                entries.append(remoteEntry(attr, full))
    except FileNotFoundError:
        return jsonify({'error': '远程路径不存在'}), 404
    except paramiko.SSHException as e:
        return jsonify({'error': str(e)}), 502
    entries.sort(key=lambda e: (e['type'] != 'folder', e['name'].lower()))
    parent = posixpath.dirname(currentPath.rstrip('/')) or '/'
    return jsonify({'currentPath': currentPath, 'homePath': homePath,
                    'parentPath': parent, 'entries': entries})


@blueprint.route('/api/ssh/sftp/mkdir', methods=['POST'])
def sftpMkdir():
    """新建远程目录；逐级创建（mkdir -p 语义），已存在则视为成功。"""
    data = request.json or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({'error': '缺少 path'}), 400
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            ensureRemoteDir(sftp, path)
        return jsonify({'ok': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 400


@blueprint.route('/api/ssh/sftp/rename', methods=['POST'])
def sftpRename():
    data = request.json or {}
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            sftp.rename(data.get('oldPath'), data.get('newPath'))
        return jsonify({'ok': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 400


@blueprint.route('/api/ssh/sftp/delete', methods=['POST'])
def sftpDelete():
    data = request.json or {}
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            if data.get('isDir'):
                sftp.rmdir(data.get('path'))
            else:
                sftp.remove(data.get('path'))
        return jsonify({'ok': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 400


@blueprint.route('/api/ssh/sftp/chmod', methods=['POST'])
def sftpChmod():
    data = request.json or {}
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            sftp.chmod(data.get('path'), int(data.get('mode'), 8))
        return jsonify({'ok': True})
    except (OSError, ValueError) as e:
        return jsonify({'error': str(e)}), 400


@blueprint.route('/api/ssh/sftp/read', methods=['POST'])
def sftpRead():
    """简单查看/编辑: 读取受大小限制的远程文本。"""
    data = request.json or {}
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            attr = sftp.stat(data.get('path'))
            if attr.st_size > READ_MAX_BYTES:
                return jsonify({'error': '文件超过 20MB，仅可下载'}), 413   # FM-18
            readonly = attr.st_size > EDIT_MAX_BYTES
            with sftp.open(data.get('path'), 'rb') as f:
                raw = f.read(attr.st_size)
            content, encoding, lossy = textio.decodeSmart(raw)
            return jsonify({'ok': True, 'content': content, 'encoding': encoding,
                            'lossy': lossy, 'readonly': readonly,
                            'mtime': int(attr.st_mtime),
                            'path': data.get('path')})
    except (IOError, UnicodeDecodeError) as e:
        return jsonify({'error': str(e)}), 400


@blueprint.route('/api/ssh/sftp/write', methods=['POST'])
def sftpWrite():
    data = request.json or {}
    path = data.get('path')
    content = data.get('content') or ''
    try:
        payload = textio.encodeUsing(content, data.get('encoding') or 'utf-8')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            try:
                currentMtime = int(sftp.stat(path).st_mtime)
            except IOError:
                currentMtime = None
            if data.get('baseMtime') is not None and currentMtime is not None \
                    and currentMtime != int(data.get('baseMtime')):
                return jsonify({'error': '文件已被修改 (mtime 冲突), 请刷新后重试'}), 409
            with sftp.open(path, 'wb') as f:
                f.write(payload)
            return jsonify({'ok': True, 'encoding': (data.get('encoding') or 'utf-8').lower()})
    except IOError as e:
        return jsonify({'error': str(e)}), 500
