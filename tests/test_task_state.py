# -*- coding: utf-8 -*-
"""任务状态机：迁移合法性与派生标志。"""
from modules import task_state as ts


def test_legal_transitions():
    task = {'id': 't', 'status': 'queued', 'sessionId': 's'}
    ts.transition(task, ts.TaskState.RUNNING)
    ts.transition(task, ts.TaskState.WAITING)
    ts.transition(task, ts.TaskState.PAUSED, 'test')
    ts.transition(task, ts.TaskState.QUEUED, 'resume')
    ts.transition(task, ts.TaskState.RUNNING)
    ts.transition(task, ts.TaskState.FAILED)
    ts.transition(task, ts.TaskState.CLOSED)
    assert task['status'] == 'closed'


def test_illegal_transition_is_logged_but_applied(caplog):
    task = {'id': 't', 'status': 'closed'}
    ts.transition(task, ts.TaskState.RUNNING, 'test')
    assert task['status'] == 'running'
    assert any('非法任务状态迁移' in r.message for r in caplog.records)


def test_derived_flags():
    assert ts.canContinue({'status': 'waiting', 'sessionId': 's'}) is True
    assert ts.canContinue({'status': 'waiting'}) is False
    assert ts.canResume({'status': 'failed', 'sessionId': 's'}) is True
    assert ts.canResume({'status': 'failed'}) is False
    assert ts.canContinue({'status': 'closed', 'sessionId': 's'}) is False


def test_unknown_state_falls_back_to_queued():
    assert ts.stateOf({'status': 'nonsense'}) == ts.TaskState.QUEUED


def test_strenum_serializes_as_plain_string():
    import json
    assert ts.TaskState.QUEUED == 'queued'
    assert json.dumps({'status': ts.TaskState.WAITING}) == '{"status": "waiting"}'
