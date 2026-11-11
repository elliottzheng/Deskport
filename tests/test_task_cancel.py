# -*- coding: utf-8 -*-
"""R5-4 随件缺陷修复：cancel 只结束当前轮——node/联邦引擎的任务取消后
保持可续（不再终态成 cancelled 导致 message 一律 409）。"""
import hashlib
import json
import queue

import pytest

import config as configMod
from modules import events as eventsMod
from modules import task_state
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'c' * 24
H = {'X-Agent-Token': TOKEN}


class DummyClient:
    """非 KimiAcp 的活客户端替身（node/联邦引擎路径）。"""

    def __init__(self):
        self.calls = []

    def cancel(self):
        self.calls.append('cancel')


@pytest.fixture
def env(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(
                                      TOKEN.encode()).hexdigest()}])
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo)


def _createTask(env):
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 202
    return resp.get_json()['task']['id']


def test_cancel_keeps_task_continuable(env):
    """node/联邦引擎：cancel 只停这一轮——状态不终态化，cancel 后 message 可续。"""
    taskId = _createTask(env)
    dummy = DummyClient()
    env.repo.setRunning(taskId, dummy)
    env.repo.setAgent(taskId, {'queue': queue.Queue(), 'client': dummy,
                               'perm': None})
    task = env.repo.get(taskId)
    with env.repo.lock:
        task_state.transition(task, task_state.TaskState.RUNNING)
        env.repo.save()

    resp = env.client.post('/api/tasks/%s/cancel' % taskId, headers=H)
    assert resp.status_code == 200
    task = env.repo.get(taskId)
    assert dummy.calls == ['cancel']
    assert task['status'] != 'cancelled'      # 不再终态化
    assert task.get('endedAt', 0) == 0        # 任务未结束
    last = [e for e in task['events'] if e['type'] == 'status'][-1]
    assert '已请求停止当前这轮' in last['text']

    # 轮结束后 runner 置回 waiting（模拟）→ 续聊不再 409
    with env.repo.lock:
        task_state.transition(task, task_state.TaskState.WAITING)
        task['sessionId'] = 'session_' + 'a' * 36
        env.repo.save()
    msg = env.client.post('/api/tasks/%s/message' % taskId, headers=H,
                          data=json.dumps({'text': '继续'}),
                          content_type='application/json')
    assert msg.status_code == 202, msg.get_json()
    assert env.repo.get(taskId)['status'] == 'running'


def test_cancel_without_live_client_still_terminal(env):
    """没有活客户端（web 重启后的残留 running）：保留旧的终态取消。"""
    taskId = _createTask(env)
    task = env.repo.get(taskId)
    with env.repo.lock:
        task_state.transition(task, task_state.TaskState.RUNNING)
        env.repo.save()
    resp = env.client.post('/api/tasks/%s/cancel' % taskId, headers=H)
    assert resp.status_code == 200
    assert env.repo.get(taskId)['status'] == 'cancelled'


def test_cancel_running_turn_of_waiting_task_is_noop(env):
    """waiting 任务调 cancel：本就不在跑，返回当前状态不动。"""
    taskId = _createTask(env)
    task = env.repo.get(taskId)
    with env.repo.lock:
        task_state.transition(task, task_state.TaskState.WAITING)
        env.repo.save()
    resp = env.client.post('/api/tasks/%s/cancel' % taskId, headers=H)
    assert resp.get_json() == {'ok': True, 'status': 'waiting'}
    assert env.repo.get(taskId)['status'] == 'waiting'
