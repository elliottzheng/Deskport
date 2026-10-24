# -*- coding: utf-8 -*-
"""会话守护进程 — 本机终端与远程 SSH 各自独立的一个小服务。

    python sessiond.py local   # ConPTY 终端会话 (sessiondUrl)
    python sessiond.py ssh     # paramiko SSH 会话 (sshSessiondUrl)

两者只共享 termcore 的会话注册表代码, 但进程、端口、令牌、日志完全独立:
重启其中一个不会影响另一个。web 只做代理, 浏览器仍然只连 6789。
"""
import hmac
import logging
import os
import sys

from flask import Flask, jsonify, request
from flask_sock import Sock

import config
import modules.internal_api as internal_api
from modules import termcore

KIND = (sys.argv[1] if len(sys.argv) > 1 else 'local').strip().lower()
if KIND not in ('local', 'ssh'):
    print('usage: python sessiond.py local|ssh', flush=True)
    sys.exit(2)

app = Flask(__name__)
sock = Sock(app)


def _setupLogging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    names = ('termcore', 'localterm') if KIND == 'local' else ('termcore', 'ssh')
    for name in names:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


_setupLogging()

if KIND == 'local':
    from modules import terminal as terminalModule
else:
    from modules import ssh as sshModule


@app.before_request
def tokenGuard():
    if request.path == '/health':
        return None
    token = config.getSessiondToken(KIND)
    supplied = (request.headers.get(internal_api.TOKEN_HEADER)
                or request.args.get('token') or '')
    if not token or not hmac.compare_digest(token, supplied):
        return jsonify({'error': 'forbidden'}), 403
    return None


@app.route('/health')
def health():
    return jsonify({'ok': True, 'kind': KIND, 'pid': os.getpid(),
                    'proto': internal_api.PROTO,
                    'sessions': len(termcore.listSessions(kind='local' if KIND == 'local' else 'ssh'))})


if KIND == 'local':
    @sock.route('/ws/terminal')
    def wsTerminal(ws):
        return terminalModule.wsLocalTerminal(ws)

    @app.route('/sessions')
    def sessions():
        return jsonify({'sessions': termcore.listSessions(kind='local')})
else:
    @sock.route('/ws/ssh')
    def wsSsh(ws):
        return sshModule.wsSshTerminal(ws)

    @app.route('/sessions')
    def sessions():
        return jsonify({'sessions': termcore.listSessions(kind='ssh')})

    @app.route('/disconnect', methods=['POST'])
    def disconnectHost():
        """结束该主机的全部 SSH 会话并断开连接 (对应 web 的 /api/ssh/disconnect)。"""
        data = request.get_json(force=True, silent=True) or {}
        hostId = request.args.get('hostId') or data.get('hostId') or ''
        killed = 0
        for item in termcore.listSessions(kind='ssh'):
            if item.get('hostId') == hostId:
                session = termcore.getSession(item['id'])
                if session is not None:
                    termcore.terminateSession(session)
                    killed += 1
        sshModule.dropConnection(hostId)
        return jsonify({'ok': True, 'killed': killed})


@app.route('/sessions/<sessionID>', methods=['DELETE'])
def killSession(sessionID):
    session = termcore.getSession(sessionID)
    if session is None:
        return jsonify({'error': '会话不存在'}), 404
    termcore.terminateSession(session)
    return jsonify({'ok': True})


def _port():
    return config.getSessiondPort(KIND)


if __name__ == '__main__':
    port = _port()
    print('[sessiond:%s] listening on 127.0.0.1:%d, token=***' % (KIND, port), flush=True)
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)