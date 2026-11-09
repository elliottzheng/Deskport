# -*- coding: utf-8 -*-
"""智能体 API — 给外部 AI 程序的机器接口（令牌认证，非交互式执行）。

认证:
    请求头 X-Agent-Token 或 Authorization: Bearer <token>，
    与 config.json agentTokens[].tokenHash（SHA-256）比对；未命中一律 401。
    浏览器 Cookie 登录态同样放行（供面板 UI 未来复用）。

端点:
    GET  /api/agent/hosts          主机列表（只含 id/name/host/port，无凭据）
    POST /api/agent/ssh/exec       非交互执行命令（paramiko exec_command）
    POST /api/agent/sftp/list      远程目录列表
    GET  /api/agent/sftp/get       读取远程文件（base64，有上限）
    POST /api/agent/sftp/put       写远程文件（base64）

审计: 每次调用写 events（agent.exec / agent.sftp.*）。
安全: 插件安全模型同档——外部智能体 = 可信代码；危险命令审批是后续增强项。
"""
import hashlib
import hmac
import os
import posixpath
import re
import secrets
import threading
import shlex
import time

import paramiko
from flask import Blueprint, jsonify, request

import config
from modules import events, textio
from modules import ssh_hosts as hosts
from modules.ssh_sftp import _openSftp

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 600
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024

blueprint = Blueprint('agent', __name__)


# ---------------------------------------------------------------- 令牌

def tokenHash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _tokenFromRequest():
    token = (request.headers.get('X-Agent-Token') or '').strip()
    if not token:
        auth = request.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            token = auth[7:].strip()
    return token


def agentFromRequest():
    """返回命中的令牌条目；未命中返回 None。"""
    token = _tokenFromRequest()
    if not token:
        return None
    digest = tokenHash(token)
    for item in config.getAgentTokens():
        stored = str(item.get('tokenHash') or '')
        if stored and hmac.compare_digest(stored, digest):
            return dict(item)
    return None


def _agentRequired():
    """守卫: 无有效令牌返回 401；有则返回令牌条目。"""
    agent = agentFromRequest()
    if agent is None:
        return None, (jsonify({'error': 'unauthorized (需要 X-Agent-Token)'}), 401)
    return agent, None


# ---------------------------------------------------------------- 主机解析

def _resolveHost(nameOrId):
    entry = hosts.findHost(nameOrId)
    if entry is not None:
        return entry
    wanted = str(nameOrId or '').strip().lower()
    for item in hosts.repo.all():
        if str(item.get('name') or '').strip().lower() == wanted:
            return item
    return None


def _safeHosts():
    out = []
    for item in hosts.repo.all():
        out.append({'id': item.get('id'), 'name': item.get('name'),
                    'host': item.get('host'), 'port': item.get('port')})
    return out


# ---------------------------------------------------------------- 执行

def _execOnHost(hostEntry, command, timeoutSec):
    """paramiko exec_command: 非 pty 通道, 轮询读双流, 到总时限掐断。"""
    client = hosts.getConnection(hostEntry)
    transport = client.get_transport()
    channel = transport.open_session(timeout=10)
    channel.settimeout(1.0)
    channel.exec_command(command)
    stdoutBuf = bytearray()
    stderrBuf = bytearray()
    deadline = time.monotonic() + timeoutSec
    timedOut = False
    while True:
        got = False
        if channel.recv_ready():
            stdoutBuf += channel.recv(65536)
            got = True
        if channel.recv_stderr_ready():
            stderrBuf += channel.recv_stderr(65536)
            got = True
        if channel.exit_status_ready() and not channel.recv_ready() \
                and not channel.recv_stderr_ready():
            break
        if time.monotonic() >= deadline:
            timedOut = True
            channel.close()
            break
        if not got:
            time.sleep(0.05)
    while channel.recv_ready():
        stdoutBuf += channel.recv(65536)
    while channel.recv_stderr_ready():
        stderrBuf += channel.recv_stderr(65536)
    exitCode = channel.recv_exit_status() if channel.exit_status_ready() else None
    return {
        'exitCode': exitCode,
        'stdout': stdoutBuf[:MAX_OUTPUT_BYTES].decode('utf-8', 'replace'),
        'stderr': stderrBuf[:MAX_OUTPUT_BYTES].decode('utf-8', 'replace'),
        'timedOut': timedOut,
        'truncated': max(len(stdoutBuf), len(stderrBuf)) > MAX_OUTPUT_BYTES,
    }


@blueprint.route('/api/agent/hosts')
def agentHosts():
    agent, err = _agentRequired()
    if agent is None:
        return err
    return jsonify({'hosts': _safeHosts(), 'tokenName': agent.get('name') or ''})


@blueprint.route('/api/agent/ssh/exec', methods=['POST'])
def agentExec():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    command = str(data.get('command') or '').strip()
    cwd = str(data.get('cwd') or '').strip()
    if not command:
        return jsonify({'error': '缺少 command'}), 400
    if cwd:
        command = 'cd ' + shlex.quote(cwd) + ' && (' + command + ')'
    try:
        timeoutSec = int(data.get('timeoutSec') or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeoutSec = DEFAULT_TIMEOUT
    timeoutSec = max(1, min(timeoutSec, MAX_TIMEOUT))

    hostEntry = _resolveHost(str(data.get('host') or ''))
    if hostEntry is None:
        names = [h.get('name') or h.get('id') for h in _safeHosts()][:8]
        return jsonify({'error': '主机不存在', 'availableHosts': names}), 404
    hostName = hostEntry.get('name') or hostEntry.get('host') or ''

    started = time.monotonic()
    try:
        result = _execOnHost(hostEntry, command, timeoutSec)
    except paramiko.SSHException as e:
        events.log('agent.exec', hostName, 'fail', command[:160], request.remote_addr)
        return jsonify({'error': 'SSH 执行失败: %s' % e}), 502
    durationMs = int((time.monotonic() - started) * 1000)

    events.log('agent.exec', hostName,
               'timeout' if result['timedOut'] else 'ok',
               command[:160], request.remote_addr)
    return jsonify({'exit': result['exitCode'],
                    'out': result['stdout'], 'err': result['stderr'],
                    'timedOut': result['timedOut'],
                    'outTruncated': result['truncated'],
                    'durationMs': durationMs})


# ---------------------------------------------------------------- 会话 (持久 shell)

_sessions = {}                # sid -> 记录
_sessionsLock = threading.Lock()
SESSION_MARKER_PREFIX = '__DONE_'
SESSION_MAX_OPEN = 16


class _SessionChannel:
    """bash -s 常驻通道: stdin 喂命令, 双流按游标累积。"""

    def __init__(self, channel):
        self.ch = channel
        self.out = bytearray()
        self.err = bytearray()

    def sendLine(self, line):
        self.ch.sendall(line.encode('utf-8') + b'\n')

    def pump(self):
        got = False
        while self.ch.recv_ready():
            self.out += self.ch.recv(65536)
            got = True
        while self.ch.recv_stderr_ready():
            self.err += self.ch.recv_stderr(65536)
            got = True
        return got

    def dead(self):
        if self.ch.closed:
            return True
        transport = self.ch.get_transport()
        return transport is None or not transport.is_active()

    def drain(self):
        while self.ch.recv_ready():
            self.out += self.ch.recv(65536)
        while self.ch.recv_stderr_ready():
            self.err += self.ch.recv_stderr(65536)


def _openAgentSession(hostEntry, cwd):
    client = hosts.getConnection(hostEntry)
    transport = client.get_transport()
    channel = transport.open_session(timeout=10)
    channel.settimeout(1.0)
    nonce = secrets.token_hex(6)
    boot = ''
    if cwd:
        boot = 'cd ' + shlex.quote(cwd) + ' && '
    channel.exec_command(boot + 'exec bash -s')
    return channel, nonce


@blueprint.route('/api/agent/ssh/sessions', methods=['POST'])
def agentSessionOpen():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    hostEntry = _resolveHost(str(data.get('host') or ''))
    if hostEntry is None:
        names = [h.get('name') or h.get('id') for h in _safeHosts()][:8]
        return jsonify({'error': '主机不存在', 'availableHosts': names}), 404
    cwd = str(data.get('cwd') or '').strip()

    with _sessionsLock:
        openCount = sum(1 for s in _sessions.values() if s['alive'])
    if openCount >= SESSION_MAX_OPEN:
        return jsonify({'error': '存活会话已达上限 (%d)，请先关闭部分会话'
                        % SESSION_MAX_OPEN}), 429

    try:
        channel, nonce = _openAgentSession(hostEntry, cwd)
    except paramiko.SSHException as e:
        return jsonify({'error': 'SSH 会话建立失败: %s' % e}), 502
    sid = secrets.token_hex(8)
    with _sessionsLock:
        _sessions[sid] = {'id': sid, 'chan': _SessionChannel(channel),
                          'nonce': nonce, 'lock': threading.Lock(),
                          'host': hostEntry.get('name') or hostEntry.get('host'),
                          'cwd': cwd or '~', 'alive': True,
                          'createdAt': time.time()}
    events.log('agent.session.open', hostEntry.get('name'), 'ok',
               cwd or '~', request.remote_addr)
    return jsonify({'sessionId': sid, 'host': hostEntry.get('name') or '',
                    'cwd': cwd or '~'})


@blueprint.route('/api/agent/ssh/sessions')
def agentSessionList():
    agent, err = _agentRequired()
    if agent is None:
        return err
    with _sessionsLock:
        items = [{'id': s['id'], 'host': s['host'], 'cwd': s['cwd'],
                  'alive': s['alive']} for s in _sessions.values()]
    return jsonify({'sessions': items})


@blueprint.route('/api/agent/ssh/sessions/<sid>', methods=['DELETE'])
def agentSessionClose(sid):
    agent, err = _agentRequired()
    if agent is None:
        return err
    with _sessionsLock:
        sess = _sessions.pop(sid, None)
    if sess is None:
        return jsonify({'error': '会话不存在'}), 404
    try:
        sess['chan'].ch.close()
    except Exception:
        pass
    sess['alive'] = False
    return jsonify({'ok': True})


def _findMarker(buf, start, nonce):
    """在 buf[start:] 找 `nonce<exit码>` 完整行; 返回 (marker起点, 行尾后, exit码)。"""
    needle = ('__DONE_' + nonce).encode('utf-8')
    searchFrom = start
    while True:
        idx = buf.find(needle, searchFrom)
        if idx < 0:
            return None, None, None
        lineEnd = buf.find(b'\n', idx)
        if lineEnd < 0:
            return None, None, None   # 行还没收全, 继续等
        m = re.search(rb'(-?\d+)\s*$', bytes(buf[idx:lineEnd]))
        if m:
            return idx, lineEnd + 1, int(m.group(1))
        searchFrom = lineEnd + 1


@blueprint.route('/api/agent/ssh/sessions/<sid>', methods=['POST'])
def agentSessionExec(sid):
    agent, err = _agentRequired()
    if agent is None:
        return err
    with _sessionsLock:
        sess = _sessions.get(sid)
    if sess is None:
        return jsonify({'error': '会话不存在'}), 404
    data = request.get_json(silent=True) or {}
    command = str(data.get('command') or '').strip()
    if not command:
        return jsonify({'error': '缺少 command'}), 400
    try:
        timeoutSec = int(data.get('timeoutSec') or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeoutSec = DEFAULT_TIMEOUT
    timeoutSec = max(1, min(timeoutSec, MAX_TIMEOUT))
    tailBytes = max(256, min(int(data.get('tailBytes') or 8192), 65536))
    combined = bool(data.get('combined'))
    fullOut = bool(data.get('full'))

    chan = sess['chan']
    with sess['lock']:
        outStart = len(chan.out)
        errStart = len(chan.err)
        markerEnd = None
        exitCode = None
        timedOut = False
        chan.sendLine(command)
        chan.sendLine(('printf "__DONE_' + sess['nonce'] + '%s\\n" "$?"'))
        deadline = time.monotonic() + timeoutSec
        markerEnd = None
        exitCode = None
        while time.monotonic() < deadline:
            got = chan.pump()
            idx, lineEnd, code = _findMarker(chan.out, outStart, sess['nonce'])
            if idx is not None:
                markerEnd = lineEnd
                exitCode = code
                break
            if chan.dead():
                timedOut = True
                sess['alive'] = False
                break
            if not got:
                time.sleep(0.05)
        outBytes = bytes(chan.out[outStart:markerEnd]) if markerEnd is not None \
            else bytes(chan.out[outStart:])
        errBytes = bytes(chan.err[errStart:])
        if markerEnd is not None:
            del chan.out[:markerEnd]
            del chan.err[:errStart]

    outText = outBytes.decode('utf-8', 'replace')
    errText = errBytes.decode('utf-8', 'replace')
    truncated = False
    if not fullOut and len(outText) > tailBytes:
        outText = outText[-tailBytes:]
        truncated = True
    if combined:
        outText = (outText.rstrip('\n') + '\n' + errText).strip('\n')
        errText = ''
    events.log('agent.exec.session', sess['host'], 'ok', command[:160],
               request.remote_addr)
    return jsonify({'exit': exitCode, 'out': outText,
                    'err': '' if combined else errText,
                    'timedOut': timedOut, 'outTruncated': truncated,
                    'cwd': sess['cwd'], 'alive': sess['alive']})


# ---------------------------------------------------------------- SFTP

def _sftpFor(hostNameOrId):
    hostEntry = _resolveHost(str(hostNameOrId or ''))
    if hostEntry is None:
        return None, None, '主机不存在'
    from modules.ssh_sftp import _openSftp as sftpOpen
    try:
        return hostEntry, sftpOpen(hostEntry['id']), ''
    except Exception as e:
        return None, None, str(e)


@blueprint.route('/api/agent/sftp/list', methods=['POST'])
def agentSftpList():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    hostEntry, sftp, err = _sftpFor(data.get('host'))
    if err:
        return jsonify({'error': err}), 404
    path = (data.get('path') or '.').strip() or '.'
    try:
        entries = []
        baseDir = path if path.endswith('/') else path + '/'
        for attr in sftp.listdir_attr(path):
            full = posixpath.normpath(baseDir + attr.filename) if path != '/' \
                else '/' + attr.filename
            entries.append({'name': attr.filename, 'path': full,
                            'size': attr.st_size, 'mtime': int(attr.st_mtime),
                            'dir': (attr.st_mode & 0o170000) == 0o040000})
        events.log('agent.sftp.list', path, 'ok', '', request.remote_addr)
        return jsonify({'path': path, 'entries': entries})
    except IOError as e:
        events.log('agent.sftp.list', path, 'fail', str(e), request.remote_addr)
        return jsonify({'error': str(e)}), 404


@blueprint.route('/api/agent/sftp/get', methods=['POST'])
def agentSftpGet():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    hostEntry, sftp, err = _sftpFor(data.get('host'))
    if err:
        return jsonify({'error': err}), 404
    path = str(data.get('path') or '')
    if not path:
        return jsonify({'error': '缺少 path'}), 400
    try:
        attr = sftp.stat(path)
        if attr.st_size > MAX_FILE_BYTES:
            return jsonify({'error': '文件超过上限 (%d bytes)，请用 SFTP 面板下载'
                            % MAX_FILE_BYTES}), 413
        with sftp.open(path, 'rb') as f:
            raw = f.read(attr.st_size)
    except IOError as e:
        return jsonify({'error': str(e)}), 404
    events.log('agent.sftp.get', path, 'ok', '%d bytes' % attr.st_size,
               request.remote_addr)
    import base64
    return jsonify({'ok': True, 'path': path, 'size': attr.st_size,
                    'contentB64': base64.b64encode(raw).decode('ascii')})


@blueprint.route('/api/agent/sftp/put', methods=['POST'])
def agentSftpPut():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    hostEntry, sftp, err = _sftpFor(data.get('host'))
    if err:
        return jsonify({'error': err}), 404
    path = str(data.get('path') or '')
    contentB64 = str(data.get('contentB64') or '')
    if not path or not contentB64:
        return jsonify({'error': '缺少 path/contentB64'}), 400
    import base64
    try:
        raw = base64.b64decode(contentB64)
    except Exception:
        return jsonify({'error': 'contentB64 不是合法 base64'}), 400
    if len(raw) > MAX_FILE_BYTES:
        return jsonify({'error': '文件超过上限 (%d bytes)' % MAX_FILE_BYTES}), 413
    try:
        with sftp.open(path, 'wb') as f:
            f.write(raw)
    except IOError as e:
        return jsonify({'error': str(e)}), 400
    events.log('agent.sftp.put', path, 'ok', '%d bytes' % len(raw),
               request.remote_addr)
    return jsonify({'ok': True, 'path': path, 'size': len(raw)})
