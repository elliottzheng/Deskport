# -*- coding: utf-8 -*-
"""智能体持久会话: bash -s 常驻通道, 多次 exec 共享 cwd 与环境。

端点 (路由挂在 agent_api 的 blueprint 上, 前缀 /api/agent/ssh/sessions):
    POST   /api/agent/ssh/sessions          开会话 {host, cwd}
    GET    /api/agent/ssh/sessions          会话列表
    POST   /api/agent/ssh/sessions/<sid>    执行命令 {command, timeoutSec, tailBytes, combined, full}
    DELETE /api/agent/ssh/sessions/<sid>    关闭会话

完成检测: 每条命令后追加 `printf "__DONE_<nonce>=<exit>\\n" "$?"`, 收到完整
标记行即取走本轮输出并推进游标 (标记行与游标一并丢弃, 下条命令从干净的
游标开始)。nonce 以字母结尾, 与 exit 码数字无歧义。
"""
import re
import secrets
import shlex
import threading
import time

import paramiko
from flask import jsonify, request

from modules import events
from modules.agent_api import (DEFAULT_TIMEOUT, MAX_TIMEOUT, blueprint,
                               _agentRequired, _resolveHost, _safeHosts)
from modules.ssh_hosts import getConnection

_sessions = {}
_sessionsLock = threading.Lock()
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


def _findMarker(buf, start, nonce):
    """在 buf[start:] 找 `__DONE_<nonce>=<exit码>` 完整行; 返回 (起点, 行尾后, exit码)。"""
    needle = ('__DONE_' + nonce).encode('utf-8')
    searchFrom = start
    while True:
        idx = buf.find(needle, searchFrom)
        if idx < 0:
            return None, None, None
        lineEnd = buf.find(b'\n', idx)
        if lineEnd < 0:
            return None, None, None   # 行还没收全, 继续等
        m = re.search(rb'=(-?\d+)\s*$', bytes(buf[idx:lineEnd]))
        if m:
            return idx, lineEnd + 1, int(m.group(1))
        searchFrom = lineEnd + 1


def _openChannel(hostEntry, cwd):
    client = getConnection(hostEntry)
    transport = client.get_transport()
    channel = transport.open_session(timeout=10)
    channel.settimeout(1.0)
    nonce = secrets.token_hex(6) + 'x'   # 以字母结尾, 避免与 exit 码数字混淆
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
        channel, nonce = _openChannel(hostEntry, cwd)
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
        markerStart = None
        markerEnd = None
        exitCode = None
        timedOut = False
        chan.sendLine(command)
        chan.sendLine('printf "__DONE_' + sess['nonce'] + '=%s\\n" "$?"')
        deadline = time.monotonic() + timeoutSec
        while time.monotonic() < deadline:
            got = chan.pump()
            idx, lineEnd, code = _findMarker(chan.out, outStart, sess['nonce'])
            if idx is not None:
                markerStart = idx
                markerEnd = lineEnd + 1
                exitCode = code
                break
            if chan.dead():
                timedOut = True
                sess['alive'] = False
                break
            if not got:
                time.sleep(0.05)
        outBytes = bytes(chan.out[outStart:markerStart]) if markerStart is not None \
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
