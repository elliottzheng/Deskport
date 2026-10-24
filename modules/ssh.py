# -*- coding: utf-8 -*-
"""SSH 模块入口：装配 hosts/terminal/sftp，并保留 sessiond 需要的两个入口。"""
import modules.termcore as termcore
from flask import jsonify

from modules import ssh_hosts, ssh_sftp, ssh_terminal  # noqa: F401  注册路由
from modules.ssh_base import blueprint, SftpAuthError
from modules.ssh_hosts import dropConnection  # noqa: F401  供 sessiond 调用
from modules.ssh_terminal import wsSshTerminal


def register(app, sock, sessionRoutes=True):
    def sshSessionsEndpoint():
        return jsonify({'sessions': termcore.listSessions(kind='ssh')})

    @app.errorhandler(SftpAuthError)
    def sftpAuthErrorHandler(e):
        return jsonify({'error': str(e)}), 401

    app.register_blueprint(blueprint)
    if sessionRoutes:
        app.add_url_rule('/api/ssh/sessions', 'sshSessions', sshSessionsEndpoint)
        sock.route('/api/ssh/ws')(wsSshTerminal)
