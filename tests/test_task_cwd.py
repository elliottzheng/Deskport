# -*- coding: utf-8 -*-
"""acp + cwd：工作目录与任务目录分离（校验 / 传参 / 清理边界）。

不启动真实 ACP：建卡类用例把 _startTask 打桩；传参用例直接跑
_sessionThread 的客户端工厂与 node 请求体，断言 cwd 真的传下去了。
"""
import hashlib
import importlib.util
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
import modules.agent_nodes as nodesMod
from modules import events as eventsMod
from modules import tasks_api, tasks_runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'd' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    fallback = tmp_path / 'fallback'
    proj = work / 'proj'
    proj.mkdir(parents=True)
    fallback.mkdir()
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(configMod, 'getWorkDirs', lambda: [str(work)])
    monkeypatch.setattr(configMod, 'getFallbackDir', lambda: str(fallback))
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    # 健康模型缓存给"新鲜空值"：不触发后台探测线程（真实网络）
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path,
                           work=work, fallback=fallback, proj=proj)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def test_create_acp_with_cwd_records_dir_and_keeps_workspace(env):
    marker = env.proj / 'keep.txt'
    marker.write_text('原文件', encoding='utf-8')
    resp = create(env, {'prompt': '改项目', 'engine': 'acp',
                        'cwd': str(env.proj)})
    assert resp.status_code == 202, resp.get_json()
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        assert task['cwd'] == os.path.realpath(str(env.proj))
        assert task['workspace'] != task['cwd']
        assert os.path.isdir(task['workspace'])
        assert os.path.abspath(task['workspace']).startswith(
            os.path.abspath(str(env.tmp / 'root')))
        assert os.path.isfile(str(marker))
    out = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()['task']
    assert out['cwd'] == os.path.realpath(str(env.proj))
    # workspace 仍是任务目录（不是用户项目目录）
    assert out['workspace'] == task['workspace']


def test_acp_without_cwd_runs_in_workspace(env):
    resp = create(env, {'prompt': 'x', 'engine': 'acp'})
    assert resp.status_code == 202
    with env.repo.lock:
        task = env.repo.get(resp.get_json()['task']['id'])
        assert task['cwd'] == task['workspace']


def test_bad_or_escaping_cwd_is_400_and_creates_nothing(env):
    outside = env.tmp / 'outside'
    outside.mkdir()
    aFile = env.proj / 'file.txt'
    aFile.write_text('x', encoding='utf-8')
    cases = [
        ('相对路径', 'proj'),
        ('不存在', str(env.proj / 'missing')),
        ('不是目录', str(aFile)),
        ('越界', str(outside)),
        ('.. 越界', os.path.join(str(env.proj), '..', '..', 'outside')),
    ]
    for label, cwd in cases:
        resp = create(env, {'prompt': 'x', 'engine': 'acp', 'cwd': cwd})
        assert resp.status_code == 400, (label, resp.get_json())
        assert 'cwd' in resp.get_json()['error'], label
    with env.repo.lock:
        assert env.repo.list() == []
    assert os.path.isdir(str(env.proj))   # 用户目录毫发无损


def test_close_with_cwd_leaves_both_dirs_intact(env):
    marker = env.proj / 'keep.txt'
    marker.write_text('原文件', encoding='utf-8')
    tid = create(env, {'prompt': 'x', 'engine': 'acp',
                       'cwd': str(env.proj)}).get_json()['task']['id']
    with env.repo.lock:
        workspace = env.repo.get(tid)['workspace']
    (open(os.path.join(workspace, 'result.md'), 'w', encoding='utf-8')).close()
    body = env.client.post('/api/tasks/%s/close' % tid, headers=H).get_json()
    assert body['status'] == 'closed'
    # close 只回收会话；workspace 与 cwd 都不该被删（尤其不是反过来删项目）
    assert os.path.isdir(workspace)
    assert marker.read_text(encoding='utf-8') == '原文件'
    assert os.path.isdir(str(env.proj))


def test_create_failure_cleanup_only_removes_workspace(env):
    marker = env.proj / 'keep.txt'
    marker.write_text('原文件', encoding='utf-8')
    resp = create(env, {'prompt': 'x', 'engine': 'acp', 'cwd': str(env.proj),
                        'paths': [str(env.tmp / 'nope.txt')]})
    assert resp.status_code == 400
    assert os.listdir(str(env.tmp / 'root')) == []
    assert marker.read_text(encoding='utf-8') == '原文件'


def test_session_thread_passes_cwd_to_client(env):
    """真跑 _sessionThread：客户端工厂拿到的是 cwd，不是 workspace。"""
    captured = {}

    class FakeClient:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0):
            captured['cwd'] = cwd
            self.stderr_lines = []
            self.replayCount = 0

        def start(self, session_id=None):
            return 'session_' + 'a' * 32

        def prompt(self, text):
            captured['prompt'] = text
            return {'stopReason': 'end'}

        def close(self):
            pass

        def shutdown(self):
            pass

    workspace = str(env.tmp / 'ws')
    os.makedirs(workspace)
    task = {'id': 'cwd-thread', 'prompt': '做点事', 'workspace': workspace,
            'cwd': os.path.realpath(str(env.proj)), 'engine': 'acp',
            'status': 'queued', 'events': [], 'seq': 0, 'createdAt': 0,
            'closedByUser': False}
    with env.repo.lock:
        env.repo.put(task)
    t = threading.Thread(target=tasks_runner._sessionThread,
                         args=(task, []), kwargs={'clientFactory': FakeClient},
                         daemon=True)
    t.start()
    deadline = time.time() + 5
    while 'prompt' not in captured and time.time() < deadline:
        time.sleep(0.02)
    assert captured['cwd'] == os.path.realpath(str(env.proj))
    assert captured['cwd'] != task['workspace']
    # cwd 与 workspace 分离时，首条 prompt 必须指路
    assert str(env.proj) in captured['prompt']
    assert os.path.join(workspace, 'inputs') in captured['prompt']
    agent = env.repo.agentOf('cwd-thread')
    assert agent is not None
    agent['queue'].put({'type': 'close'})
    t.join(5)
    assert not t.is_alive()
    assert task['sessionId'] == 'session_' + 'a' * 32


def test_session_thread_falls_back_to_workspace_without_cwd(env):
    """没给 cwd（含旧任务记录里没有该字段）→ 仍在 workspace 里跑。"""
    captured = {}

    class FakeClient:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0):
            captured['cwd'] = cwd
            self.stderr_lines = []
            self.replayCount = 0

        def start(self, session_id=None):
            return 'session_' + 'b' * 32

        def prompt(self, text):
            return {'stopReason': 'end'}

        def close(self):
            pass

        def shutdown(self):
            pass

    workspace = str(env.tmp / 'ws-plain')
    os.makedirs(workspace)
    task = {'id': 'no-cwd-thread', 'prompt': '看看', 'workspace': workspace,
            'engine': 'acp', 'status': 'queued', 'events': [], 'seq': 0,
            'createdAt': 0, 'closedByUser': False}
    with env.repo.lock:
        env.repo.put(task)
    t = threading.Thread(target=tasks_runner._sessionThread,
                         args=(task, []), kwargs={'clientFactory': FakeClient},
                         daemon=True)
    t.start()
    deadline = time.time() + 5
    while 'cwd' not in captured and time.time() < deadline:
        time.sleep(0.02)
    assert captured['cwd'] == workspace
    agent = env.repo.agentOf('no-cwd-thread')
    deadline = time.time() + 5
    while agent is None and time.time() < deadline:
        time.sleep(0.02)
        agent = env.repo.agentOf('no-cwd-thread')
    agent['queue'].put({'type': 'close'})
    t.join(5)
    assert not t.is_alive()


def test_prompt_lists_absolute_inputs_when_cwd_differs():
    task = {'prompt': '改它', 'workspace': r'E:\ws', 'cwd': r'E:\proj'}
    manifest = [{'input': os.path.join('inputs', 'a.txt'), 'source': r'E:\src\a.txt'}]
    text = store._promptFor(task, manifest)
    assert os.path.join(r'E:\ws', 'inputs', 'a.txt') in text
    assert '你的工作目录是' in text
    # 不分离时保持旧 prompt（不添乱）
    same = {'prompt': '改它', 'workspace': r'E:\ws', 'cwd': r'E:\ws'}
    assert '你的工作目录是' not in store._promptFor(same, manifest)


def test_local_node_payload_carries_cwd(env, monkeypatch):
    """engine=acp + 本机 node：POST /api/sessions 的 cwd 就是任务 cwd。"""
    node = {'name': 'local', 'url': 'http://127.0.0.1:1', 'token': '',
            'via': '', 'remote': ''}
    monkeypatch.setattr(configMod, 'getLocalAgentNode', lambda: node)
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: node)
    sent = {}

    def fakeRequest(target, method, path, payload=None, timeout=30):
        sent.update({'path': path, 'payload': payload})
        return 500, {'error': 'stop'}

    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeRequest)
    task = {'engine': 'acp', 'node': '', 'cwd': os.path.realpath(str(env.proj)),
            'model': '', 'kimiSessionId': '', 'kimiSessionFork': True}
    client = nodesMod.clientFactoryFor(task)(cwd=task['cwd'])
    with pytest.raises(RuntimeError):
        client.start()
    assert sent['path'] == '/api/sessions'
    assert sent['payload']['cwd'] == task['cwd']


def test_remote_cwd_semantics_unchanged(env, monkeypatch):
    """remote 仍用 remoteCwd；传进来的 workspace 不覆盖它。"""
    node = {'name': 'ai-xmyz', 'url': 'http://127.0.0.1:1', 'token': '',
            'via': '', 'remote': ''}
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: node)
    sent = {}

    def fakeRequest(target, method, path, payload=None, timeout=30):
        sent.update({'payload': payload})
        return 500, {'error': 'stop'}

    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeRequest)
    task = {'engine': 'remote', 'node': 'ai-xmyz', 'remoteCwd': '/mnt/proj',
            'cwd': '', 'model': '', 'kimiSessionId': '', 'kimiSessionFork': True}
    client = nodesMod.clientFactoryFor(task)(cwd=r'E:\ws')
    assert client.cwd == '/mnt/proj'
    with pytest.raises(RuntimeError):
        client.start()
    assert sent['payload']['cwd'] == '/mnt/proj'


def _load_client():
    path = os.path.join(ROOT, '.agents', 'skills', 'deskport', 'deskport.py')
    spec = importlib.util.spec_from_file_location('deskport_skill_client', path)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod   # dataclasses 解析注解时要能查到模块
    spec.loader.exec_module(mod)
    return mod


def test_client_sends_acp_cwd_and_remote_still_requires_node():
    dpMod = _load_client()
    dp = dpMod.Deskport('http://127.0.0.1:1', token='x')
    bodies = []

    def fakeRequest(method, path, body=None, timeout=None):
        bodies.append(body)
        return {'task': {'id': 't1'}}

    dp._request = fakeRequest
    dp.create_task('x', engine='acp', cwd=r'E:\proj')
    assert bodies[0]['cwd'] == r'E:\proj'
    with pytest.raises(ValueError):
        dp.create_task('x', engine='remote', cwd=r'E:\proj')