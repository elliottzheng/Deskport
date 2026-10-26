# -*- coding: utf-8 -*-
"""SSH SFTP：列表/读写/上传下载/权限，共 12 个接口。"""
import os
import posixpath
import re
from contextlib import contextmanager

import paramiko
from flask import Response, jsonify, request

from modules import ssh_hosts as hosts
from modules import fsutil, textio
from modules.ssh_base import blueprint, log, SftpAuthError


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
    """一次请求一个 SFTP 通道，退出时必然关闭（12 个端点共用）。"""
    sftp = _openSftp(hostId)
    try:
        yield sftp
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def _remoteEntry(attr, path):
    import stat as statMod
    isDir = statMod.S_ISDIR(attr.st_mode)
    return {'name': posixpath.basename(path.rstrip('/')) or '/',
            'path': path, 'type': 'folder' if isDir else 'file',
            'size': attr.st_size, 'mtime': int(attr.st_mtime),
            'perms': oct(statMod.S_IMODE(attr.st_mode))}


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
                entries.append(_remoteEntry(attr, full))
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
            _ensureRemoteDir(sftp, path)
        return jsonify({'ok': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 400


def _ensureRemoteDir(sftp, path):
    """逐级创建远程目录，已存在的层级直接跳过。"""
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


@blueprint.route('/api/ssh/sftp/download')
def sftpDownload():
    """网页端下载: 远程文件流式转发给浏览器。"""
    hostId = request.args.get('hostId')
    path = request.args.get('path') or ''
    if not path:
        return jsonify({'error': '缺少路径'}), 400
    name = posixpath.basename(path.rstrip('/'))
    try:
        sftp = _openSftp(hostId)
        remote = sftp.open(path, 'rb')
    except IOError as e:
        # 诊断: 列出父目录, 确认 SFTP 视角下文件是否真的不存在
        try:
            parent = posixpath.dirname(path)
            with _sftpSession(hostId) as diag:
                listing = [x.filename for x in diag.listdir_attr(parent)][:20]
            log.warning('download miss path=%s | 父目录内容: %s', path, listing)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 404

    def generate():
        try:
            while True:
                chunk = remote.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            remote.close()
            try:
                sftp.close()
            except Exception:
                pass

    headers = {'Content-Disposition':
               "attachment; filename*=UTF-8''" + name}
    return Response(generate(), mimetype='application/octet-stream',
                    headers=headers)


def _remoteExists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def _uniqueRemotePath(sftp, path, isDir=False):
    parent, name = posixpath.split(path)
    return fsutil.uniquePath(parent or '/', name, isDir=isDir, start=1,
                             pathmod=posixpath,
                             exists=lambda p: _remoteExists(sftp, p))


def _uploadLocalPath(sftp, localPath, remoteDir, stats):
    """递归上传本地文件/文件夹; 同名自动加 (1)/(2)。"""
    name = os.path.basename(localPath.rstrip('\\/'))
    if not name:
        return
    if os.path.isdir(localPath):
        target = _uniqueRemotePath(sftp, posixpath.join(remoteDir, name), True)
        sftp.mkdir(target)
        for entry in sorted(os.scandir(localPath), key=lambda e: e.name.lower()):
            if entry.is_symlink():
                continue
            _uploadLocalPath(sftp, entry.path, target, stats)
        return
    if not os.path.isfile(localPath):
        return
    target = _uniqueRemotePath(sftp, posixpath.join(remoteDir, name), False)
    sftp.put(localPath, target, confirm=True)
    try:
        st = os.stat(localPath)
        sftp.utime(target, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    stats['files'] += 1
    stats['bytes'] += os.path.getsize(localPath)
    stats['items'].append(target)


@blueprint.route('/api/ssh/sftp/upload-local', methods=['POST'])
def sftpUploadLocal():
    """把本机文件/文件夹直接上传到指定 SSH 主机的远程目录。

    与 multipart 上传不同, 文件不经过浏览器, 由 web 进程读取本地路径后
    通过 SFTP 写入; 文件夹递归上传。同名目标自动加序号, 不覆盖。
    """
    data = request.get_json(force=True, silent=True) or {}
    hostId = data.get('hostId') or ''
    remoteDir = (data.get('remoteDir') or '.').strip() or '.'
    paths = data.get('paths') or []
    if not hostId:
        return jsonify({'error': '缺少主机'}), 400
    if not isinstance(paths, list) or not paths:
        return jsonify({'error': '没有选择文件'}), 400
    if remoteDir == '~':
        remoteDir = '.'
    for path in paths:
        if not isinstance(path, str) or not os.path.exists(path):
            return jsonify({'error': '文件不存在: %s' % path}), 400
        norm = os.path.abspath(path)
        if not re.match(r'^[A-Za-z]:[\\/]', norm):
            return jsonify({'error': '只允许本机磁盘路径: %s' % path}), 403
        if re.match(r'^[A-Za-z]:\\?$', norm):
            return jsonify({'error': '不能上传整个磁盘: %s' % norm}), 400
    try:
        with _sftpSession(hostId) as sftp:
            try:
                sftp.stat(remoteDir)
            except IOError:
                return jsonify({'error': '远程目录不存在: %s' % remoteDir}), 400
            stats = {'files': 0, 'bytes': 0, 'items': [], 'errors': []}
            for path in paths:
                try:
                    _uploadLocalPath(sftp, os.path.abspath(path), remoteDir, stats)
                except (IOError, OSError, paramiko.SSHException) as e:
                    stats['errors'].append({'path': path, 'error': str(e)})
    except SftpAuthError as e:
        return jsonify({'error': str(e)}), 400
    except paramiko.SSHException as e:
        return jsonify({'error': 'SSH 连接失败: %s' % e}), 502
    log.info('sftp upload-local host=%s dir=%s files=%d bytes=%d errors=%d',
             hostId, remoteDir, stats['files'], stats['bytes'], len(stats['errors']))
    return jsonify({'ok': not stats['errors'], 'remoteDir': remoteDir, **stats})


@blueprint.route('/api/ssh/sftp/upload', methods=['POST'])
def sftpUpload():
    """网页端上传: multipart 文件 → SFTP put (流式, 不落盘中转)。"""
    hostId = request.form.get('hostId')
    remoteDir = request.form.get('remoteDir') or '.'
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'error': '未选择文件'}), 400
    remotePath = posixpath.join(remoteDir, file.filename)
    try:
        with _sftpSession(hostId) as sftp:
            with sftp.open(remotePath, 'wb') as dst:
                while True:
                    chunk = file.stream.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        return jsonify({'ok': True, 'path': remotePath})
    except IOError as e:
        return jsonify({'error': str(e)}), 500


@blueprint.route('/api/ssh/sftp/upload-batch', methods=['POST'])
def sftpUploadBatch():
    """多选/整夹上传: 一次请求完成 N 个文件（含目录结构），共用一个 SFTP 通道。

    表单: hostId / remoteDir / files[](多个) / paths[](与 files 对齐的相对路径)。
    """
    hostId = request.form.get('hostId')
    remoteDir = request.form.get('remoteDir') or '.'
    files = request.files.getlist('files')
    rels = request.form.getlist('paths')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '未选择文件'}), 400
    uploaded, failed = [], []
    try:
        with _sftpSession(hostId) as sftp:
            baseDir = sftp.normalize(remoteDir)
            for index, file in enumerate(files):
                rel = (rels[index] if index < len(rels) else '') or file.filename
                rel = posixpath.normpath(rel.replace('\\', '/')).lstrip('/')
                if not rel or rel.startswith('..'):
                    failed.append({'name': file.filename, 'error': '非法路径'})
                    continue
                target = posixpath.join(baseDir, rel)
                try:
                    _ensureRemoteDir(sftp, posixpath.dirname(target))
                    with sftp.open(target, 'wb') as dst:
                        while True:
                            chunk = file.stream.read(64 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                    uploaded.append(target)
                except IOError as e:
                    failed.append({'name': file.filename, 'error': str(e)})
    except paramiko.SSHException as e:
        failed.append({'error': '连接中断: %s' % e})
    return jsonify({'ok': not failed, 'uploaded': uploaded, 'failed': failed})


@blueprint.route('/api/ssh/sftp/read', methods=['POST'])
def sftpRead():
    """简单查看/编辑: 读取 ≤1MB 的远程文本。"""
    data = request.json or {}
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            attr = sftp.stat(data.get('path'))
            if attr.st_size > 20 * 1024 * 1024:
                return jsonify({'error': '文件超过 20MB，仅可下载'}), 413   # FM-18
            readonly = attr.st_size > 1024 * 1024   # >1MB 只读查看, 不提供编辑保存
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


@blueprint.route('/api/ssh/sftp/to-local', methods=['POST'])
def sftpToLocal():
    """SFTP 下载 → 到电脑: 远程文件转存到本机目录 (与文件管理联动)。"""
    data = request.json or {}
    remotePath = data.get('remotePath') or ''
    localDir = data.get('localDir') or os.path.expanduser('~/Downloads')
    if not remotePath:
        return jsonify({'error': '缺少远程路径'}), 400
    if not os.path.isdir(localDir):
        return jsonify({'error': '本机目录不存在'}), 400
    localPath = os.path.join(localDir, posixpath.basename(remotePath))
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            sftp.get(remotePath, localPath)
        return jsonify({'ok': True, 'localPath': localPath})
    except IOError as e:
        return jsonify({'error': str(e)}), 500


@blueprint.route('/api/ssh/sftp/from-local', methods=['POST'])
def sftpFromLocal():
    """从本机文件系统推送文件到 SSH 主机。"""
    data = request.json or {}
    localPath = data.get('localPath') or ''
    remoteDir = data.get('remoteDir') or '.'
    if not localPath or not os.path.isfile(localPath):
        return jsonify({'error': '本机文件不存在'}), 404
    remotePath = posixpath.join(remoteDir, os.path.basename(localPath))
    try:
        with _sftpSession(data.get('hostId')) as sftp:
            sftp.put(localPath, remotePath)
        return jsonify({'ok': True, 'remotePath': remotePath})
    except IOError as e:
        return jsonify({'error': str(e)}), 500
