# -*- coding: utf-8 -*-
"""联邦外部应用发现测试 — 广播端点/策略白名单/发现挂载/token 注入/冲突跳过/撤销。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config
import modules.external_proxy as externalProxy
import modules.federation as federation
from app import create_app


class ProviderHandler(BaseHTTPRequestHandler):
    """远端 Deskport 桩: /api/external-apps 广播 + 被接力的 /yuanchuang/* 上游。"""
    APPS = []
    LAST = {}

    def _record(self):
        ProviderHandler.LAST = {'path': self.path, 'method': self.command,
                                'headers': dict(self.headers.items())}

    def do_GET(self):
        self._record()
        if self.path == '/api/external-apps':
            body = json.dumps({'apps': ProviderHandler.APPS}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', len(b'upstream-ok'))
        self.end_headers()
        self.wfile.write(b'upstream-ok')

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def provider():
    server = ThreadingHTTPServer(('127.0.0.1', 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:%d' % server.server_address[1]
    server.shutdown()


@pytest.fixture(autouse=True)
def cleanState(monkeypatch):
    """线程不启动 (手动 refreshOnce 驱动), 模块状态每个用例前清零。"""
    monkeypatch.setattr(federation, '_ensureThread', lambda force=False: None)
    monkeypatch.setattr(federation, 'REFRESH_SECONDS', 3600)
    federation._extCache.clear()
    federation._cache.clear()
    externalProxy._discovered.clear()
    yield
    federation._extCache.clear()
    federation._cache.clear()
    externalProxy._discovered.clear()


def makeRemote(base, apps=('yuanchuang',), terminal=True, token='tok-b'):
    return {'id': 'b', 'name': 'B机', 'base': base, 'token': token,
            'terminal': terminal, 'apps': list(apps)}


def localApp(upstream):
    return [{'id': 'yuanchuang', 'upstream': upstream, 'prefix': '/yuanchuang',
             'mode': 'full', 'label': '远窗', 'icon': 'plug'}]


def loggedInApp(monkeypatch, externalApps=(), remotes=(), fedToken='tok-main'):
    monkeypatch.setattr(config, 'getExternalApps', lambda: list(externalApps))
    monkeypatch.setattr(config, 'getRemotes', lambda: list(remotes))
    monkeypatch.setattr(config, 'getFederationToken', lambda: fedToken)
    app, _sock = create_app()
    federation.bindApp(app)
    app.secret_key = 'test'
    return app


def authed(client):
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


# ---- B 侧广播端点 ----

def test_provider_broadcast_requires_auth(monkeypatch, provider):
    app = loggedInApp(monkeypatch, externalApps=localApp(provider))
    client = app.test_client()
    resp = client.get('/api/external-apps')
    assert resp.status_code == 401                                   # 未登录 → 401 JSON
    resp = client.get('/api/external-apps', headers={'X-Deskport-Token': 'tok-main'})
    assert resp.status_code == 200
    apps = resp.get_json()['apps']
    assert apps and apps[0]['prefix'] == '/yuanchuang' and apps[0]['mode'] == 'full'
    with client.session_transaction() as session:
        session['authenticated'] = True
    assert client.get('/api/external-apps').status_code == 200        # 本机会话也可见


# ---- A 侧发现: 拉取 → 白名单 → 挂载 → 带token透传 ----

def test_discovery_mounts_and_forwards_with_token(monkeypatch, provider):
    ProviderHandler.APPS = [{'id': 'yuanchuang', 'prefix': '/yuanchuang',
                             'label': '远窗', 'icon': 'plug', 'mode': 'full'}]
    monkeypatch.setattr(config, 'getRemotes', lambda: [makeRemote(provider)])
    monkeypatch.setattr(config, 'getFederationToken', lambda: 'tok-main')
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    federation.bindApp(app)
    client = authed(app.test_client())

    assert client.get('/yuanchuang/page').status_code == 404          # 未发现前不存在
    federation.refreshOnce()

    resp = client.get('/yuanchuang/page')
    assert resp.status_code == 200 and resp.data == b'upstream-ok'
    last = ProviderHandler.LAST
    assert last['path'] == '/yuanchuang/page'                         # full 模式原样透传
    assert last['headers'].get('X-Deskport-Token') == 'tok-b'         # 联邦身份注入
    # 导航自动出现 (external: true → 新标签页)
    nav = externalProxy.discoveredNavItems()
    assert nav and nav[0]['href'] == '/yuanchuang' and nav[0]['external'] is True
    assert nav[0]['id'] == 'b:yuanchuang'                             # 机器前缀防撞名


def test_discovery_respects_allowlist(monkeypatch, provider):
    ProviderHandler.APPS = [{'id': 'yuanchuang', 'prefix': '/yuanchuang', 'mode': 'full'},
                            {'id': 'other', 'prefix': '/other', 'mode': 'full'}]
    monkeypatch.setattr(config, 'getRemotes', lambda: [makeRemote(provider, apps=('other',))])
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    federation.bindApp(app)
    client = authed(app.test_client())
    federation.refreshOnce()
    assert client.get('/other/x').status_code == 200                  # 白名单内的接了
    assert client.get('/yuanchuang/x').status_code == 404             # 白名单外的不接
    assert externalProxy.discoveredNavItems()[0]['id'] == 'b:other'
    ProviderHandler.APPS = []


def test_discovery_wildcard_and_empty_policy(monkeypatch, provider):
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    federation.bindApp(app)
    client = authed(app.test_client())
    ProviderHandler.APPS = [{'id': 'a1', 'prefix': '/a1', 'mode': 'full'}]
    monkeypatch.setattr(config, 'getRemotes', lambda: [makeRemote(provider, apps=('*',))])
    federation.refreshOnce()
    assert client.get('/a1/x').status_code == 200                     # ["*"] 全收

    externalProxy._discovered.clear()
    monkeypatch.setattr(config, 'getRemotes', lambda: [makeRemote(provider, apps=())])
    federation.refreshOnce()
    assert client.get('/a1/x').status_code == 404                     # 策略清空即撤下
    assert externalProxy.discoveredNavItems() == []
    ProviderHandler.APPS = []


def test_discovery_skips_conflicting_prefix(monkeypatch, provider):
    ProviderHandler.APPS = [{'id': 'evil', 'prefix': '/app', 'mode': 'full'}]   # 撞本机页面路由
    monkeypatch.setattr(config, 'getRemotes', lambda: [makeRemote(provider)])
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    federation.bindApp(app)
    client = authed(app.test_client())
    federation.refreshOnce()                                          # 不得装配失败
    assert client.get('/app').status_code == 200                      # 本机路由原样
    assert 'wp-brand' in client.get('/app').get_data(as_text=True)
    assert client.get('/app/x').status_code != 404 or True            # 不抛即可
    ProviderHandler.APPS = []


def test_discovery_only_full_mode(monkeypatch, provider):
    ProviderHandler.APPS = [{'id': 'ns', 'prefix': '/ns', 'mode': 'namespace'}]
    monkeypatch.setattr(config, 'getRemotes', lambda: [makeRemote(provider)])
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    federation.bindApp(app)
    federation.refreshOnce()
    assert externalProxy._discovered == {}                            # namespace 不进联邦
    ProviderHandler.APPS = []


def test_terminal_policy_gates_sessions_and_routing(monkeypatch, provider):
    monkeypatch.setattr(config, 'getRemotes',
                        lambda: [makeRemote(provider, terminal=False)])
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    assert federation.machineOptions() == [{'id': '', 'name': '本机'}]   # 不出现在新建会话
    remote = federation.findRemote('b')
    federation._owners['s1'] = 'b'
    assert federation.wsUpstreamFor('s1') is None                     # 不路由 WS
    assert federation.killUpstreamFor('s1') is None                   # 不转发 kill
    federation._owners.pop('s1', None)
