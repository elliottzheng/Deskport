# -*- coding: utf-8 -*-
"""SSH 终端：paramiko shell 后端、交互认证透传、/api/ssh/ws。"""
import json
import socket
import time

import paramiko
from flask import request

import modules.termcore as termcore
from modules.termcore import TermSession, newSessionID
from modules import ssh_hosts as hosts
from modules.ssh_base import AUTH_TIMEOUT, HostKeyChangedError

class SSHBackend:
    """paramiko interactive shell channel 的 TermSession IO 后端。"""

    def __init__(self, channel):
        self.channel = channel
        self.channel.settimeout(0.05)

    def spawn(self):
        pass    # handler 线程已完成连接+开通道 (幂等)

    def read(self):
        try:
            data = self.channel.recv(4096)
        except (socket.timeout, TimeoutError):
            return None
        except (EOFError, OSError) as e:
            raise EOFError(str(e))
        if not data:
            raise EOFError
        return data

    def write(self, data):
        if isinstance(data, str):
            data = data.replace('\r\n', '\r').replace('\n', '\r').encode('utf-8')
        self.channel.send(data)

    def resize(self, rows, cols):
        try:
            self.channel.resize_pty(width=cols, height=rows)
        except paramiko.SSHException:
            pass

    @property
    def exitcode(self):
        return 0

    def close(self):
        try:
            self.channel.close()
        except Exception:
            pass


def _interactiveAuth(ws, transport, username):
    """keyboard-interactive / 2FA 认证: 提示透传到前端, 答案从 WS 读回。"""
    def handler(title, instructions, prompts):
        ws.send(json.dumps({
            'type': 'ssh-auth',
            'title': title or '',
            'instructions': instructions or '',
            'prompts': [{'text': p[0], 'echo': bool(p[1])} for p in prompts],
        }, ensure_ascii=False))
        deadline = time.time() + AUTH_TIMEOUT
        while time.time() < deadline:
            try:
                msg = ws.receive(timeout=5)
            except Exception:
                raise paramiko.SSHException('认证中断')
            if msg is None:
                continue
            payload, isCtrl = termcore._decodeFrame(msg)
            if isCtrl and payload.get('type') == 'ssh-auth-answer':
                return [str(a) for a in payload.get('answers', [])]
        raise paramiko.SSHException('认证超时')
    transport.auth_interactive(username, handler)


def wsSshTerminal(ws):
    hostId = request.args.get('hostId') or ''
    sessionID = request.args.get('session') or None
    wantReplay = request.args.get('replay') == '1'

    # 重连: 命中存活 SSH 会话直接附着 (channel 仍在)
    if sessionID:
        existing = termcore.getSession(sessionID)
        if existing is not None and existing.alive and existing.kind == 'ssh':
            termcore.attachAndPump(ws, existing, resumed=True, wantReplay=wantReplay)
            return

    hostEntry = hosts.findHost(hostId)
    if hostEntry is None:
        try:
            ws.send(json.dumps({'type': 'error', 'msg': '主机不存在'},
                               ensure_ascii=False))
            ws.close()
        except Exception:
            pass
        return

    try:
        client = hosts.getConnection(hostEntry)
        channel = client.invoke_shell(term='xterm-256color', width=100, height=30)
    except HostKeyChangedError as e:
        # 主机密钥变更: 强告警帧 (前端阻断并要求显式重新信任)
        try:
            ws.send(json.dumps({'type': 'ssh-hostkey-changed', 'msg': str(e)},
                               ensure_ascii=False))
            ws.close()
        except Exception:
            pass
        return
    except paramiko.AuthenticationException:
        # 密码认证被拒 → 尝试 keyboard-interactive 透传 (2FA/MFA)
        try:
            transport = paramiko.Transport((hostEntry['host'],
                                             int(hostEntry.get('port') or 22)))
            transport.connect()
            _interactiveAuth(ws, transport, hostEntry['user'])
            # 认证成功: 包装成 client 形态并放入连接注册表 (SFTP 复用同一 transport)
            client = paramiko.SSHClient()
            client._transport = transport
            hosts.cacheConnection(hostEntry['id'], client)
            channel = client.invoke_shell(term='xterm-256color', width=100, height=30)
        except Exception as e:
            try:
                ws.send(json.dumps({'type': 'error', 'msg': 'SSH 认证失败: ' + str(e)},
                                   ensure_ascii=False))
                ws.close()
            except Exception:
                pass
            return
    except Exception as e:
        try:
            ws.send(json.dumps({'type': 'error', 'msg': '连接失败: ' + str(e)},
                               ensure_ascii=False))
            ws.close()
        except Exception:
            pass
        return

    label = hostEntry.get('name') or (hostEntry['user'] + '@' + hostEntry['host'])
    backend = SSHBackend(channel)
    session = TermSession(newSessionID(), 'ssh', label, backend, hostId=hostId)
    termcore.newSession(session)
    termcore.attachAndPump(ws, session, resumed=False, wantReplay=False)
