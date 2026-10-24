# -*- coding: utf-8 -*-
"""web → 会话守护进程代理（本机终端 / 远程 SSH 两个独立服务）。

浏览器仍然只连 web 的 6789: 认证、路由不变; WS 与会话列表在这里双向转发。
这样 web 重启不会杀掉 pty/SSH channel, 客户端重连后按 session id 重新附着。
"""
import json
import logging
import threading

import requests
from flask import jsonify, request
from simple_websocket import Client, ConnectionClosed

import modules.internal_api as internal_api
from modules.termcore import sealSocketAfterClose

log = logging.getLogger('sessionproxy')


def _fail(ws, message):
    try:
        ws.send(json.dumps({'type': 'error', 'msg': message}, ensure_ascii=False))
    except Exception:
        pass
    sealSocketAfterClose(ws)


def _proxyWs(ws, kind):
    query = request.query_string.decode('utf-8', 'replace')
    url = internal_api.wsUrl(kind, query)
    try:
        upstream = Client(url, headers=internal_api.headers(kind))
    except Exception as e:
        log.warning('sessiond(%s) connect failed: %s', kind, e)
        _fail(ws, '会话服务不可用: %s' % e)
        return

    stop = threading.Event()

    def pumpBrowserToDaemon():
        while not stop.is_set():
            try:
                msg = ws.receive(timeout=0.05)
            except ConnectionClosed:
                break
            except Exception:
                break
            if msg is None:
                continue
            try:
                upstream.send(msg)
            except Exception:
                break
        stop.set()
        try:
            upstream.close()
        except Exception:
            pass

    threading.Thread(target=pumpBrowserToDaemon, daemon=True).start()
    try:
        while not stop.is_set():
            try:
                msg = upstream.receive(timeout=0.05)
            except ConnectionClosed:
                break
            except Exception:
                break
            if msg is None:
                continue
            try:
                ws.send(msg)
            except Exception:
                break
    finally:
        stop.set()
        try:
            upstream.close()
        except Exception:
            pass
        sealSocketAfterClose(ws)


def _proxyJson(kind, path, method='GET'):
    try:
        resp = requests.request(method, internal_api.base(kind) + path,
                                headers=internal_api.headers(kind), timeout=5)
    except requests.RequestException as e:
        return jsonify({'error': '会话服务不可用: %s' % e}), 503
    try:
        data = resp.json()
    except ValueError:
        data = {'error': resp.text[:200]}
    return jsonify(data), resp.status_code


def _registerTerminal(app, sock):
    def wsTerminal(ws):
        return _proxyWs(ws, 'local')

    def terminalSessions():
        return _proxyJson('local', '/sessions')

    def terminalKill(sessionId):
        return _proxyJson('local', '/sessions/' + sessionId, 'DELETE')

    sock.route('/api/terminal/ws')(wsTerminal)
    app.add_url_rule('/api/terminal/sessions', 'proxyTerminalSessions', terminalSessions)
    app.add_url_rule('/api/terminal/sessions/<sessionId>', 'proxyTerminalKill',
                     terminalKill, methods=['DELETE'])


def _registerSsh(app, sock):
    def wsSsh(ws):
        return _proxyWs(ws, 'ssh')

    def sshSessions():
        return _proxyJson('ssh', '/sessions')

    def sshKill(sessionId):
        return _proxyJson('ssh', '/sessions/' + sessionId, 'DELETE')

    sock.route('/api/ssh/ws')(wsSsh)
    app.add_url_rule('/api/ssh/sessions', 'proxySshSessions', sshSessions)
    app.add_url_rule('/api/ssh/sessions/<sessionId>', 'proxySshKill',
                     sshKill, methods=['DELETE'])


def register(app, sock):
    """按配置分别代理: 本机终端与远程 SSH 互不依赖。"""
    if internal_api.base('local'):
        _registerTerminal(app, sock)
    if internal_api.base('ssh'):
        _registerSsh(app, sock)