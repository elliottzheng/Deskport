# -*- coding: utf-8 -*-
"""R5-4（方向反转）：/api/tasks/nodes 默认不带探测字段（?modelStatus=1 才带、
只列已探测）、probe 响应瘦身 {node, queued, models}、自动探测默认关闭
（modelProbe=false → node 启动零 kimi -p；web nudge 不发起）。"""
import json
import os
import sys

import pytest

import config as configMod

TOKEN = 'tok-' + 'm' * 24          # 与 test_model_probe.py 的 H/TOKEN 同款
H = {'X-Agent-Token': TOKEN}
NODE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'agent_node')
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


def test_nodes_list_has_no_model_status_by_default(webEnv):
    """R5-4 反转：默认响应不带 modelStatus/usableModels（精简形态）。"""
    resp = webEnv.client.get('/api/tasks/nodes', headers=H)
    assert resp.status_code == 200
    node = next(n for n in resp.get_json()['nodes'] if n['name'] == 'ai-xmyz')
    assert 'modelStatus' not in node and 'usableModels' not in node
    assert node['models'] == ['bad/m1', 'good/m2', 'idle/m3']
    assert node['modelSelectable'] is True


def test_nodes_list_model_status_flag_returns_probed_only(webEnv):
    """/?modelStatus=1 才带，且只列已探测的（usable≠null）。"""
    resp = webEnv.client.get('/api/tasks/nodes?modelStatus=1', headers=H)
    node = next(n for n in resp.get_json()['nodes'] if n['name'] == 'ai-xmyz')
    assert node['modelStatus']['bad/m1']['usable'] is False
    assert node['modelStatus']['good/m2']['usable'] is True
    assert 'idle/m3' not in node['modelStatus']   # 没探测的不占字节
    assert node['usableModels'] == ['good/m2']


def test_create_task_rejects_known_unusable_model(webEnv):
    resp = webEnv.client.post('/api/tasks', headers=H, data=json.dumps({
        'prompt': 'x', 'node': 'ai-xmyz', 'cwd': '/tmp',
        'model': 'bad/m1'}), content_type='application/json')
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['usableModels'] == ['good/m2']
    assert body['probedModelStatus']['usable'] is False


def test_create_task_allows_unprobed_model(webEnv):
    """没探测过的（modelStatus 里没有条目）照常放行。"""
    resp = webEnv.client.post('/api/tasks', headers=H, data=json.dumps({
        'prompt': 'x', 'node': 'ai-xmyz', 'cwd': '/tmp',
        'model': 'idle/m3'}), content_type='application/json')
    assert resp.status_code == 202


def test_probe_route_slim_response(webEnv, monkeypatch):
    """R5-4 第 1 条：probe 响应只回 {node, queued, models}，不带 modelStatus。"""
    calls = []

    def fakeProbe(name, models=None):
        calls.append((name, models))
        return 200, {'queued': len(models or []),
                     'models': list(models or []),
                     'modelStatus': {'junk': '不该出现在响应里'}}

    monkeypatch.setattr(webEnv.availMod, 'probeNodeModels', fakeProbe)
    resp = webEnv.client.post('/api/tasks/nodes/ai-xmyz/probe', headers=H,
                              data=json.dumps({'models': ['a', 'b']}),
                              content_type='application/json')
    assert resp.status_code == 200
    assert resp.get_json() == {'node': 'ai-xmyz', 'queued': 2,
                               'models': ['a', 'b']}
    assert calls == [('ai-xmyz', ['a', 'b'])]
    # 缺省 models 透传 None（= 全量）
    resp = webEnv.client.post('/api/tasks/nodes/ai-xmyz/probe', headers=H,
                              data='{}', content_type='application/json')
    assert calls[-1][1] is None
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
    body = resp.get_json()
    assert body['node'] == 'ghost' and body['queued'] == 0


# ---------------------------------------------------------------- 自动探测关闭

def test_nudge_disabled_by_default(webEnv, monkeypatch):
    """modelProbe=false（缺省）：nudge 直接返回，绝不发起任何探测请求。"""
    calls = []
    monkeypatch.setattr(webEnv.availMod, 'autoProbeEnabled', lambda: False)

    def noRequest(*a, **k):
        calls.append(a)
        raise AssertionError('默认配置不允许发起探测请求')
    monkeypatch.setattr(webEnv.nodesMod, 'nodeRequest', noRequest)
    webEnv.availMod.nudgeDefaultProbes(
        {'ai-xmyz': {'models': ['m'], 'default': 'd/1',
                     'modelStatus': {}, 'usableModels': []}})
    assert calls == []


def test_nudge_enabled_sends_default_and_recent(webEnv, monkeypatch):
    """modelProbe=true：web 把 default + 近 7 天用过的推给节点（timeout 120）。"""
    got = []
    monkeypatch.setattr(webEnv.availMod, 'autoProbeEnabled', lambda: True)
    monkeypatch.setattr(webEnv.availMod, 'recentTaskModels',
                        lambda name, **k: ['used/7d'])
    monkeypatch.setattr(webEnv.nodesMod, 'nodeRequest',
                        lambda node, method, path, payload, timeout=5:
                        got.append((node['name'], path, payload)) or (200, {}))
    monkeypatch.setattr(webEnv.availMod, '_lastNudge', 0.0)
    webEnv.availMod.nudgeDefaultProbes(
        {'ai-xmyz': {'models': [], 'default': 'd/1',
                     'modelStatus': {}, 'usableModels': []}})
    assert got == [('ai-xmyz', '/api/probe-models',
                    {'models': ['d/1', 'used/7d'], 'timeout': 120})]


def test_node_startup_makes_zero_probe_calls(tmp_path, monkeypatch):
    """「默认配置下启动后不会发起任何 kimi -p 调用」的直接证据：
    node 进程起来后 1.5s 内，假 kimi 的调用日志必须是空的。"""
    import socket
    import subprocess
    import time
    import urllib.request
    log = tmp_path / 'kimi-calls.log'
    log.write_text('', encoding='utf-8')
    kimi = tmp_path / 'fake-kimi.cmd'
    kimi.write_text('@echo off\r\necho %* >> "%KIMI_PROBE_LOG%"\r\nexit /b 0\r\n',
                    encoding='utf-8')
    home = tmp_path / 'home'          # 有声明模型+default：旧代码会去探测，
    home.mkdir()                      # 新代码（R5-4）必须一个都不碰
    kc = home / '.kimi-code'
    kc.mkdir()
    (kc / 'config.toml').write_text(
        'default_model = "fake-plan/m1"\n\n[models]\n"fake-plan/m1" = {}\n',
        encoding='utf-8')
    env = dict(os.environ)
    env['USERPROFILE'] = str(home)
    env['KIMI_PROBE_LOG'] = str(log)
    tokenFile = tmp_path / 'node.token'
    tokenFile.write_text('t' * 20, encoding='utf-8')
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(
        [sys.executable, 'agent_node.py', '--port', str(port),
         '--token-file', str(tokenFile), '--kimi', str(kimi),
         '--data', str(tmp_path / 'data')],
        cwd=NODE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with opener.open('http://127.0.0.1:%d/health' % port,
                                 timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        time.sleep(1.5)               # 给（不该存在的）自动探测留时间
        assert log.read_text(encoding='utf-8') == ''
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
