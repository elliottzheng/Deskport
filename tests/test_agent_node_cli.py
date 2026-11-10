# -*- coding: utf-8 -*-
"""agent_node CLI 令牌链（R4-6）：token-file > env > --token 的优先级与警告。

起真进程打 /health 与带令牌的受保护端点，保证优先级与认证行为端到端成立。
"""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

NODE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'agent_node')


def _freePort():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _launch(args, dataDir, port=None, extraEnv=None):
    env = dict(os.environ)
    env.pop('AGENT_NODE_TOKEN', None)
    env.update(extraEnv or {})
    return subprocess.Popen(
        [sys.executable, 'agent_node.py', '--port', str(port or _freePort()),
         '--kimi', 'x', '--data', str(dataDir)] + args,
        cwd=NODE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连，绕系统代理


def _http(port, path, token=None):
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path))
    if token:
        req.add_header('X-Agent-Token', token)
    try:
        with _opener.open(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _waitReady(proc, port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('node 提前退出: %s' % proc.stderr.read()[:300])
        try:
            if _http(port, '/health') == 200:
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError('node 启动超时')


def test_token_file_works(tmp_path):
    tokenFile = tmp_path / 'node.token'
    tokenFile.write_text('tok-file-123\n', encoding='utf-8')   # 带换行：须 strip
    port = _freePort()
    proc = _launch(['--token-file', str(tokenFile)], tmp_path / 'data', port=port)
    try:
        _waitReady(proc, port)
        # /health 免认证；受保护端点：无令牌 401、对令牌 200
        assert _http(port, '/health') == 200
        assert _http(port, '/api/sessions') == 401
        assert _http(port, '/api/sessions', token='tok-file-123') == 200
    finally:
        _stop(proc)


def test_env_var_is_used_when_no_token_file(tmp_path):
    port = _freePort()
    proc = _launch([], tmp_path / 'data', port=port,
                   extraEnv={'AGENT_NODE_TOKEN': 'tok-env-456'})
    try:
        _waitReady(proc, port)
        assert _http(port, '/api/sessions') == 401
        assert _http(port, '/api/sessions', token='tok-env-456') == 200
    finally:
        _stop(proc)


def test_plaintext_token_still_works_but_warns(tmp_path):
    port = _freePort()
    proc = _launch(['--token', 'tok-legacy-789'], tmp_path / 'data', port=port)
    try:
        _waitReady(proc, port)
        assert _http(port, '/api/sessions', token='tok-legacy-789') == 200
    finally:
        _stop(proc)
        err = proc.stderr.read().decode('utf-8', 'replace')
        assert '--token 已废弃' in err
