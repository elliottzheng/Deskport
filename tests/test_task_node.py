# -*- coding: utf-8 -*-
"""执行点语义：node 缺省 local 与显式 local 同一条路径；旧记录读时兜底。

不启动真实 runner：_startTask 打桩，只比对建卡产出的任务记录。
"""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
import modules.agent_nodes as nodesMod
from modules import events as eventsMod
from modules import task_node
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'e' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def _stored(env, tid):
    with env.repo.lock:
        return env.repo.get(tid)


def _stable(task):
    """去掉 id / 时间 / 由它们派生的路径后用于比对。"""
    out = {k: v for k, v in task.items()
           if k not in ('id', 'workspace', 'cwd', 'createdAt',
                        'startedAt', 'endedAt', 'lastActiveAt', 'events')}
    return out


def test_default_node_and_explicit_local_are_identical(env):
    """{prompt} 与 {prompt, node:'local'} 落同一条执行路径、同一份任务记录。"""
    a = create(env, {'prompt': '同一个活'})
    b = create(env, {'prompt': '同一个活', 'node': 'local'})
    assert a.status_code == b.status_code == 202, (a.get_json(), b.get_json())
    ta = _stored(env, a.get_json()['task']['id'])
    tb = _stored(env, b.get_json()['task']['id'])
    assert ta['node'] == tb['node'] == 'local'
    assert _stable(ta) == _stable(tb)
    # 缺省 cwd = 任务目录（workspace），两者一致地落在 workspace
    assert ta['cwd'] == ta['workspace'] and tb['cwd'] == tb['workspace']
    assert 'engine' not in ta and 'engine' not in tb
    # 分派键一致（runner 只按 node 类型分派）
    assert nodesMod.executionKind(ta.get('node')) == 'local'
    assert nodesMod.executionKind(tb.get('node')) == 'local'


def test_engine_param_rejected_even_with_valid_node(env):
    resp = create(env, {'prompt': 'x', 'node': 'local', 'engine': 'acp'})
    assert resp.status_code == 400
    error = resp.get_json()['error']
    assert 'node' in error and 'cwd' in error


def test_legacy_task_record_without_node_reads_local():
    legacy = {'id': 'old', 'workspace': r'E:\ws', 'cwd': ''}
    assert task_node.of(legacy) == 'local'
    assert task_node.isLocal(task_node.of(legacy))
    assert task_node.cwdOf(legacy) == r'E:\ws'
    legacyRemote = {'id': 'r', 'node': 'ai-xmyz', 'cwd': '',
                    'remoteCwd': '/mnt/proj', 'workspace': r'E:\ws'}
    assert task_node.of(legacyRemote) == 'ai-xmyz'
    assert task_node.cwdOf(legacyRemote) == '/mnt/proj'


def test_workline_legacy_engine_maps_once():
    assert task_node.worklineNode({'node': 'ai-xmyz'}) == ('ai-xmyz', False)
    assert task_node.worklineNode({'engine': 'acp'}) == ('local', False)
    assert task_node.worklineNode({'engine': 'remote', 'node': 'ai-xmyz'}) \
        == ('ai-xmyz', False)
    # 旧 remote 线缺 node：留空 + 要求补，不能悄悄落本机
    assert task_node.worklineNode({'engine': 'remote'}) == ('', True)
    assert task_node.worklineNode({}) == ('local', False)