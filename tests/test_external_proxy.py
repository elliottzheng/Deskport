# -*- coding: utf-8 -*-
"""外部应用反代挂载测试 — stdlib 上游桩覆盖透传/XFF/SSE/Range/404/流式/两模式/守卫。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config
import modules.external_proxy as externalProxy
from app import create_app


class StubHandler(BaseHTTPRequestHandler):
    """记录请求的上游桩; 类属性 LAST 保存最近一次请求供断言。"""
    LAST = {}

    def _echo(self, code=None, extraHeaders=None, body=None):
        StubHandler.LAST = {
            'path': self.path,
            'headers': dict(self.headers.items()),
            'method': self.command,
            'body': self.rfile.read(int(self.headers.get('Content-Length') or 0)),
        }
        if body is None:
            body = b'upstream-ok'
        self.send_response(code if code is not None else 200)
        for key, value in (extraHeaders or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('X-Upstream-Header', 'yes')
        self.send_header('Connection', 'keep-alive')  # hop-by-hop, 应被剥掉
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path.endswith('/') and path != '/':
            path = path.rstrip('/')
        branch = path[len('/yuanchuang'):] if path.startswith('/yuanchuang') else path
        if branch == '/sse':
            # SSE: 手写 chunked 逐条推送
            StubHandler.LAST = {'path': self.path, 'headers': dict(self.headers.items()),
                                'method': self.command, 'body': b''}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for i in range(3):
                payload = ('data: event-%d\n\n' % i).encode()
                self.wfile.write(b'%x\r\n%s\r\n' % (len(payload), payload))
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            return
        if branch == '/range-target':
            self._echo(code=206, extraHeaders={'Content-Range': 'bytes 2-5/11'},
                       body=b'cdef')
            return
        if branch == '/missing':
            self._echo(code=404, body=b'{"error": "nope"}')
            return
        self._echo()

    def do_POST(self):
        self._echo()

    def do_HEAD(self):
        self._echo()

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def upstream():
    server = ThreadingHTTPServer(('127.0.0.1', 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:%d' % server.server_address[1]
    server.shutdown()


def makeApp(monkeypatch, upstream, mode='full', prefix='/yuanchuang'):
    monkeypatch.setattr(config, 'getExternalApps', lambda: [{
        'id': 'yuanchuang', 'upstream': upstream, 'prefix': prefix,
        'mode': mode, 'label': '远窗', 'icon': 'plug',
    }])
    app, _sock = create_app()
    app.secret_key = 'test'
    with app.test_client() as client:
        with client.session_transaction() as session:
            session['authenticated'] = True
        yield client
    externalProxy.register(app, [])  # 清登记, 避免串到下一个用例


def loginOtherApp(monkeypatch):
    """切一个无外部应用的 app 实例, 测未登录守卫。"""
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    return app.test_client()


# ---- full 模式 ----

def test_full_passthrough_path_and_headers(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'full'))
    resp = client.get('/yuanchuang/some/page?q=1')
    assert resp.status_code == 200
    assert resp.data == b'upstream-ok'
    assert resp.headers['X-Upstream-Header'] == 'yes'
    last = StubHandler.LAST
    # full 模式: 原样透传, 不剥前缀
    assert last['path'] == '/yuanchuang/some/page?q=1'
    assert last['headers']['X-Forwarded-Proto'] == 'http'
    assert last['headers']['X-Forwarded-Host']
    # hop-by-hop 响应头被剥
    assert 'Connection' not in resp.headers


def test_mount_root_trailing_slash_proxies(monkeypatch, upstream):
    """/<prefix>/（尾斜杠）也要命中: 登录守卫 302 的 next 常带尾斜杠, 404 会断登录回流。"""
    client = next(makeApp(monkeypatch, upstream, 'full'))
    resp = client.get('/yuanchuang/')
    assert resp.status_code == 200
    assert StubHandler.LAST['path'] == '/yuanchuang/'


def test_xff_appends_remote_addr(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'full'))
    client.get('/yuanchuang/page', headers={'X-Forwarded-For': '10.0.0.1'})
    assert StubHandler.LAST['headers']['X-Forwarded-For'].endswith(', 127.0.0.1')
    assert StubHandler.LAST['headers']['X-Forwarded-For'].startswith('10.0.0.1')
    client.get('/yuanchuang/page')
    assert StubHandler.LAST['headers']['X-Forwarded-For'] == '127.0.0.1'


def test_post_body_streamed(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'full'))
    big = b'x' * (256 * 1024)
    resp = client.post('/yuanchuang/upload', data=big,
                       headers={'Content-Type': 'application/octet-stream'})
    assert resp.status_code == 200
    assert StubHandler.LAST['body'] == big


def test_sse_streams_live(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'full'))
    resp = client.get('/yuanchuang/sse')
    assert resp.status_code == 200
    assert resp.is_streamed  # gzipText 会跳过, SSE 不被缓冲
    assert b'event-0' in resp.data and b'event-2' in resp.data


def test_range_206_passthrough(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'full'))
    resp = client.get('/yuanchuang/range-target', headers={'Range': 'bytes=2-5'})
    assert resp.status_code == 206
    assert resp.headers['Content-Range'] == 'bytes 2-5/11'
    assert resp.data == b'cdef'
    assert StubHandler.LAST['headers']['Range'] == 'bytes=2-5'


def test_upstream_404_passthrough(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'full'))
    resp = client.get('/yuanchuang/missing')
    assert resp.status_code == 404
    assert resp.data == b'{"error": "nope"}'


# ---- namespace 模式 ----

def test_namespace_strips_prefix(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'namespace'))
    resp = client.get('/yuanchuang/page.html')
    assert resp.status_code == 200
    assert StubHandler.LAST['path'] == '/page.html'


def test_namespace_x_passthrough(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'namespace'))
    resp = client.get('/_x/api/data?k=v')
    assert resp.status_code == 200
    assert StubHandler.LAST['path'] == '/_x/api/data?k=v'


# ---- 守卫 ----

def test_guard_redirects_unlogged_page(monkeypatch, upstream):
    monkeypatch.setattr(config, 'getExternalApps', lambda: [{
        'id': 'yuanchuang', 'upstream': upstream, 'prefix': '/yuanchuang',
        'mode': 'namespace', 'label': '远窗', 'icon': 'plug',
    }])
    app, _sock = create_app()
    resp = app.test_client().get('/yuanchuang/index.html')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_guard_api_form_401_json(monkeypatch, upstream):
    monkeypatch.setattr(config, 'getExternalApps', lambda: [{
        'id': 'yuanchuang', 'upstream': upstream, 'prefix': '/yuanchuang',
        'mode': 'namespace', 'label': '远窗', 'icon': 'plug',
    }])
    app, _sock = create_app()
    resp = app.test_client().get('/yuanchuang/api/state')
    assert resp.status_code == 401
    assert resp.get_json() == {'error': 'unauthorized'}


# ---- 冲突检测与开关 ----

def test_prefix_conflict_rejected(monkeypatch, upstream):
    monkeypatch.setattr(config, 'getExternalApps', lambda: [{
        'id': 'bad', 'upstream': upstream, 'prefix': '/assets',
        'mode': 'full', 'label': 'bad', 'icon': 'plug',
    }])
    with pytest.raises(ValueError, match='冲突'):
        create_app()


def test_disabled_config_no_routes_no_nav(monkeypatch, upstream):
    monkeypatch.setattr(config, 'getExternalApps', lambda: [])
    app, _sock = create_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert not any(rule.startswith('/yuanchuang') for rule in rules)
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    assert client.get('/yuanchuang/').status_code == 404
    assert externalProxy.getNavItems() == []


def test_nav_injected_into_pages(monkeypatch, upstream):
    client = next(makeApp(monkeypatch, upstream, 'namespace'))
    page = client.get('/app').get_data(as_text=True)
    assert '__WP_PLUGIN_NAV__' in page
    assert '/yuanchuang' in page
    assert '远窗' in page


def test_config_parses_external_apps():
    """getExternalApps 对真实 config.json 缺省返回列表 (live 配置不含该节)。"""
    apps = config.getExternalApps()
    assert isinstance(apps, list)
    assert json.dumps(apps)  # 可序列化
