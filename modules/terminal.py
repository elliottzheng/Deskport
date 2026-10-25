# -*- coding: utf-8 -*-
"""本地终端模块 — ConPTY (端口内默认终端)

会话/协议核心在 modules/termcore.py; 本文件只做本地 ConPTY 的装配。
"""
import logging
import os
import shutil

from flask import jsonify, request

from modules import termcore
from modules.termcore import ConPTYBackend

# 默认 pwsh 7 (用户指定); 没装则回落 Windows PowerShell 5.1。
# pwsh 7.6.6 在 ConPTY 下遇到退化尺寸(如 1x1)会卡死, 由 ConPTYBackend.resize 的下限兜住。
DEFAULT_TERMINAL_CMD = 'pwsh.exe' if shutil.which('pwsh.exe') else 'powershell.exe'
log = logging.getLogger('localterm')


def register(app, sock, sessionRoutes=True):
    def localSessionsEndpoint():
        return jsonify({'sessions': termcore.listSessions(kind='local')})

    def killSessionEndpoint(sessionID):
        session = termcore.getSession(sessionID)
        if session is None:
            return jsonify({'error': '会话不存在或已退出'}), 404
        termcore.terminateSession(session)
        return jsonify({'ok': True})

    if sessionRoutes:
        app.add_url_rule('/api/terminal/sessions', 'terminalSessions', localSessionsEndpoint)
        app.add_url_rule('/api/terminal/sessions/<sessionID>', 'terminalKill',
                         killSessionEndpoint, methods=['DELETE'])
        sock.route('/api/terminal/ws')(wsLocalTerminal)
        # legacy 兼容：RemoteConnecter 插件（RemoteVSCode 等）写死 /terminal/api/ws
        sock.route('/terminal/api/ws')(wsLocalTerminalLegacy)


def wsLocalTerminalLegacy(ws):
    """legacy 插件入口：路径不同，行为与 /api/terminal/ws 一致。"""
    return wsLocalTerminal(ws)


def wsLocalTerminal(ws):
    cmd = request.args.get('cmd') or DEFAULT_TERMINAL_CMD
    sessionID = request.args.get('session') or None
    wantReplay = request.args.get('replay') == '1'
    # 新会话的工作目录 (文件页"在终端中打开"/收件箱案例目录); 只接受真实存在的目录
    cwd = request.args.get('cwd') or None
    if cwd and not os.path.isdir(cwd):
        cwd = None

    resumed = False
    session = None
    if sessionID:
        existing = termcore.getSession(sessionID)
        if existing is not None and existing.alive and existing.kind == 'local':
            session = existing
            resumed = True
    if session is None:
        session = termcore.TermSession(
            termcore.newSessionID(), 'local', cmd, ConPTYBackend(cmd, cwd),
            cwd=cwd)
        termcore.newSession(session)

    termcore.attachAndPump(ws, session, resumed=resumed, wantReplay=wantReplay)
