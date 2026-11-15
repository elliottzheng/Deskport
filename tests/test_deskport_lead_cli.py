# -*- coding: utf-8 -*-
"""批次 SR 例 7：CLI `deskport.py lead --register <名字>` 真跑（桩服务器）。

起一个本地假 Deskport（只实现 GET/POST /api/agent/lead，照服务端语义回），
照外部智能体的路径真跑 CLI：注册成功输出主管名与 id 退出 0；已绑定的令牌
再注册别的名字 → 打印现有主管退出 0（幂等友好）；名字被别的主管占用而本
令牌未绑 → 报错退出 2，不冒充成功。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, '.agents', 'skills', 'deskport', 'deskport.py')
STUB_TOKEN = 'tok-stub-lead'          # 合成令牌，只进请求头，不落任何对外文件


class _LeadHandler(BaseHTTPRequestHandler):
    """假 Deskport：只实现 GET/POST /api/agent/lead，照服务端语义回。"""

    lead = None      # 已注册的主管记录（None = 本令牌未绑）

    def _json(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != '/api/agent/lead':
            self._json({'error': 'not found'}, 404)
            return
        self._json({'lead': _LeadHandler.lead})

    def do_POST(self):
        if self.path != '/api/agent/lead':
            self._json({'error': 'not found'}, 404)
            return
        req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))
                         or b'{}')
        name = str(req.get('name') or '')
        lead = _LeadHandler.lead
        if lead is not None:
            if lead['name'] == name:
                self._json({'lead': lead}, 200)
            else:
                self._json({'error': '该令牌已绑定主管 %s' % lead['name'],
                            'lead': lead}, 409)
            return
        if name == '甲':
            _LeadHandler.lead = {'id': 'ld_abcd1234', 'name': '甲',
                                 'project': req.get('project') or '',
                                 'kind': 'external', 'parent': 'human',
                                 'status': 'active'}
            self._json({'lead': _LeadHandler.lead}, 201)
        else:
            self._json({'error': '已存在同名主管: %s（换个名字）' % name}, 409)

    def log_message(self, *a):
        pass


@pytest.fixture
def leadServer():
    _LeadHandler.lead = None
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _LeadHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d' % srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    _LeadHandler.lead = None


def _runLeadCli(tmp_path, url, *args):
    d = tmp_path / ('cli-%d' % len(args))
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLIENT, d / 'deskport.py')
    (d / '.agent-token').write_text(STUB_TOKEN, encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.endswith('_PROXY')}
    for k in ('DESKPORT_TOKEN', 'DESKPORT_URL', 'DESKPORT_NODE', 'DESKPORT_INSECURE'):
        env.pop(k, None)
    p = subprocess.run([sys.executable, 'deskport.py', '--url', url, 'lead', *args],
                       cwd=str(d), env=env, capture_output=True, encoding='utf-8',
                       errors='replace', timeout=90)
    return p.returncode, p.stdout, p.stderr


def test_cli_register_prints_name_and_id(leadServer, tmp_path):
    """例 7a：lead --register "甲" → 输出主管名与 id，退出 0。"""
    code, out, err = _runLeadCli(tmp_path, leadServer, '--register', '甲')
    assert code == 0, err
    assert '甲' in out and 'ld_abcd1234' in out
    assert 'Traceback' not in err


def test_cli_register_already_bound_prints_existing(leadServer, tmp_path):
    """例 7b：已绑定令牌再 register 别的名字 → 打印现有主管，退出 0。"""
    assert _runLeadCli(tmp_path / 'a', leadServer, '--register', '甲')[0] == 0
    code, out, err = _runLeadCli(tmp_path / 'b', leadServer, '--register', '乙')
    assert code == 0, err
    assert '甲' in out and 'ld_abcd1234' in out
    bare = _runLeadCli(tmp_path / 'c', leadServer)
    assert bare[0] == 0 and '甲' in bare[1]


def test_cli_register_name_taken_fails(leadServer, tmp_path):
    """例 7c：名字被别的主管占用（本令牌未绑）→ 报错退出 2，不冒充成功。"""
    code, out, err = _runLeadCli(tmp_path, leadServer, '--register', '丙')
    assert code == 2
    assert '同名' in err and 'Traceback' not in err
    assert 'Traceback' not in out
