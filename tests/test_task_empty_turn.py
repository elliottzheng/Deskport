# -*- coding: utf-8 -*-
"""空转轮次要能被看见：一个工具都不调、一句话都不回就结束轮次 → lastTurn.empty=True。

背景（2026-11-13 中招验收方早夭事件）：续跑消息投递成功、会话历史也 load 了，
模型却 0 工具 0 文本结束轮次。events 里只剩「开始执行 / 这轮结束」两条 status，
跟正常结束长得一模一样，主控只能反复续跑、反复空转。
"""
import hashlib
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_store as store
from modules import tasks_api
from modules import tasks_runner as runner
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'e' * 24
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
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


def _newRunning(env, title, **extra):
    task = {'id': 't-' + title, 'title': title, 'prompt': 'p', 'seq': 0,
            'events': [], 'status': 'queued',
            'workspace': str(env.tmp / 'root'), 'node': 'local'}
    task.update(extra)
    with env.repo.lock:
        env.repo.tasks[task['id']] = task
    env.repo.setAgent(task['id'], {'queue': queue.Queue()})
    return task


class SilentClient:
    """prompt 立刻返回、什么都不做：模拟早夭那一轮。"""

    def prompt(self, text, timeout=None):
        return {'stopReason': 'end_turn'}


class BusyClient(SilentClient):
    """同一次 prompt 里先报一个工具调用：对照组，不许被判成空转。"""

    def __init__(self, task):
        self.task = task

    def prompt(self, text, timeout=None):
        runner._handleAcpUpdate(self.task, {'toolName': 'Bash',
                                            'toolInput': 'ls',
                                            'sessionUpdate': 'tool_call'})
        return {'stopReason': 'end_turn'}


def test_empty_turn_is_flagged(env):
    task = _newRunning(env, 'empty')
    with env.repo.lock:
        task['status'] = 'running'
    runner._promptOnce(task, SilentClient(), '继续干完', False)
    assert task['lastTurn']['tools'] == 0
    assert task['lastTurn']['empty'] is True
    assert env.repo.summaryBrief(task)['emptyTurn'] is True


def test_busy_turn_is_not_flagged(env):
    task = _newRunning(env, 'busy')
    with env.repo.lock:
        task['status'] = 'running'
        task['lastText'] = '干完了'
    runner._promptOnce(task, BusyClient(task), '继续', False)
    assert task['lastTurn']['tools'] == 1, task.get('lastTurn')
    assert task['lastTurn']['empty'] is False
    assert env.repo.summaryBrief(task)['emptyTurn'] is False
