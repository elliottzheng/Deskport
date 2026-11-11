# -*- coding: utf-8 -*-
"""R4-10 模型可用性探测：probeOnce 信号语义 / ModelProber 缓存与并发 /
node HTTP 端点 / web 侧呈现与建任务门禁。探测不起真 kimi（dev 实机验收的事），
用假 kimi（.cmd 脚本）锁死「退出码 = 可用性」的契约。"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import config as configMod
from agent_node.model_probe import MAX_WORKERS, ModelProber, nowMs, probeOnce

NODE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'agent_node')

TOKEN = 'tok-' + 'a' * 24
H = {'X-Agent-Token': TOKEN}


# ---------------------------------------------------------------- 假 kimi

def _makeFakeKimi(tmp_path, mode='exit', code=0, delaySec=0):
    """假 kimi：参数写进 KIMI_PROBE_LOG；mode='branch' 时含 bad → 退出 1。"""
    log = tmp_path / 'kimi-probe.log'
    log.write_text('', encoding='utf-8')
    body = ['@echo off',
            'echo %* >> "%KIMI_PROBE_LOG%"']
    if mode == 'branch':
        body += ['echo %* | findstr /C:"bad" >nul && exit /b 1']
    if delaySec:
        body += ['ping -n %d 127.0.0.1 >nul' % (delaySec + 1)]
    body += ['exit /b %d' % code]
    bin_ = tmp_path / ('fake-kimi-%s.cmd' % mode)
    bin_.write_text('\r\n'.join(body) + '\r\n', encoding='utf-8')
    os.environ['KIMI_PROBE_LOG'] = str(log)
    return str(bin_), str(log)


@pytest.fixture(autouse=True)
def _restoreProbeLogEnv():
    saved = os.environ.pop('KIMI_PROBE_LOG', None)
    yield
    os.environ.pop('KIMI_PROBE_LOG', None)
    if saved is not None:
        os.environ['KIMI_PROBE_LOG'] = saved


# ---------------------------------------------------------------- probeOnce

def test_probe_once_exit_zero_is_usable(tmp_path):
    kimi, log = _makeFakeKimi(tmp_path, mode='exit', code=0)
    usable, error = probeOnce(kimi, 'provider/model-a', str(tmp_path))
    assert usable is True and error == ''
    with open(log, encoding='utf-8') as f:
        assert '-p ok --model provider/model-a' in f.read()


def test_probe_once_nonzero_is_unusable_with_stderr(tmp_path):
    kimi, _log = _makeFakeKimi(tmp_path, mode='branch')
    usable, error = probeOnce(kimi, 'x/bad-alias', str(tmp_path))
    assert usable is False and error != ''


def test_probe_once_timeout_is_unusable(tmp_path):
    kimi, _log = _makeFakeKimi(tmp_path, mode='exit', code=0, delaySec=2)
    usable, error = probeOnce(kimi, 'x/hangs', str(tmp_path), timeout=1)
    assert usable is False and '超时' in error


def test_probe_once_bad_bin_is_unusable(tmp_path):
    assert probeOnce('', 'x/y', str(tmp_path))[0] is False
    usable, error = probeOnce(str(tmp_path / 'no-such.cmd'), 'x/y', str(tmp_path))
    assert usable is False and 'kimi 启动失败' in error


def _waitDrained(prober, timeout=10):
    deadline = time.time() + timeout
    while prober.pending() and time.time() < deadline:
        time.sleep(0.02)


def _stubProber(tmp_path, fn):
    return ModelProber('kimi-unused', str(tmp_path), probe_fn=fn)


def test_prober_flow_usable_unusable_unprobed(tmp_path):
    def fn(kimi, model, cwd):
        return (False, '后端挂了') if 'bad' in model else (True, '')
    prober = _stubProber(tmp_path, fn)
    assert prober.probe(['a/m1', 'b/bad', 'c/m2']) == 3
    _waitDrained(prober)
    status = prober.status(['a/m1', 'b/bad', 'c/m2', 'd/unprobed'])
    assert status['a/m1']['usable'] is True
    assert status['b/bad']['usable'] is False and '挂了' in status['b/bad']['error']
    assert status['c/m2']['usable'] is True
    # 没探测过的：usable=null、checkedAt=0
    assert status['d/unprobed'] == {'usable': None, 'checkedAt': 0, 'error': ''}
    assert prober.usableModels(['a/m1', 'b/bad', 'c/m2', 'd/unprobed']) \
        == ['a/m1', 'c/m2']
    assert prober.isUsable('a/m1') is True
    assert prober.isUsable('d/unprobed') is None


def test_prober_persists_and_reloads(tmp_path):
    prober = _stubProber(tmp_path, lambda k, m, c: (True, ''))
    prober.probe(['p/m1'])
    _waitDrained(prober)
    revived = _stubProber(tmp_path, lambda k, m, c: (False, '不该再测'))
    assert revived.isUsable('p/m1') is True
    assert revived.probe(['p/m1'], force=False) == 0   # 24h 内不重复花钱


def test_prober_ttl_expiry_reports_null(tmp_path):
    stale = {'s/old': {'usable': False, 'checkedAt': nowMs() - 25 * 3600 * 1000,
                       'error': '当时挂了'}}
    (tmp_path / 'model_status.json').write_text(
        json.dumps(stale), encoding='utf-8')
    prober = _stubProber(tmp_path, lambda k, m, c: (True, ''))
    item = prober.status(['s/old'])['s/old']
    assert item['usable'] is None            # 过期 = 当没探测过（放行）
    assert item['checkedAt'] > 0             # 上次探测时间保留可追溯
    assert prober.probe(['s/old'], force=False) == 1   # 过期后允许自动补测


def test_prober_dedups_inflight_and_force_reruns(tmp_path):
    seen = []
    lock = threading.Lock()

    def fn(kimi, model, cwd):
        with lock:
            seen.append(model)
        time.sleep(0.15)
        return True, ''

    prober = _stubProber(tmp_path, fn)
    assert prober.probe(['d/x']) == 1
    assert prober.probe(['d/x']) == 0        # 排队/在测中的不重复入队
    _waitDrained(prober)
    assert seen == ['d/x']
    assert prober.probe(['d/x'], force=True) == 1    # 手动触发强制复测
    _waitDrained(prober)
    assert seen == ['d/x', 'd/x']


def test_prober_never_exceeds_two_concurrent(tmp_path):
    state = {'cur': 0, 'peak': 0}
    lock = threading.Lock()

    def fn(kimi, model, cwd):
        with lock:
            state['cur'] += 1
            state['peak'] = max(state['peak'], state['cur'])
        time.sleep(0.1)
        with lock:
            state['cur'] -= 1
        return True, ''

    prober = _stubProber(tmp_path, fn)
    assert MAX_WORKERS == 2
    prober.probe(['c/%d' % i for i in range(6)])
    _waitDrained(prober)
    assert state['peak'] <= 2 and state['peak'] >= 1


def test_prober_recent_models_from_sessions(tmp_path):
    prober = _stubProber(tmp_path, lambda k, m, c: (True, ''))
    now = nowMs()
    sessions = [{'model': 'a/old', 'lastActive': now - 8 * 86400 * 1000},
                {'model': 'b/new', 'lastActive': now - 1000},
                {'model': 'b/new', 'lastActive': now - 2000},
                {'model': 'c/new', 'lastActive': now - 3 * 86400 * 1000},
                {'model': '', 'lastActive': now}]
    assert prober.recentModels(sessions, days=7) == ['b/new', 'c/new']


def test_worker_survives_probe_fn_crash(tmp_path):
    def fn(kimi, model, cwd):
        raise RuntimeError('炸了')
    prober = _stubProber(tmp_path, fn)
    prober.probe(['x/1', 'x/2'])
    _waitDrained(prober)
    for m in ('x/1', 'x/2'):
        assert prober.isUsable(m) is False
        assert '炸了' in prober.status([m])[m]['error']


def _freePort():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(port, path, payload=None, token=TOKEN):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') \
        if payload is not None else None
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                 data=data, method='POST' if data else 'GET')
    req.add_header('X-Agent-Token', token)
    req.add_header('Content-Type', 'application/json')
    try:
        with _opener.open(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8', 'replace') or '{}')


def _waitForHealth(port, cond, timeout=10):
    """轮询 /health 直到 cond 成立；返回最后一份 health。"""
    deadline, health = time.time() + timeout, {}
    while time.time() < deadline:
        try:
            health = _http(port, '/health')[1]
            if cond(health):
                break
        except Exception:
            pass
        time.sleep(0.2)
    return health


def test_node_health_and_probe_endpoint(tmp_path):
    tokenFile = tmp_path / 'node.token'
    tokenFile.write_text(TOKEN, encoding='utf-8')
    kimi, _log = _makeFakeKimi(tmp_path, mode='branch')
    # USERPROFILE 指到空目录：声明清单为空，启动默认扫描无事可做，
    # 测试不碰真实 ~/.kimi-code/config.toml
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ)
    env['USERPROFILE'] = str(home)
    data = tmp_path / 'data'
    port = _freePort()
    proc = subprocess.Popen(
        [sys.executable, 'agent_node.py', '--port', str(port),
         '--token-file', str(tokenFile), '--kimi', kimi, '--data', str(data)],
        cwd=NODE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        _waitForHealth(port, lambda h: h.get('ok') is True)
        # 未探测：usableModels 空
        status, health = _http(port, '/health')
        assert status == 200 and health['modelStatus'] == {}
        assert health['usableModels'] == []
        # 未认证 401；坏参数 400
        assert _http(port, '/api/probe-models', {'models': ['x']}, token='')[0] == 401
        assert _http(port, '/api/probe-models', {'models': 'x'})[0] == 400
        # 手动触发：bad 别名退出 1 → false；其余 true
        status, probed = _http(port, '/api/probe-models',
                               {'models': ['m-ok', 'm-bad']})
        assert status == 200 and probed['queued'] == 2
        health = _waitForHealth(port, lambda h: (
            (h.get('modelStatus') or {}).get('m-ok', {}).get('usable')
            is not None and (h.get('modelStatus') or {}).get('m-bad', {})
            .get('usable') is not None))
        ms = health['modelStatus']
        assert ms['m-ok']['usable'] is True
        assert ms['m-bad']['usable'] is False and ms['m-bad']['error']
        assert health['usableModels'] == ['m-ok']
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------- web 侧

class _NullProber:
    """web 测试替身：stdio 探测托管（绝不碰真 kimi 与真实数据目录）。"""

    def status(self, declared=()):
        return {m: {'usable': None, 'checkedAt': 0, 'error': ''}
                for m in (declared or [])}

    def usableModels(self, declared=()):
        return []


@pytest.fixture
def webEnv(tmp_path, monkeypatch):
    import hashlib
    import time as _time
    from types import SimpleNamespace
    import modules.agent_nodes as nodesMod
    import modules.model_availability as availMod
    from modules import events as eventsMod
    from modules import tasks_api
    from modules import tasks_store as store
    from modules.tasks_store import TaskRepository

    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(
                                      TOKEN.encode()).hexdigest()}])
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    nodes = [{'name': 'ai-xmyz', 'url': 'http://127.0.0.1:1', 'token': '',
              'via': 'x', 'remote': '',
              'models': ['bad/m1', 'good/m2', 'idle/m3'], 'model': 'good/m2'}]
    monkeypatch.setattr(configMod, 'getAgentNodes', lambda: nodes)
    monkeypatch.setattr(configMod, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(configMod, 'getRemotes', lambda: [])
    # stdio 探测托管换测试替身：/api/tasks/nodes 的 local 条目不触发真探测
    monkeypatch.setattr(availMod, '_stdioProber', lambda: _NullProber())
    # 新鲜缓存：不触发后台刷新线程，杜绝真实网络；MS=已探测结果桩
    MS = {'bad/m1': {'usable': False, 'checkedAt': 1, 'error': '挂'},
          'good/m2': {'usable': True, 'checkedAt': 1, 'error': ''}}
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _time.monotonic(),
                         'data': {'ai-xmyz': {
                             'models': ['bad/m1', 'good/m2', 'idle/m3'],
                             'default': 'good/m2', 'modelStatus': MS,
                             'usableModels': ['good/m2']}}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), app=app,
                           nodesMod=nodesMod, availMod=availMod, repo=repo)


def test_nodes_list_includes_model_status(webEnv):
    resp = webEnv.client.get('/api/tasks/nodes', headers=H)
    assert resp.status_code == 200
    node = next(n for n in resp.get_json()['nodes'] if n['name'] == 'ai-xmyz')
    assert node['modelStatus']['bad/m1']['usable'] is False
    assert node['modelStatus']['good/m2']['usable'] is True
    # 声明了但还没探测的：null 条目（null=还没探测）
    assert node['modelStatus']['idle/m3'] == {'usable': None, 'checkedAt': 0,
                                              'error': ''}
    assert node['usableModels'] == ['good/m2']


def test_create_task_rejects_known_unusable_model(webEnv):
    resp = webEnv.client.post('/api/tasks', headers=H, data=json.dumps({
        'prompt': 'x', 'engine': 'remote', 'node': 'ai-xmyz', 'cwd': '/tmp',
        'model': 'bad/m1'}), content_type='application/json')
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['usableModels'] == ['good/m2']
    assert body['probedModelStatus']['usable'] is False


def test_create_task_allows_unprobed_model(webEnv):
    resp = webEnv.client.post('/api/tasks', headers=H, data=json.dumps({
        'prompt': 'x', 'engine': 'remote', 'node': 'ai-xmyz', 'cwd': '/tmp',
        'model': 'idle/m3'}), content_type='application/json')
    assert resp.status_code == 202   # 没探测过的照常放行


def test_probe_route_forwards_and_validates(webEnv, monkeypatch):
    calls = []

    def fakeProbe(name, models=None):
        calls.append((name, models))
        return 200, {'queued': len(models or [])}

    monkeypatch.setattr(webEnv.availMod, 'probeNodeModels', fakeProbe)
    resp = webEnv.client.post('/api/tasks/nodes/ai-xmyz/probe', headers=H,
                              data=json.dumps({'models': ['a', 'b']}),
                              content_type='application/json')
    assert resp.status_code == 200
    assert resp.get_json()['node'] == 'ai-xmyz'
    assert calls == [('ai-xmyz', ['a', 'b'])]
    # 缺省 models 透传 None（= 全量）
    resp = webEnv.client.post('/api/tasks/nodes/ai-xmyz/probe', headers=H,
                              data='{}', content_type='application/json')
    assert resp.status_code == 200 and calls[-1][1] is None
    # 参数不合法 400
    resp = webEnv.client.post('/api/tasks/nodes/ai-xmyz/probe', headers=H,
                              data=json.dumps({'models': 'bad'}),
                              content_type='application/json')
    assert resp.status_code == 400


def test_probe_route_passes_through_upstream_404(webEnv, monkeypatch):
    monkeypatch.setattr(webEnv.availMod, 'probeNodeModels',
                        lambda n, m=None: (404, {'error': '节点不存在'}))
    resp = webEnv.client.post('/api/tasks/nodes/ghost/probe', headers=H,
                              data='{}', content_type='application/json')
    assert resp.status_code == 404
