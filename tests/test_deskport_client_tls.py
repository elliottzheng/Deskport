# -*- coding: utf-8 -*-
"""批次 M：外部智能体照 SKILL.md 走到第一步就撞自签证书——报错必须自己说清怎么修。

起一个本地 HTTPS 假 Deskport（openssl 自签证书），照外部智能体的路径真跑 CLI：
无 insecure → 输出「三种处理」、exit 2、无 traceback；insecure / cafile → 同一命令成功。
另覆盖缺令牌、.deskport.json 的 url/node 为空，以及 URLError 里包着证书异常时的翻译
（insecure 缺省仍是 False，绝不因为探测到自签就自动放宽）。
"""
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, '.agents', 'skills', 'deskport', 'deskport.py')
TOKEN = 'tok-synthetic'          # 合成令牌，只进请求头，不落任何对外文件


def _load():
    """把技能目录里的 deskport.py 当模块加载（与 tests/test_task_cwd.py 同一套）。"""
    spec = importlib.util.spec_from_file_location('deskport_skill_client', CLIENT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses 解析注解时要能查到模块
    spec.loader.exec_module(mod)
    return mod


def _freePort():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _noProxyEnv():
    e = {k: v for k, v in os.environ.items() if not k.endswith('_PROXY')}
    for k in ('DESKPORT_TOKEN', 'DESKPORT_URL', 'DESKPORT_NODE', 'DESKPORT_INSECURE'):
        e.pop(k, None)
    return e


class _Handler(BaseHTTPRequestHandler):
    """假 Deskport：只实现 exec 一个端点，回固定结果。"""

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.dumps({'exit': 0, 'out': 'hi\n', 'err': '', 'timedOut': False,
                           'outTruncated': False, 'durationMs': 1}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):    # 别把请求日志泼进测试输出
        pass


@pytest.fixture
def tls(tmp_path):
    """本地 HTTPS 假服务器（自签证书，CN=127.0.0.1）；没有 openssl 就跳过。"""
    openssl = shutil.which('openssl')
    if not openssl:
        pytest.skip('没有 openssl，起不了本地 HTTPS 假服务器')
    cert, key = str(tmp_path / 'cert.pem'), str(tmp_path / 'key.pem')
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', key,
                    '-out', cert, '-days', '2', '-nodes', '-subj', '/CN=127.0.0.1',
                    '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'],
                   check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = ThreadingHTTPServer(('127.0.0.1', _freePort()), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'https://127.0.0.1:%d' % srv.server_address[1], cert
    srv.shutdown()
    srv.server_close()


def _runCli(workdir, cfg):
    """在干净目录里真跑 `python deskport.py run "echo hi"`；返回 (码, stdout, stderr)。"""
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLIENT, workdir / 'deskport.py')
    (workdir / '.agent-token').write_text(TOKEN, encoding='utf-8')
    (workdir / '.deskport.json').write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
    p = subprocess.run([sys.executable, 'deskport.py', 'run', 'echo hi'], cwd=str(workdir),
                       env=_noProxyEnv(), capture_output=True, encoding='utf-8',
                       errors='replace', timeout=90)
    return p.returncode, p.stdout, p.stderr


def test_cert_verify_failure_says_what_to_do(tls, tmp_path):
    """a) 无 insecure 打自签 HTTPS：输出「三种处理」、exit 2、不许有 traceback。"""
    url, _cert = tls
    code, out, err = _runCli(tmp_path / 'plain', {'url': url, 'node': 'local'})
    assert code == 2
    assert 'Traceback' not in out + err
    assert '对端证书校验失败' in err and '自签证书' in err
    for line in ('三种处理，按可信度从高到低：', 'cafile', '"insecure": true',
                 'http://<host>:6789', '确认连的是自己的 Deskport'):
        assert line in err, line
    assert 'hi' not in out


def test_insecure_true_reaches_the_server(tls, tmp_path):
    """b) 加上 "insecure": true → 同一命令成功（echo hi）。"""
    url, _cert = tls
    code, out, err = _runCli(tmp_path / 'insecure', {'url': url, 'node': 'local', 'insecure': True})
    assert code == 0, err
    assert out == 'hi\n'


def test_cafile_pointing_at_the_real_cert_works(tls, tmp_path):
    """c) cafile 指向真实证书 → 成功，且不必放宽校验。"""
    url, cert = tls
    code, out, err = _runCli(tmp_path / 'cafile', {'url': url, 'node': 'local', 'cafile': cert})
    assert code == 0, err
    assert out == 'hi\n'


def test_missing_token_names_the_env_var(tmp_path):
    """d) 缺令牌：文案点明同目录 .agent-token 与环境变量 DESKPORT_TOKEN。"""
    d = tmp_path / 'notoken'
    d.mkdir()
    shutil.copyfile(CLIENT, d / 'deskport.py')
    (d / '.deskport.json').write_text(json.dumps({'url': 'http://127.0.0.1:1', 'node': 'local'}),
                                      encoding='utf-8')
    p = subprocess.run([sys.executable, 'deskport.py', 'run', 'echo hi'], cwd=str(d),
                       env=_noProxyEnv(), capture_output=True, encoding='utf-8',
                       errors='replace', timeout=90)
    assert p.returncode == 2
    assert 'DESKPORT_TOKEN' in p.stderr and '.agent-token' in p.stderr


@pytest.mark.parametrize('cfg,key', [
    ({'url': '', 'node': 'local'}, 'url'),
    ({'url': 'http://127.0.0.1:1', 'node': ''}, 'node'),
])
def test_empty_config_key_is_named(tmp_path, cfg, key):
    """e) .deskport.json 的 url/node 为空：明确说是哪个键，别走一半才 400。"""
    d = tmp_path / ('empty-' + key)
    d.mkdir()
    shutil.copyfile(CLIENT, d / 'deskport.py')
    (d / '.agent-token').write_text(TOKEN, encoding='utf-8')
    (d / '.deskport.json').write_text(json.dumps(cfg), encoding='utf-8')
    p = subprocess.run([sys.executable, 'deskport.py', 'run', 'echo hi'], cwd=str(d),
                       env=_noProxyEnv(), capture_output=True, encoding='utf-8',
                       errors='replace', timeout=90)
    assert p.returncode == 2
    assert '"%s" 键' % key in p.stderr, p.stderr
    assert 'Traceback' not in p.stderr


def test_urlerror_wrapping_cert_error_becomes_tlserror(monkeypatch):
    """URLError 里包着 SSLCertVerificationError → TlsError，文案带三种处理。"""
    dpMod = _load()
    dp = dpMod.Deskport('https://127.0.0.1:1', token='x')

    def boom(*a, **kw):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(
            1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: '
               'self-signed certificate (_ssl.c:1082)'))

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    with pytest.raises(dpMod.TlsError) as ei:
        dp._request('GET', '/api/agent/hosts')
    assert '三种处理' in str(ei.value) and '"insecure": true' in str(ei.value)


def test_handshake_failure_is_distinguished_from_cert_failure(monkeypatch):
    """握手失败另说一句：点明「不是证书不受信任」，并让调用方先去核地址/隧道。"""
    dpMod = _load()
    dp = dpMod.Deskport('https://127.0.0.1:1', token='x')

    def boom(*a, **kw):
        raise urllib.error.URLError(ssl.SSLError(
            1, '[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1009)'))

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    with pytest.raises(dpMod.TlsError) as ei:
        dp._request('GET', '/api/agent/hosts')
    assert '对端证书校验失败' not in str(ei.value)
    assert 'TLS 握手失败' in str(ei.value) and '不是证书不受信任' in str(ei.value)


def test_plain_connection_error_is_not_relabelled(monkeypatch):
    """连不上（不是证书问题）的 URLError 原样抛出，不冒充 TLS 文案。"""
    dpMod = _load()
    dp = dpMod.Deskport('http://127.0.0.1:1', token='x')

    def boom(*a, **kw):
        raise urllib.error.URLError(ConnectionRefusedError(10061, 'connection refused'))

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    with pytest.raises(urllib.error.URLError):
        dp._request('GET', '/api/agent/hosts')


def test_insecure_default_is_still_false():
    """不自动放宽：不传 insecure 时上下文是「校验主机名 + 要求证书」。"""
    dpMod = _load()
    dp = dpMod.Deskport('https://127.0.0.1:1', token='x')
    assert dp._ctx.verify_mode == ssl.CERT_REQUIRED
    assert dp._ctx.check_hostname is True
