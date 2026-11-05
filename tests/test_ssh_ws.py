# -*- coding: utf-8 -*-
"""SSH WS 附着回归（需要 dev 正在运行 + fakessh 本地桩服务）:

    set WP_RUN_SSH_WS=1
    pytest tests/test_ssh_ws.py -q

覆盖 wsproto.attachAndPump 在 SSH 侧的落地: hello(kind=ssh)、回显、
重连 resumed、断开后会话存活、会话列表 kind 过滤、清理。
不依赖真实 SSH 服务器, 也不依赖用户主机库的既有数据。
"""
import json
import os
import sys
import time

import pytest
import requests
import websocket

BASE = os.environ.get('WP_BASE', 'http://127.0.0.1:6799')
WS = BASE.replace('http://', 'ws://').replace('https://', 'wss://')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    os.environ.get('WP_RUN_SSH_WS') != '1',
    reason='set WP_RUN_SSH_WS=1 with dev running')

_created = []


def _login():
    cfg = json.load(open('config.json', encoding='utf-8'))
    s = requests.Session()
    assert s.post(BASE + '/login', data={'password': cfg['password']},
                  allow_redirects=False).status_code == 302
    return s


@pytest.fixture(scope='module')
def fakeServer(tmp_path_factory):
    from fakessh_server import Handler, Server, USER_PASSWORD
    Server.root = str(tmp_path_factory.mktemp('fakessh-root'))
    srv = Server(('127.0.0.1', 0), Handler)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, USER_PASSWORD
    srv.shutdown()


@pytest.fixture(scope='module')
def session():
    return _login()


@pytest.fixture(scope='module')
def sshHost(fakeServer, session):
    """注册 fakessh 主机并完成 TOFU; 结束时删除主机与连接。"""
    srv, (user, password) = fakeServer
    port = srv.server_address[1]
    resp = session.post(BASE + '/api/ssh/hosts', json={
        'name': 'ws-regress-fake', 'host': '127.0.0.1', 'port': port,
        'user': user, 'authType': 'password', 'password': password})
    assert resp.status_code == 200, resp.text
    hostId = resp.json()['id']
    _created.append(hostId)

    chk = session.get(BASE + '/api/ssh/hostkey/check', params={'hostId': hostId})
    assert chk.status_code == 200, chk.text
    assert chk.json()['status'] == 'new'
    trust = session.post(BASE + '/api/ssh/hostkey/trust', json={
        'host': '127.0.0.1', 'port': port,
        'fingerprint': chk.json()['fingerprint']})
    assert trust.status_code == 200

    yield hostId

    session.delete(BASE + '/api/ssh/hosts/' + hostId)
    session.post(BASE + '/api/ssh/disconnect', json={'hostId': hostId})


def _cookie(s):
    return '; '.join('%s=%s' % kv for kv in s.cookies.get_dict().items())


def _connect(http, hostId, **query):
    q = 'hostId=%s' % hostId
    for k, v in query.items():
        q += '&%s=%s' % (k, v)
    ws = websocket.create_connection(WS + '/api/ssh/ws?' + q,
                                     cookie=_cookie(http), timeout=20)
    ws.settimeout(20)
    return ws


def test_ssh_ws_hello_echo_and_resume(sshHost, session):
    ws = _connect(session, sshHost)
    hello = None
    text = ''
    deadline = time.time() + 20
    while time.time() < deadline and hello is None:
        raw = ws.recv()
        raw = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
        try:
            frame = json.loads(raw)
        except ValueError:
            text += raw
            continue
        if frame.get('type') == 'hello':
            hello = frame
    assert hello is not None, '未收到 hello 帧'
    assert hello['kind'] == 'ssh'
    assert hello['label'] == 'ws-regress-fake'
    assert hello['proto'] >= 1
    realId = hello['session']

    ws.send('echo SSH_WS_REGRESS_OK\r')
    deadline = time.time() + 15
    while time.time() < deadline and 'SSH_WS_REGRESS_OK' not in text:
        raw = ws.recv()
        text += raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
    assert 'SSH_WS_REGRESS_OK' in text

    # 会话列表: SSH 会话走 /api/ssh/sessions (与本地终端是两个端点)
    sshList = session.get(BASE + '/api/ssh/sessions').json()['sessions']
    assert any(s['id'] == realId and s['kind'] == 'ssh' for s in sshList),         'SSH 会话应出现在 /api/ssh/sessions'

    ws.close()
    time.sleep(1.0)

    # 断开后仍存活, 且 attached 归零
    after = session.get(BASE + '/api/ssh/sessions').json()['sessions']
    mine = [s for s in after if s['id'] == realId]
    assert mine, '断开后 SSH 会话应继续存活'
    assert mine[0]['attached'] is False

    # 重连: resumed=True, 同一会话, 回放含旧输出
    ws = _connect(session, sshHost, session=realId, replay='1')
    resumed = None
    replay = ''
    deadline = time.time() + 10
    while time.time() < deadline:
        raw = ws.recv()
        raw = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
        try:
            frame = json.loads(raw)
        except ValueError:
            replay += raw
            continue
        if frame.get('type') == 'hello':
            resumed = frame
            break
    assert resumed is not None and resumed['resumed'] is True
    assert resumed['session'] == realId
    deadline = time.time() + 8
    while time.time() < deadline and 'SSH_WS_REGRESS_OK' not in replay:
        raw = ws.recv()
        replay += raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
    assert 'SSH_WS_REGRESS_OK' in replay, '回放应含断开前的输出'
    ws.close()


def test_ssh_ws_unknown_host_sends_error(session):
    ws = _connect(session, 'no-such-host-id')
    raw = ws.recv()
    raw = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
    frame = json.loads(raw)
    assert frame.get('type') == 'error'
    assert '主机不存在' in frame.get('msg', '')
    ws.close()
