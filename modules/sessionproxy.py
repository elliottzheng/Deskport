# -*- coding: utf-8 -*-
"""web → 会话守护进程代理（本机终端 / 远程 SSH 两个独立服务）。

浏览器仍然只连 web 的 6789: 认证、路由不变; WS 与会话列表在这里双向转发。
这样 web 重启不会杀掉 pty/SSH channel, 客户端重连后按 session id 重新附着。

联邦: 配置 remotes 后, 其他 Deskport 的终端会话会合并进本机列表
（条目带 machine 标签）, WS/删除按 session id 路由到归属机器
（带联邦 token）。会话本体仍活在各机器自己的进程里, hub 重启不影响。
"""
import json
import logging
import threading

import requests
from flask import jsonify, request
from simple_websocket import Client, ConnectionClosed

import config
import modules.federation as federation
import modules.internal_api as internal_api
from modules.termcore import sealSocketAfterClose

log = logging.getLogger('sessionproxy')


def _fail(ws, message):
    try:
        ws.send(json.dumps({'type': 'error', 'msg': message}, ensure_ascii=False))
    except Exception:
        pass
    sealSocketAfterClose(ws)


def _proxyWs(ws, url, headers, mid=None):
    """双向泵: 浏览器 WS ⇄ 上游 WS。mid 用于联邦会话归属登记。"""
    try:
        upstream = Client(url, headers=headers)
    except Exception as e:
        log.warning('sessiond connect failed: %s', e)
        _fail(ws, '会话服务不可用: %s' % e)
        return

    stop = threading.Event()
    registered = False

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
            if mid and not registered:
                try:
                    data = json.loads(msg)
                    if isinstance(data, dict) and data.get('type') == 'hello':
                        federation.remember(mid, data.get('session'))
                        registered = True
                except ValueError:
                    pass
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


def _proxyJsonData(url, headers, method='GET'):
    try:
        resp = requests.request(method, url, headers=headers, timeout=5)
    except requests.RequestException as e:
        return {'error': '会话服务不可用: %s' % e}, 503
    try:
        return resp.json(), resp.status_code
    except ValueError:
        return {'error': resp.text[:200]}, resp.status_code


def _proxyJson(kind, path, method='GET'):
    url = internal_api.base(kind) + path
    data, status = _proxyJsonData(url, internal_api.headers(kind), method)
    return jsonify(data), status


def _localWsUrl():
    return internal_api.wsUrl('local', request.query_string.decode('utf-8', 'replace'))


def _localHeaders():
    return internal_api.headers('local')


def _resolveTerminalWs():
    """联邦路由: ?machine= 指定远端(新会话), 或 ?session= 归属远端(附着)。

    返回 (url, headers, mid) 或 None (走本机上游)。
    """
    mid = request.args.get('machine')
    if mid:
        remote = federation.findRemote(mid)
        if remote:
            scheme = 'wss://' if remote['base'].startswith('https://') else 'ws://'
            url = scheme + remote['base'][len('http://'):].rstrip('/') + '/api/terminal/ws'
            headers = {'X-Deskport-Token': remote['token']}
            if request.query_string:
                url += '?' + request.query_string.decode('utf-8', 'replace')
            return url, headers, mid
        log.warning('联邦机器不存在: %s', mid)
    session = request.args.get('session')
    if session:
        found = federation.wsUpstreamFor(session, request.query_string.decode('utf-8', 'replace'))
        if found:
            return found[0], found[1], None
    return None


def _registerTerminal(app, sock):
    def wsTerminal(ws):
        upstream = _resolveTerminalWs()
        if upstream:
            url, headers, mid = upstream
            return _proxyWs(ws, url, headers, mid=mid)
        return _proxyWs(ws, _localWsUrl(), _localHeaders())

    def terminalSessions():
        data, status = _proxyJsonData(internal_api.base('local') + '/sessions',
                                      internal_api.headers('local'))
        if status == 200 and isinstance(data.get('sessions'), list):
            data['sessions'] = federation.mergeTerminalSessions(data['sessions'])
        return jsonify(data), status

    def terminalKill(sessionID):
        found = federation.killUpstreamFor(sessionID)
        if found:
            url, headers = found
            data, status = _proxyJsonData(url, headers, 'DELETE')
            return jsonify(data), status
        data, status = _proxyJson('local', '/sessions/' + sessionID, 'DELETE')
        return jsonify(data), status

    sock.route('/api/terminal/ws')(wsTerminal)
    # legacy 兼容：RemoteConnecter 插件（RemoteVSCode 等）写死 /terminal/api/ws
    def wsTerminalLegacy(ws):
        return wsTerminal(ws)
    sock.route('/terminal/api/ws')(wsTerminalLegacy)
    app.add_url_rule('/api/terminal/sessions', 'proxyTerminalSessions', terminalSessions)
    app.add_url_rule('/api/terminal/sessions/<sessionId>', 'proxyTerminalKill',
                     terminalKill, methods=['DELETE'])


def _registerSsh(app, sock):
    def wsSsh(ws):
        return _proxyWs(ws, internal_api.wsUrl('ssh', request.query_string.decode('utf-8', 'replace')),
                        internal_api.headers('ssh'))

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
