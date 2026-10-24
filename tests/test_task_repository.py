# -*- coding: utf-8 -*-
"""TaskRepository：持久化、事件流水与重启恢复。"""
from modules.tasks_store import TaskRepository


def test_roundtrip_and_log(tmp_path):
    store = tmp_path / 'tasks.json'
    logs = tmp_path / 'logs'
    repo = TaskRepository(str(store), str(logs))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'queued'}
    repo.tasks['t1'] = task
    repo.append_event(task, 'status', text='hello')
    repo.save()

    repo2 = TaskRepository(str(store), str(logs))
    repo2.load()
    assert repo2.tasks['t1']['events'][0]['text'] == 'hello'
    assert (logs / 't1.jsonl').read_text(encoding='utf-8').strip()


def test_load_marks_running_as_paused(tmp_path):
    store = tmp_path / 'tasks.json'
    store.write_text(
        '{"t1":{"id":"t1","status":"running","sessionId":"s1","events":[]}}',
        encoding='utf-8')
    repo = TaskRepository(str(store), str(tmp_path / 'logs'))
    repo.load()
    task = repo.tasks['t1']
    assert task['status'] == 'paused'
    assert task['resume'] is True


def test_load_marks_no_session_as_interrupted(tmp_path):
    store = tmp_path / 'tasks.json'
    store.write_text('{"t1":{"id":"t1","status":"running","events":[]}}',
                     encoding='utf-8')
    repo = TaskRepository(str(store), str(tmp_path / 'logs'))
    repo.load()
    assert repo.tasks['t1']['status'] == 'interrupted'
    assert repo.tasks['t1']['error']


def test_save_stable_while_writers_add_keys(tmp_path):
    """序列化必须在锁内快照：历史上无锁 json.dump 会撞 changed size。"""
    import threading

    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'queued'}
    repo.put(task)

    def mutate():
        for i in range(300):
            with repo.lock:
                task['k%d' % i] = i

    writer = threading.Thread(target=mutate)
    writer.start()
    for _ in range(300):
        repo.append_event(task, 'status', text='x')
    writer.join()
    import json
    data = json.loads((tmp_path / 'tasks.json').read_text(encoding='utf-8'))
    assert data['t1']['status'] == 'queued'
    assert data['t1']['k299'] == 299


def test_public_accessors(tmp_path):
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'queued'}
    repo.put(task)
    assert repo.get('t1') is task
    assert repo.list() == [task]
    assert repo.runningCount() == 0
    repo.setRunning('t1', object())
    assert repo.runningCount() == 1
    repo.dropRunning('t1')
    assert repo.runningCount() == 0


def test_summary_matches_typeddict_contract(tmp_path):
    from modules.contracts import TaskSummary
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': 't1', 'status': 'waiting', 'sessionId': 's1',
            'events': [], 'createdAt': 0}
    assert set(repo.summary(task)) == set(TaskSummary.__annotations__)
