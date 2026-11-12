# -*- coding: utf-8 -*-
"""智能体 SFTP 写操作: delete / mkdir / rename（与 list/get/put 同蓝图）。

给跨机附件投递（§7）与联邦内部维护用，不替代文件页的回收站。
错误码沿用 agent_sftp: 403 权限 / 404 不存在 / 409 已存在 / 400 用法。

契约:
    POST /api/agent/sftp/delete {host, path, recursive?}
        文件/软链: remove；目录: 空目录 rmdir，recursive=true 才整棵删。
    POST /api/agent/sftp/mkdir  {host, path, parents?}
        已存在 → 409；parents=true 逐级创建（已存在的父级跳过）。
    POST /api/agent/sftp/rename {host, from, to, overwrite?}
        目标已存在且 overwrite=false → 409；overwrite=true 才覆盖。
"""
import os
import shutil
import stat as statMod

from flask import jsonify, request

from modules import events
from modules.agent_api import _agentRequired, blueprint
from modules.agent_sftp import (_mapSftpError, _normalize, _sftpFor,
                                _sftpMakedirs, isLocal)


def _audit(op, target, result, detail=''):
    events.log('agent.sftp.' + op, target, result, detail, request.remote_addr or '')


def _localEnabled():
    from modules.agent_local import localEnabled
    return localEnabled()


def _localPath(raw):
    from modules.agent_local import normalizeLocalPath
    return normalizeLocalPath(os.path.expanduser(str(raw or '')))


def _bool(data, key):
    return data.get(key) is True


# ---------------------------------------------------------------- 本机

def _deleteLocal(path, recursive):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
        return 1
    if os.path.isdir(path):
        if recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
        return 1
    raise FileNotFoundError(path)


def _mkdirLocal(path, parents):
    if os.path.exists(path):
        raise FileExistsError(path)
    if parents:
        os.makedirs(path, exist_ok=False)
    else:
        os.mkdir(path)


def _renameLocal(src, dst, overwrite):
    if os.path.exists(dst) and not overwrite:
        raise FileExistsError(dst)
    if overwrite:
        os.replace(src, dst)
    else:
        os.rename(src, dst)


# ---------------------------------------------------------------- SSH

def _rmtree(sftp, path):
    for attr in sftp.listdir_attr(path):
        child = path.rstrip('/') + '/' + attr.filename
        if statMod.S_ISDIR(attr.st_mode):
            _rmtree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def _deleteSftp(sftp, path, recursive):
    attr = sftp.stat(path)
    if statMod.S_ISDIR(attr.st_mode):
        if recursive:
            _rmtree(sftp, path)
        else:
            sftp.rmdir(path)
    else:
        sftp.remove(path)


def _mkdirSftp(sftp, path, parents):
    try:
        sftp.stat(path)
    except IOError:
        pass
    else:
        raise FileExistsError(path)
    if parents:
        _sftpMakedirs(sftp, path)
    else:
        sftp.mkdir(path)


def _renameSftp(sftp, src, dst, overwrite):
    if overwrite:
        try:
            sftp.posix_rename(src, dst)
            return
        except IOError:
            sftp.rename(src, dst)
            return
    try:
        sftp.stat(dst)
    except IOError:
        pass
    else:
        raise FileExistsError(dst)
    sftp.rename(src, dst)


_EMPTY_ERRNOS = {39, 41}   # ENOTEMPTY / EEXIST（Windows 目录非空）


def _emptyDirError(exc):
    if getattr(exc, 'errno', None) in _EMPTY_ERRNOS:
        return True
    return 'not empty' in str(exc).lower() or '目录不是空的' in str(exc)


# ---------------------------------------------------------------- 路由

@blueprint.route('/api/agent/sftp/delete', methods=['POST'])
def agentSftpDelete():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    raw = str(data.get('path') or '').strip()
    if not raw:
        return jsonify({'error': '缺少 path'}), 400
    recursive = _bool(data, 'recursive')
    try:
        if isLocal(data.get('host')):
            if not _localEnabled():
                return jsonify({'error': '主机不存在'}), 404
            path = _localPath(raw)
            count = _deleteLocal(path, recursive)
        else:
            _host, sftp, err = _sftpFor(data.get('host'))
            if err:
                return jsonify({'error': err}), 404
            path = _normalize(sftp, raw)
            _deleteSftp(sftp, path, recursive)
            count = 1
    except FileNotFoundError:
        _audit('delete', raw, 'fail', '不存在')
        return jsonify({'error': '路径不存在: %s' % raw}), 404
    except OSError as e:
        if isinstance(e, FileExistsError) or _emptyDirError(e):
            _audit('delete', raw, 'fail', '目录非空')
            return jsonify({'error': '目录非空（如需整棵删除加 recursive:true）'}), 409
        status, msg = _mapSftpError(e)
        _audit('delete', raw, 'fail', msg[:160])
        return jsonify({'error': msg}), status
    _audit('delete', path, 'ok',
           'recursive=%s' % ('true' if recursive else 'false'))
    return jsonify({'ok': True, 'path': path, 'count': count})


@blueprint.route('/api/agent/sftp/mkdir', methods=['POST'])
def agentSftpMkdir():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    raw = str(data.get('path') or '').strip()
    if not raw:
        return jsonify({'error': '缺少 path'}), 400
    parents = _bool(data, 'parents') or _bool(data, 'mkdirs')
    try:
        if isLocal(data.get('host')):
            if not _localEnabled():
                return jsonify({'error': '主机不存在'}), 404
            path = _localPath(raw)
            _mkdirLocal(path, parents)
        else:
            _host, sftp, err = _sftpFor(data.get('host'))
            if err:
                return jsonify({'error': err}), 404
            path = _normalize(sftp, raw)
            _mkdirSftp(sftp, path, parents)
    except FileExistsError:
        _audit('mkdir', raw, 'fail', '已存在')
        return jsonify({'error': '路径已存在: %s' % raw}), 409
    except OSError as e:
        status, msg = _mapSftpError(e)
        _audit('mkdir', raw, 'fail', msg[:160])
        return jsonify({'error': msg}), status
    _audit('mkdir', path, 'ok', 'parents=%s' % ('true' if parents else 'false'))
    return jsonify({'ok': True, 'path': path})


@blueprint.route('/api/agent/sftp/rename', methods=['POST'])
def agentSftpRename():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    src = str(data.get('from') or data.get('oldPath') or '').strip()
    dst = str(data.get('to') or data.get('newPath') or '').strip()
    if not src or not dst:
        return jsonify({'error': '缺少 from / to'}), 400
    overwrite = _bool(data, 'overwrite')
    try:
        if isLocal(data.get('host')):
            if not _localEnabled():
                return jsonify({'error': '主机不存在'}), 404
            srcPath, dstPath = _localPath(src), _localPath(dst)
            _renameLocal(srcPath, dstPath, overwrite)
        else:
            _host, sftp, err = _sftpFor(data.get('host'))
            if err:
                return jsonify({'error': err}), 404
            srcPath = _normalize(sftp, src)
            dstPath = _normalize(sftp, dst)
            _renameSftp(sftp, srcPath, dstPath, overwrite)
    except FileExistsError:
        _audit('rename', src, 'fail', '目标已存在')
        return jsonify({'error': '目标已存在: %s（overwrite:true 可覆盖）' % dst}), 409
    except OSError as e:
        status, msg = _mapSftpError(e)
        _audit('rename', src, 'fail', msg[:160])
        return jsonify({'error': msg}), status
    _audit('rename', src, 'ok', '→ %s overwrite=%s'
           % (dstPath, 'true' if overwrite else 'false'))
    return jsonify({'ok': True, 'from': srcPath, 'to': dstPath})