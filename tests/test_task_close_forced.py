# -*- coding: utf-8 -*-
"""close 必须真的能停：回合卡在工具调用里时，cancel 不够，要硬停并如实标 forced。

2026-11-13 实测：中枢 8 个并发名额被 4 个"已 merge 但状态还 running"的任务占着，
`/close` 与 `/cancel` 都回 `{"ok": true, "status": "running"}`——嘴上说关了，实际没停，
名额一个都没释放。根因是 cancel 只请求停这一轮；代理若卡在工具调用里（例如跑全量
测试），会话不回结果，托管线程永远等不到终态。
"""
import hashlib
import json
import queue
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_store as store
from modules import tasks_api
from modules.tasks_store import TaskRepository

TOKEN = 'tok-close-forced-0123456789abcdef'
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(TOKEN.encode()).hexdigest()}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


class StuckClient:
    """cancel 与 close 都不改变任何状态：模拟"卡在工具调用里"的会话。"""

    def __init__(self):
        self.calls = []

    def cancel(self):
        self.calls.append('cancel')

    def close(self):
        self.calls.append('close')


def test_close_forces_when_turn_never_returns(env):
    client = StuckClient()
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 202, resp.get_data(as_text=True)
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'running'
    env.repo.setAgent(tid, {'client': client, 'queue': queue.Queue(),
                            'permDecision': None, 'permEvent': None})

    body = env.client.post('/api/tasks/%s/close' % tid, headers=H).get_json()

    assert client.calls == ['cancel', 'close'], client.calls   # 先温和请求，再硬停
    assert body['status'] == 'cancelled', body                 # 终态真的落地
    assert body.get('forced') is True, body                    # 如实告诉调用方
    assert env.repo.get(tid)['status'] == 'cancelled'