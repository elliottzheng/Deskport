# -*- coding: utf-8 -*-
"""SSH SFTP 批量传输与打包：下载/打包下载/上传(三种)/与本机互传，共 7 个接口。

从 ssh_sftp 拆出（2026-11-05）。远程文件 CRUD 在 ssh_sftp_ops，
共用的通道与工具在 ssh_sftp_base。
"""
import io
import json as jsonMod
import os
import posixpath
import re
import zipfile
from urllib.parse import quote

import paramiko
from flask import Response, jsonify, request, send_file

from modules.ssh_base import blueprint, log
from modules.ssh_sftp_base import (_openSftp, _sftpSession, ensureRemoteDir,
                                   uniqueRemotePath)

ZIP_SIZE_BUDGET = 2 * 1024 * 1024 * 1024   # 打包内存上限 2GB，超出报 413


class ZipBudgetExceeded(Exception):
    pass


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

    ext = posixpath.splitext(name)[1]
    asciiName = 'download' + ext if ext and len(ext) <= 12 else 'download'
    headers = {'Content-Disposition':
               "attachment; filename=\"%s\"; filename*=UTF-8''%s"
               % (asciiName, quote(name, safe=''))}
    return Response(generate(), mimetype='application/octet-stream',
                    headers=headers)


def _zipRemoteTree(sftp, remotePath, arcPrefix, zf, budget):
    """递归把远程文件/目录写进 zip；budget 是剩余字节数的 [n] 引用。"""
    import stat as statMod
    attr = sftp.stat(remotePath)
    if statMod.S_ISDIR(attr.st_mode):
        parent = remotePath.rstrip('/') or '/'
        for entry in sftp.listdir_attr(parent):
            child = posixpath.join(parent, entry.filename)
            isDir = statMod.S_ISDIR(entry.st_mode)
            arc = arcPrefix + '/' + entry.filename
            if isDir:
                _zipRemoteTree(sftp, child, arc, zf, budget)
            else:
                budget[0] -= entry.st_size or 0
                _zipAddFile(sftp, child, arc, zf, budget)
    else:
        budget[0] -= attr.st_size or 0
        _zipAddFile(sftp, remotePath, arcPrefix, zf, budget)


def _zipAddFile(sftp, remotePath, arc, zf, budget):
    if budget[0] < 0:
        raise ZipBudgetExceeded('打包总量超过 2GB 上限，请分批选择')
    with sftp.open(remotePath, 'rb') as src, zf.open(arc, 'w') as dst:
        while True:
            chunk = src.read(256 * 1024)
            if not chunk:
                break
            dst.write(chunk)


@blueprint.route('/api/ssh/sftp/download-zip')
def sftpDownloadZip():
    """多选/文件夹打包下载: 服务端拉取远程文件写入 zip 内存流后整体下发。"""
    hostId = request.args.get('hostId')
    try:
        paths = [str(p) for p in jsonMod.loads(request.args.get('paths') or '[]')]
    except ValueError:
        return jsonify({'error': 'paths 必须是 JSON 数组'}), 400
    paths = [p for p in paths if p]
    if not paths:
        return jsonify({'error': '缺少路径'}), 400

    buf = io.BytesIO()
    budget = [ZIP_SIZE_BUDGET]
    try:
        with _sftpSession(hostId) as sftp, \
                zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            used = set()
            for path in paths:
                base = posixpath.basename(path.rstrip('/')) or path
                arc, i = base, 1
                while arc in used:
                    arc = '%s(%d)' % (base, i)
                    i += 1
                used.add(arc)
                _zipRemoteTree(sftp, path, arc, zf, budget)
    except ZipBudgetExceeded as e:
        return jsonify({'error': str(e)}), 413
    except IOError as e:
        return jsonify({'error': str(e)}), 404
    except paramiko.SSHException as e:
        return jsonify({'error': str(e)}), 502

    buf.seek(0)
    stamp = request.args.get('stamp') or 'sftp'
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='sftp-%s.zip' % re.sub(r'[^0-9A-Za-z_-]', '', stamp)[:40])


def _uploadLocalPath(sftp, localPath, remoteDir, stats):
    """递归上传本地文件/文件夹; 同名自动加 (1)/(2)。"""
    name = os.path.basename(localPath.rstrip('\\/'))
    if not name:
        return
    if os.path.isdir(localPath):
        target = uniqueRemotePath(sftp, posixpath.join(remoteDir, name), True)
        sftp.mkdir(target)
        for entry in sorted(os.scandir(localPath), key=lambda e: e.name.lower()):
            if entry.is_symlink():
                continue
            _uploadLocalPath(sftp, entry.path, target, stats)
        return
    if not os.path.isfile(localPath):
        return
    target = uniqueRemotePath(sftp, posixpath.join(remoteDir, name), False)
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
                    ensureRemoteDir(sftp, posixpath.dirname(target))
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
