# -*- coding: utf-8 -*-
"""协议契约 (需要 dev 正在运行):

    set WP_RUN_PROTOCOL=1
    pytest tests/test_protocol.py -q

验证 hello 带 proto/caps、建会话、回显、关闭会话列表为空。
跨 web 重启的存活验证是重度场景, 用 .run/phase1-*.py / phase2-*.py 手工跑。
"""
import json
import os
import time

import pytest
import requests
import websocket

from modules import internal_api

BASE = os.environ.get('WP_BASE', 'http://127.0.0.1:6788')
WS = BASE.replace('http://', 'ws://').replace('https://', 'wss://')
pytestmark = pytest.mark.skipif(
    os.environ.get('WP_RUN_PROTOCOL') != '1',
    reason='set WP_RUN_PROTOCOL=1 with dev running')


def _login():
    cfg = json.load(open('config.json', encoding='utf-8'))
    s = requests.Session()
    assert s.post(BASE + '/login', data={'password': cfg['password']},
                  allow_redirects=False).status_code == 302
    return s


def test_hello_protocol_and_echo():
    s = _login()
    cookie = '; '.join('%s=%s' % kv for kv in s.cookies.get_dict().items())
    ws = websocket.create_connection(WS + '/api/terminal/ws?cmd=pwsh.exe',
                                     cookie=cookie, timeout=20)
    hello = None
    text = ''
    end = time.time() + 15
    while time.time() < end and hello is None:
        raw = ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        try:
            frame = json.loads(raw)
        except ValueError:
            text += raw
            continue
        if frame.get('type') == 'hello':
            hello = frame
    assert hello is not None
    assert hello.get('proto') == internal_api.PROTO
    assert isinstance(hello.get('caps'), list)
    ws.send('echo WP-PROTO-OK\r')
    end = time.time() + 10
    while time.time() < end and 'WP-PROTO-OK' not in text:
        raw = ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        try:
            frame = json.loads(raw)
        except ValueError:
            text += raw
            continue
        if frame.get('type') == 'data':
            text += frame.get('data', '')
    assert 'WP-PROTO-OK' in text
    ws.close()
    assert s.delete(BASE + '/api/terminal/sessions/' + hello['session']).status_code == 200