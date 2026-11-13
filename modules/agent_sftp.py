# -*- coding: utf-8 -*-
"""智能体 SFTP：list/get/put，同一套错误码语义与分块读写。

与 agent_sessions 同模式挂在 agent_api 的蓝图上（app.py 显式登记）。

约定:
    - 错误码: 权限不足 403 / 不存在 404 / 用法错误(如对目录 get) 400 / 超 413；
    - list 返回 type(file/dir/symlink/fifo/…)、mode、symlink 的 target，
      path 统一为远端规范化的绝对路径（`~` 与相对路径都解析）；
    - get 支持 offset/length 分块读取大文件（整读上限 MAX_FILE_BYTES）；
    - put 严格校验 base64（坏编码直接 400，绝不静默写空文件），可选 sha256
      校验，写临时文件后 rename 原子落盘，覆盖时保留原文件权限。
"""
import base64
import hashlib
import secrets
import stat as statMod

from flask import jsonify, request

from modules.agent_api import (auditAgentAction, requireAgentScope, _resolveHost,
                               blueprint)
from modules.fsutil import decodeBase64
from modules.ssh_sftp import _openSftp

MAX_FILE_BYTES = 8 * 1024 * 1024

_TYPE_NAMES = {
    statMod.S_IFDIR: 'dir', statMod.S_IFREG: 'file', statMod.S_IFLNK: 'symlink',
    statMod.S_IFIFO: 'fifo', statMod.S_IFSOCK: 'socket',
    statMod.S_IFCHR: 'chardev', statMod.S_IFBLK: 'blockdev',
}


def sftpFor(hostNameOrId):
    hostEntry = _resolveHost(str(hostNameOrId or ''))
    if hostEntry is None:
        return None, None, '主机不存在'
    try:
        return hostEntry, _openSftp(hostEntry['id']), ''
    except Exception as e:
        return None, None, str(e)


def isLocal(hostNameOrId):
    from modules.agent_local import isLocalHost
    return isLocalHost(hostNameOrId)


def mapSftpError(e):
    """IOError → (status, 说明)。13=权限, 2=不存在, 其余按用法错误。"""
    errno = getattr(e, 'errno', None)
    text = str(e)
    if errno == 13 or 'Permission denied' in text:
        return 403, text
    if errno == 2 or 'No such file' in text:
        return 404, text
    return 400, text


def _sftpParentOf(path):
    """远端路径的父目录（/ 与 \\ 都认）；无分隔符 = 相对当前目录。"""
    norm = str(path or '').replace('\\', '/')
    return norm[:norm.rfind('/')] if '/' in norm else ''


def sftpMakedirs(sftp, parent):
    """逐级创建远端目录（SFTP 无递归 mkdir）；入参就是父目录。
    已存在/无法创建的段跳过，最终缺失由调用方 stat 兜底给出干净错误
    （不暴露临时文件名）。"""
    parent = str(parent or '').replace('\\', '/').rstrip('/')
    if not parent or parent == '/':
        return
    prefix = '/' if parent.startswith('/') else ''
    acc = ''
    for part in [p for p in parent.split('/') if p]:
        acc = (acc + '/' + part) if (acc or prefix) else part
        try:
            sftp.stat(acc)
        except IOError:
            try:
                sftp.mkdir(acc)
            except IOError:
                pass


def _joinPath(base, name):
    if base == '/':
        return '/' + name
    return base.rstrip('/') + '/' + name


def normalize(sftp, path):
    """`~` 与相对路径统一解析为远端绝对路径。"""
    try:
        return sftp.normalize(path)
    except Exception:
        return path


def _entryFor(sftp, base, attr):
    name = attr.filename
    full = _joinPath(base, name)
    ftype = _TYPE_NAMES.get(attr.st_mode & 0o170000, 'other')
    entry = {'name': name, 'path': full, 'size': attr.st_size,
             'mtime': int(attr.st_mtime or 0),
             'dir': ftype == 'dir', 'type': ftype,
             'mode': '%04o' % (attr.st_mode & 0o7777)}
    if ftype == 'symlink':
        try:
            entry['target'] = sftp.readlink(full)
        except IOError:
            entry['target'] = ''
        try:
            target = sftp.stat(full)   # 跟随链接；坏链保持链接自身属性
            entry['dir'] = statMod.S_ISDIR(target.st_mode)
            entry['size'] = target.st_size
        except IOError:
            pass
    return entry


@blueprint.route('/api/agent/sftp/list', methods=['POST'])
def agentSftpList():
    agent, err = requireAgentScope()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    if isLocal(data.get('host')):
        from modules.agent_local import localEnabled, sftpListLocal
        if not localEnabled():
            return jsonify({'error': '主机不存在'}), 404
        return sftpListLocal(data.get('path'))
    _host, sftp, err = sftpFor(data.get('host'))
    if err:
        return jsonify({'error': err}), 404
    raw = str(data.get('path') or '.').strip() or '.'
    try:
        path = normalize(sftp, raw)
        st = sftp.stat(path)
        if not statMod.S_ISDIR(st.st_mode):
            return jsonify({'error': '不是目录: %s' % path,
                            'hint': 'list 只能列目录，读文件请用 sftp/get'}), 400
        entries = [_entryFor(sftp, path, attr) for attr in sftp.listdir_attr(path)]
    except IOError as e:
        status, msg = mapSftpError(e)
        auditAgentAction(agent, 'agent.sftp.list', raw, 'fail', msg[:160])
        return jsonify({'error': msg}), status
    auditAgentAction(agent, 'agent.sftp.list', path, 'ok',
                     '%d items' % len(entries))
    return jsonify({'path': path, 'entries': entries})


@blueprint.route('/api/agent/sftp/get', methods=['POST'])
def agentSftpGet():
    agent, err = requireAgentScope()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    if isLocal(data.get('host')):
        from modules.agent_local import localEnabled, sftpGetLocal
        if not localEnabled():
            return jsonify({'error': '主机不存在'}), 404
        try:
            return sftpGetLocal(data.get('path'),
                                int(data.get('offset') or 0),
                                int(data.get('length') or 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'offset/length 必须是整数'}), 400
    _host, sftp, err = sftpFor(data.get('host'))
    if err:
        return jsonify({'error': err}), 404
    raw = str(data.get('path') or '')
    if not raw:
        return jsonify({'error': '缺少 path'}), 400
    offset = 0
    length = 0   # 0 = 整读
    for name, value in (('offset', data.get('offset')), ('length', data.get('length'))):
        if value is None:
            continue
        try:
            num = int(value)
        except (TypeError, ValueError):
            return jsonify({'error': '%s 必须是非负整数' % name}), 400
        if num < 0 or (name == 'length' and num == 0):
            return jsonify({'error': '%s 必须是非负整数' % name}), 400
        if name == 'offset':
            offset = num
        else:
            length = num
    if length > MAX_FILE_BYTES:
        return jsonify({'error': 'length 上限 %d bytes' % MAX_FILE_BYTES}), 400
    try:
        path = normalize(sftp, raw)
        attr = sftp.stat(path)
        if statMod.S_ISDIR(attr.st_mode):
            return jsonify({'error': '是目录: %s' % path,
                            'hint': '读目录内容请用 sftp/list'}), 400
        total = attr.st_size
        if length == 0:
            if offset:
                length = MAX_FILE_BYTES   # 带 offset 未带 length = 从 offset 读到底
            elif total > MAX_FILE_BYTES:
                return jsonify({'error': '文件超过整读上限 (%d bytes)' % MAX_FILE_BYTES,
                                'hint': '用 offset/length 分块读取（本响应含 total）',
                                'total': total, 'chunkLimit': MAX_FILE_BYTES}), 413
        with sftp.open(path, 'rb') as f:
            if offset:
                f.seek(offset)
            chunk = f.read(min(length or MAX_FILE_BYTES, MAX_FILE_BYTES))
    except IOError as e:
        status, msg = mapSftpError(e)
        auditAgentAction(agent, 'agent.sftp.get', raw, 'fail', msg[:160])
        return jsonify({'error': msg}), status
    auditAgentAction(agent, 'agent.sftp.get', path, 'ok',
                     '%d+%d bytes' % (offset, len(chunk)))
    return jsonify({'ok': True, 'path': path, 'size': len(chunk), 'total': total,
                    'offset': offset,
                    'contentB64': base64.b64encode(chunk).decode('ascii')})


@blueprint.route('/api/agent/sftp/put', methods=['POST'])
def agentSftpPut():
    agent, err = requireAgentScope()
    if agent is None:
        return err
    # 解析请求体之前先看体量：超大上传直接拒，不等收完
    if request.content_length and request.content_length > MAX_FILE_BYTES * 2 + 65536:
        return jsonify({'error': '请求体超过上限（文件上限 %d bytes）' % MAX_FILE_BYTES}), 413
    data = request.get_json(silent=True) or {}
    if isLocal(data.get('host')):
        from modules.agent_local import localEnabled, sftpPutLocal
        if not localEnabled():
            return jsonify({'error': '主机不存在'}), 404
        return sftpPutLocal(data.get('path'), data.get('contentB64'),
                            str(data.get('sha256') or ''),
                            mkdirs=bool(data.get('mkdirs')))
    _host, sftp, err = sftpFor(data.get('host'))
    if err:
        return jsonify({'error': err}), 404
    path = str(data.get('path') or '')
    if not path:
        return jsonify({'error': '缺少 path'}), 400
    try:
        raw = decodeBase64(data.get('contentB64'))
    except ValueError:
        return jsonify({'error': 'contentB64 不是合法 base64（已拒绝写入，'
                                 '原文件未被改动）'}), 400
    if len(raw) > MAX_FILE_BYTES:
        return jsonify({'error': '文件超过上限 (%d bytes)' % MAX_FILE_BYTES}), 413
    wantSha = str(data.get('sha256') or '').lower()
    if wantSha and hashlib.sha256(raw).hexdigest() != wantSha:
        return jsonify({'error': 'sha256 校验失败：内容与调用方摘要不一致'}), 400
    parent = _sftpParentOf(path)
    try:
        sftp.stat(parent or '.')
    except IOError:
        # R5-3：父目录缺失 → 干净的 404（不暴露临时文件名），mkdirs 可自动创建
        if not bool(data.get('mkdirs')):
            auditAgentAction(agent, 'agent.sftp.put', path, 'fail',
                             '父目录不存在')
            return jsonify({'error': '父目录不存在：%s' % parent,
                            'hint': '加 mkdirs:true 自动创建'}), 404
        sftpMakedirs(sftp, parent)
        try:
            sftp.stat(parent or '.')
        except IOError:
            return jsonify({'error': '父目录不存在：%s（自动创建失败）' % parent}), 404
    try:
        oldMode = 0
        try:
            oldMode = sftp.stat(path).st_mode & 0o7777
        except IOError:
            pass
        tmp = '%s.deskport-put-%s' % (path, secrets.token_hex(3))
        with sftp.open(tmp, 'wb') as f:
            f.write(raw)
        try:
            sftp.posix_rename(tmp, path)
        except IOError:
            # 老服务器不支持 posix-rename@openssh.com：退化为直写后清理
            with sftp.open(path, 'wb') as f:
                f.write(raw)
            try:
                sftp.remove(tmp)
            except IOError:
                pass
        if oldMode:
            try:
                sftp.chmod(path, oldMode)
            except IOError:
                pass
    except IOError as e:
        status, msg = mapSftpError(e)
        auditAgentAction(agent, 'agent.sftp.put', path, 'fail', msg[:160])
        return jsonify({'error': msg}), status
    digest = hashlib.sha256(raw).hexdigest()
    auditAgentAction(agent, 'agent.sftp.put', path, 'ok',
                     '%d bytes' % len(raw))
    return jsonify({'ok': True, 'path': path, 'size': len(raw), 'sha256': digest})
