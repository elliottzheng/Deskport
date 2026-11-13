# -*- coding: utf-8 -*-
"""联邦文件浏览测试 — 成员桩上的只读转发/策略/审计/流式/离线/本机回归。

桩 (MemberHandler) 与真成员 Deskport 同形地伺服 /api/files* 只读端点,
并且每个请求都校验 X-Deskport-Token —— 各转发用例由此隐式断言中枢带了
成员令牌。断网成员用死端口, 不依赖任何真实 B 机在线。
"""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

import config
from modules import events as eventsModule
from modules import federation
from app import create_app

DOWNLOAD_BYTES = bytes(range(256)) * 8192   # 2MB 固定图案, 验证流式完整
FED_TOKEN = 'fed-tok-0001'


class MemberHandler(BaseHTTPRequestHandler):
    TOKEN = 'tok-b'
    LAST = {}          # 最近一次请求 {path, method, headers(小写键), body}

    def _record(self):
        MemberHandler.LAST = {
            'path': self.path,
            'method': self.command,
            'headers': {k.lower(): v for k, v in self.headers.items()},
            'body': b'',
        }

    def _send(self, payload, ctype='application/json', status=200):
        body = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        if self.headers.get('X-Deskport-Token') != MemberHandler.TOKEN:
            return self._send({'error': 'bad token'}, status=403)
        if self.path.startswith('/api/files/federation-info'):
            return self._send({'maxUploadMB': 1, 'writeRootsConfigured': True})
        if self.path.startswith('/api/files/download'):
            return self._send(DOWNLOAD_BYTES, ctype='application/octet-stream')
        if self.path.startswith('/api/files/thumb'):
            return self._send(b'\x89PNG\r\n\x1a\n', ctype='image/png')
        if self.path.startswith('/api/files/search'):
            return self._send({'results': [], 'truncated': False})
        if self.path.startswith('/api/files'):
            return self._send({'currentPath': '/', 'parentPath': None, 'entries': [
                {'name': 'notes.txt', 'path': 'C:\\m\\notes.txt', 'type': 'file',
                 'size': 3, 'mtime': 1700000000, 'hidden': False,
                 'system': False, 'readonly': False}]})
        return self._send({'error': 'not found'}, status=404)

    def do_POST(self):
        self._record()
        length = int(self.headers.get('Content-Length') or 0)
        MemberHandler.LAST['body'] = self.rfile.read(length) if length else b''
        if self.headers.get('X-Deskport-Token') != MemberHandler.TOKEN:
            return self._send({'error': 'bad token'}, status=403)
        if self.path.startswith('/api/files/read'):
            return self._send({'content': 'from-member', 'size': 11,
                               'encoding': 'utf-8', 'lossy': False,
                               'truncated': False, 'mtime': 1700000000,
                               'sha256': '0' * 64})
        for op in ('rename', 'new-folder', 'recycle-delete', 'paste', 'upload',
                   'write'):
            if self.path.startswith('/api/files/' + op):
                return self._send({'ok': True, 'op': op})
        return self._send({'error': 'not found'}, status=404)

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def member():
    server = ThreadingHTTPServer(('127.0.0.1', 0), MemberHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:%d' % server.server_address[1]
    server.shutdown()


@pytest.fixture(autouse=True)
def cleanFederation(monkeypatch):
    """刷新线程不启动; 联邦缓存每用例清零。"""
    from modules import files_federation
    monkeypatch.setattr(federation, '_ensureThread', lambda force=False: None)
    federation._cache.clear()
    files_federation._limitCache.clear()
    yield
    federation._cache.clear()
    files_federation._limitCache.clear()


def remoteEntry(base, files=True, mid='b', token='tok-b', filesWrite=False):
    return {'id': mid, 'name': 'B机' if mid == 'b' else mid, 'base': base,
            'token': token, 'terminal': False, 'apps': [], 'files': files,
            'filesWrite': filesWrite, 'models': [], 'model': ''}


def makeHub(monkeypatch, remotes, brand='中枢A', audit=None):
    """装配一个已登录的中枢 test client; remotes 为 config.getRemotes 替身。"""
    monkeypatch.setattr(config, 'getRemotes', lambda: [dict(r) for r in remotes])
    monkeypatch.setattr(config, 'getBrandName', lambda: brand)
    monkeypatch.setattr(config, 'getFederationToken', lambda: FED_TOKEN)
    if audit is not None:
        def capture(action, target='', result='ok', detail='', ip=''):
            audit.append({'action': action, 'target': target, 'result': result,
                          'detail': detail, 'ip': ip})
        monkeypatch.setattr(eventsModule, 'log', capture)
    app, _sock = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def fedAudit(audit):
    return [a for a in audit if a['action'] == 'files.federation']


# ---------------------------------------------------------------- 转发

def test_browse_forwards_with_token_and_strips_machine(member, monkeypatch):
    audit = []
    for c in makeHub(monkeypatch, [remoteEntry(member)], audit=audit):
        MemberHandler.LAST = {}
        r = c.get('/api/files?path=%2F&machine=b')
        assert r.status_code == 200
        data = r.get_json()
        assert data['entries'][0]['name'] == 'notes.txt'
        last = MemberHandler.LAST
        # 成员令牌 + 发起方身份都带到了
        assert last['headers']['x-deskport-token'] == 'tok-b'
        assert last['headers']['x-deskport-from'] == quote('中枢A', safe='')
        # machine 参数已剥掉, 不会在成员端造成回环
        assert 'machine=' not in last['path']
        assert 'Cookie' not in last['headers']
    entries = fedAudit(audit)
    assert entries and entries[-1]['target'] == 'B机'
    assert entries[-1]['result'] == 'ok' and 'browse' in entries[-1]['detail']


def test_read_machine_in_json_body_is_stripped(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/read', json={'path': 'C:\\m\\notes.txt', 'machine': 'b'})
        assert r.status_code == 200
        assert r.get_json()['content'] == 'from-member'
        body = json.loads(MemberHandler.LAST['body'].decode('utf-8'))
        assert body == {'path': 'C:\\m\\notes.txt'}   # machine 已从 body 剥掉
        assert MemberHandler.LAST['path'].startswith('/api/files/read')


def test_download_streams_fully_and_passthrough_range(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member)]):
        MemberHandler.LAST = {}
        r = c.get('/api/files/download?path=big.bin&machine=b')
        assert r.status_code == 200
        assert r.data == DOWNLOAD_BYTES                       # 2MB 完整到达
        assert r.headers['Content-Type'].startswith('application/octet-stream')
        assert MemberHandler.LAST['path'].startswith('/api/files/download')
        r2 = c.get('/api/files/download?path=big.bin&machine=b',
                   headers={'Range': 'bytes=0-99'})
        assert r2.status_code == 200
        assert MemberHandler.LAST['headers'].get('range') == 'bytes=0-99'  # Range 原样透传


# ---------------------------------------------------------------- 策略与边界

def test_policy_off_rejected_without_outbound(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, files=False)]):
        MemberHandler.LAST = {}
        r = c.get('/api/files?path=%2F&machine=b')
        assert r.status_code == 403
        assert '未开放文件浏览' in r.get_json()['error']
        assert MemberHandler.LAST == {}   # 策略关 = 一枚请求都不外发


def test_unknown_member_404(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member)]):
        r = c.get('/api/files?path=%2F&machine=zz')
        assert r.status_code == 404


def test_write_ops_blocked_in_member_mode(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/rename?machine=b',
                   json={'path': 'C:\\m\\a.txt', 'newName': 'b.txt'})
        assert r.status_code == 403
        r2 = c.post('/api/files/paste?machine=b',
                    json={'mode': 'copy', 'paths': ['C:\\m\\a.txt'], 'dest': 'C:\\n'})
        assert r2.status_code == 403
        assert MemberHandler.LAST == {}   # 写操作一律不外发


def test_files_write_switch_off_blocks_write(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, filesWrite=False)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/rename?machine=b',
                   json={'path': 'C:\\m\\a.txt', 'newName': 'b.txt'})
        assert r.status_code == 403
        assert 'filesWrite' in r.get_json()['error']
        assert MemberHandler.LAST == {}


def test_files_write_requires_files_master_switch(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, files=False, filesWrite=True)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/rename?machine=b',
                   json={'path': 'C:\\m\\a.txt', 'newName': 'b.txt'})
        assert r.status_code == 403
        assert '未开放文件浏览' in r.get_json()['error']
        assert MemberHandler.LAST == {}


def test_write_forwarded_with_actor_and_machine_stripped(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, filesWrite=True)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/rename?machine=b',
                   json={'path': 'C:\\m\\a.txt', 'newName': 'b.txt'})
        assert r.status_code == 200
        assert r.get_json()['op'] == 'rename'
        last = MemberHandler.LAST
        assert last['path'].startswith('/api/files/rename')
        assert 'machine=' not in last['path']
        assert json.loads(last['body'].decode('utf-8')) == {
            'path': 'C:\\m\\a.txt', 'newName': 'b.txt'}
        assert last['headers']['x-deskport-token'] == 'tok-b'
        assert last['headers']['x-deskport-actor'] == '127.0.0.1'


def test_permanent_delete_never_forwarded(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, filesWrite=True)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/delete?machine=b', json={'paths': ['C:\\m\\a.txt']})
        assert r.status_code == 403
        assert MemberHandler.LAST == {}


def test_upload_forwarded_streaming(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, filesWrite=True)]):
        MemberHandler.LAST = {}
        r = c.post('/api/files/upload?machine=b',
                   data={'path': 'C:\\m', 'file': (io.BytesIO(b'hello'), 'a.txt')},
                   content_type='multipart/form-data')
        assert r.status_code == 200, r.get_data(as_text=True)
        last = MemberHandler.LAST
        assert last['path'].startswith('/api/files/upload')
        assert 'machine=' not in last['path']
        assert b'hello' in last['body']
        assert last['headers']['content-type'].startswith('multipart/form-data')


def test_upload_over_member_limit_rejected_at_hub(member, monkeypatch):
    for c in makeHub(monkeypatch, [remoteEntry(member, filesWrite=True)]):
        MemberHandler.LAST = {}
        payload = b'x' * (1024 * 1024 + 100 * 1024)
        r = c.post('/api/files/upload?machine=b',
                   data={'path': 'C:\\m', 'file': (io.BytesIO(payload), 'big.bin')},
                   content_type='multipart/form-data')
        assert r.status_code == 413
        assert '上限' in r.get_json()['error']
        # 只发了 federation-info 探测，没有发上传体
        assert MemberHandler.LAST.get('path', '').startswith('/api/files/federation-info')


def test_relay_forbidden(member, monkeypatch):
    """成员端收到 From + machine 时按成员模式本地处理，绝不接力。"""
    for c in makeHub(monkeypatch, [remoteEntry(member)]):
        MemberHandler.LAST = {}
        r = c.get('/api/files?path=%2F&machine=b',
                  headers={'X-Deskport-From': quote('上游X', safe='')})
        assert r.status_code == 200
        assert MemberHandler.LAST == {}


def test_member_offline_502_with_audit(monkeypatch):
    audit = []
    for c in makeHub(monkeypatch, [remoteEntry('http://127.0.0.1:1', mid='dead')], audit=audit):
        r = c.get('/api/files?path=%2F&machine=dead')
        assert r.status_code == 502
        assert '离线' in r.get_json()['error']
    entries = fedAudit(audit)
    assert entries and entries[-1]['result'] == 'fail'


# ---------------------------------------------------------------- 本机不回归

def test_local_browse_unaffected(tmp_path, member, monkeypatch):
    (tmp_path / 'local.txt').write_text('hi', encoding='utf-8')
    for c in makeHub(monkeypatch, [remoteEntry(member)]):
        MemberHandler.LAST = {}
        r = c.get('/api/files?path=' + quote(str(tmp_path), safe=''))
        assert r.status_code == 200
        names = [e['name'] for e in r.get_json()['entries']]
        assert 'local.txt' in names
        assert MemberHandler.LAST == {}   # 没带 machine → 不外发


def test_member_side_audit_passthrough(tmp_path, member, monkeypatch):
    """成员视角: 带中枢令牌 + From 头的请求放行本机路由并记审计。"""
    (tmp_path / 'local.txt').write_text('hi', encoding='utf-8')
    audit = []
    for c in makeHub(monkeypatch, [remoteEntry(member)], audit=audit):
        MemberHandler.LAST = {}
        r = c.get('/api/files?path=' + quote(str(tmp_path), safe=''),
                  headers={'X-Deskport-Token': FED_TOKEN,
                           'X-Deskport-From': quote('中枢X', safe='')})
        assert r.status_code == 200                       # 本地正常响应
        assert 'local.txt' in [e['name'] for e in r.get_json()['entries']]
        assert MemberHandler.LAST == {}                   # 没有再外发
    entries = fedAudit(audit)
    assert entries and entries[-1]['target'] == '来自 中枢X'
    assert 'actor=' in entries[-1]['detail']


def test_member_receiving_machine_does_not_relay(tmp_path, member, monkeypatch):
    """单跳: 成员同时收到 From 与 machine，也只在本机处理，绝不二次转发。"""
    (tmp_path / 'local.txt').write_text('hi', encoding='utf-8')
    for c in makeHub(monkeypatch, [remoteEntry(member, filesWrite=True)]):
        MemberHandler.LAST = {}
        r = c.get('/api/files?path=' + quote(str(tmp_path), safe='') + '&machine=b',
                  headers={'X-Deskport-From': quote('中枢X', safe=''),
                           'X-Deskport-Actor': '10.9.9.9'})
        assert r.status_code == 200
        assert 'local.txt' in [e['name'] for e in r.get_json()['entries']]
        assert MemberHandler.LAST == {}                   # 成员没有再外发


# ---------------------------------------------------------------- 配置面

def test_file_machines_filters_by_policy(monkeypatch, member):
    monkeypatch.setattr(config, 'getRemotes',
                        lambda: [remoteEntry(member), remoteEntry(member + 'x', files=False, mid='c')])
    assert federation.fileMachines() == [
        {'id': '', 'name': '本机'}, {'id': 'b', 'name': 'B机'}]


def test_settings_files_policy_roundtrip(tmp_path, monkeypatch, member):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'port': 6788, 'password': 'test'}), encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', str(cfgfile))
    config._cache.update(mtime=None, data=None)
    try:
        remotes = [{'id': 'b', 'name': 'B机', 'base': member, 'token': 'tok-b',
                    'terminal': True, 'files': True, 'apps': []}]
        config.setRemotes(remotes)
        got = config.getRemotes()
        assert got[0]['files'] is True
        assert 'files' in got[0]
        # 设置接口校验: files 非布尔拒绝
        assert config.getRemotes()[0]['files'] is True
    finally:
        config._cache.update(mtime=None, data=None)


def test_settings_api_rejects_non_bool_files(tmp_path, monkeypatch, member):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'port': 6788, 'password': 'test'}), encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', str(cfgfile))
    config._cache.update(mtime=None, data=None)
    try:
        app, _sock = create_app()
        app.config.update(TESTING=True)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess['authenticated'] = True
            r = c.post('/api/settings/federation/remotes',
                       json={'remotes': [{'id': 'b', 'name': 'B机', 'base': member,
                                          'token': 'tok-b', 'terminal': True,
                                          'files': 'yes', 'apps': []}]})
            assert r.status_code == 400
            r2 = c.post('/api/settings/federation/remotes',
                        json={'remotes': [{'id': 'b', 'name': 'B机', 'base': member,
                                           'token': 'tok-b', 'terminal': True,
                                           'files': True, 'apps': []}]})
            assert r2.status_code == 200
            saved = json.loads(cfgfile.read_text(encoding='utf-8'))['remotes']
            assert saved[0]['files'] is True
    finally:
        config._cache.update(mtime=None, data=None)
