# -*- coding: utf-8 -*-
"""批次 PS1（任务持久化第 1 批，存储层）回归集：重启行为。

覆盖验收例 2/4/5/6/7：同路径新 repo 模拟重启后 /events 窗口与重启前逐字段
相等（条数 ≤ MAX_EVENTS）、/wait 的 pending 重建吃回填窗口、jsonl 坏行跳过、
jsonl 写失败重试与 /api/diagnostics.taskLogWriteFailures 计数、快照 seq 落后
jsonl 时的 seq 校正。存储层静态侧（剥离/备份/截断表）见 test_task_persist_meta.py。
"""
import builtins
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules import task_events
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
TOKEN = 'tok-' + 'p' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD,
                                   'sessiondUrl': 'http://127.0.0.1:63991',
                                   'sshSessiondUrl': 'http://127.0.0.1:63992',
                                   'localAgentNodePort': 1}), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    import time as _time
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302
    return SimpleNamespace(client=client, repo=repo, tmp=tmp_path,
                           monkeypatch=monkeypatch)


def _restart(env):
    """同路径新 repo 模拟重启：路由此后走新仓库（首次 get 触发惰性回填）。"""
    repo2 = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
    env.monkeypatch.setattr(store, 'repo', repo2)
    return repo2


def _create(env, body=None):
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps(body or {'prompt': 'x'}),
                           content_type='application/json')
    return resp.get_json()['task']['id']


# ---------------------------------------------------------------- 例 2：重启后窗口逐字段相等

def test_restart_events_window_field_equal(env):
    """例 2：同路径新 repo 重启后 GET /events?after=0 与重启前窗口逐条相等
    （含 assistant 8000 / tool args 8000 / tool_result 4000 / plan 8000 截断
    形状与 user 不截），条数 ≤ MAX_EVENTS。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        store.repo.append_event(task, 'user', text='问' * 10000)      # 不截
        store.repo.append_event(task, 'assistant', text='答' * 8000,
                                full='答' * 9000, truncated=True)
        store.repo.append_event(task, 'tool', name='Bash', args='a' * 8000,
                                full='a' * 9000, truncated=True, callId='c1')
        store.repo.append_event(task, 'tool_result', text='出' * 4000,
                                full='出' * 5000, truncated=True, callId='c1')
        store.repo.append_event(task, 'plan',
                                text='\n'.join('计划 %d' % i for i in range(9000))[:8000])
        store.repo.append_event(task, 'status', text='这轮结束')
        before = [dict(e) for e in task['events']]
    _restart(env)
    body = env.client.get('/api/tasks/%s/events?after=0' % tid, headers=H).get_json()
    after = body['events']
    assert len(after) == len(before) <= store.MAX_EVENTS
    assert after == before, '重启后窗口与重启前不逐字段相等'
    assert [e['seq'] for e in after] == [e['seq'] for e in before]


def test_restart_window_caps_at_max_events(env):
    """例 2 条数侧：超过 MAX_EVENTS 后窗口只留最近 MAX_EVENTS 条，重启后一致。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        for i in range(store.MAX_EVENTS + 100):
            store.repo.append_event(task, 'status', text='e%d' % i)
        assert len(task['events']) == store.MAX_EVENTS
        before = [dict(e) for e in task['events']]
    _restart(env)
    body = env.client.get('/api/tasks/%s/events?after=0' % tid, headers=H).get_json()
    assert len(body['events']) == store.MAX_EVENTS
    assert body['events'] == before


# ---------------------------------------------------------------- 例 4：/wait 的 pending 重建

def test_wait_pending_rebuilt_from_hydrated_window(env):
    """例 4：waiting + pendingPermission + liveFrom，重启（同路径新 repo）后
    GET /wait 仍回报 pending——回填窗口里有那条 permission_request。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'sess-w'
        task['status'] = 'waiting'
        task['liveFrom'] = task['seq']          # 水位：之后的审批才是 live 的
        store.repo.append_event(task, 'permission_request', requestId='req-7',
                                text='等待你授权：Bash', name='Bash', callId='c7',
                                args='{"command": "ls"}',
                                options=[{'optionId': 'o1', 'name': '允许',
                                          'kind': 'allow_once'}])
        task['pendingPermission'] = 'req-7'
        env.repo.save()
    repo2 = _restart(env)
    repo2.load()               # 重启：清死标记 + 状态机；窗口靠回填
    with repo2.lock:
        task2 = repo2.get(tid)
        assert any(e['type'] == 'permission_request' and e['requestId'] == 'req-7'
                   for e in task2['events'])
        task2['pendingPermission'] = 'req-7'   # 活标记：会话线程在跑，load 清不到
    body = env.client.get('/api/tasks/%s/wait?after=0&timeout=1' % tid,
                          headers=H).get_json()
    assert body['status'] == 'permission'
    assert body['pending']['requestId'] == 'req-7'
    assert body['pending']['callId'] == 'c7'
    assert body['pending']['name'] == 'Bash'


# ---------------------------------------------------------------- 例 5：jsonl 坏行

def test_hydration_skips_bad_lines(env):
    """例 5：jsonl 里混坏行/半行——回填跳过它们，其余事件照常返回。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        for i in range(6):
            store.repo.append_event(task, 'status', text='好行 %d' % i)
    logPath = env.tmp / 'logs' / (tid + '.jsonl')
    with open(logPath, 'a', encoding='utf-8') as f:
        f.write('{"seq": 99, "at": 1, "type": "status", "text": "坏行' + '\n')
        f.write('{"seq": 100, "at": 1, "type": "status", "text": "截断半行')
    _restart(env)
    body = env.client.get('/api/tasks/%s/events?after=0' % tid, headers=H).get_json()
    texts = [e['text'] for e in body['events'] if e['type'] == 'status']
    assert texts == ['任务已创建'] + ['好行 %d' % i for i in range(6)]
    assert all('坏行' not in t and '截断半行' not in t for t in texts)


# ---------------------------------------------------------------- 例 6：jsonl 写失败不静默

def _flakyOpen(failTimes):
    """builtins.open 的替身：只拦 .jsonl 追加写，前 failTimes 次抛 OSError。"""
    real = builtins.open

    def fakeOpen(file, mode='r', *args, **kwargs):
        if 'a' in mode and str(file).endswith('.jsonl') and failTimes[0] > 0:
            failTimes[0] -= 1
            raise OSError('fake disk full')
        return real(file, mode, *args, **kwargs)
    return fakeOpen


def test_log_write_retries_then_succeeds_without_counting(env, monkeypatch):
    """例 6a：前两次写 jsonl 抛 OSError → 重试后落盘、计数为 0。"""
    tid = _create(env)
    before = task_events.logWriteFailures()
    fails = [2]
    real = builtins.open
    monkeypatch.setattr(builtins, 'open', _flakyOpen(fails))
    with env.repo.lock:
        task = env.repo.get(tid)
        store.repo.append_event(task, 'status', text='重试后落盘')
    monkeypatch.setattr(builtins, 'open', real)
    assert fails[0] == 0
    assert task_events.logWriteFailures() == before
    text = (env.tmp / 'logs' / (tid + '.jsonl')).read_text(encoding='utf-8')
    assert '重试后落盘' in text


def test_log_write_failure_counts_and_event_stays_in_memory(env, monkeypatch):
    """例 6b：一直写失败 → 计数 +1、事件留在内存窗口、/api/diagnostics 可见。"""
    tid = _create(env)
    before = task_events.logWriteFailures()
    fails = [999]
    real = builtins.open
    monkeypatch.setattr(builtins, 'open', _flakyOpen(fails))
    with env.repo.lock:
        task = env.repo.get(tid)
        store.repo.append_event(task, 'status', text='只进内存')
    monkeypatch.setattr(builtins, 'open', real)
    assert task_events.logWriteFailures() == before + 1
    assert any(e.get('text') == '只进内存' for e in task['events'])
    assert '只进内存' not in (env.tmp / 'logs' / (tid + '.jsonl')).read_text(
        encoding='utf-8')
    body = env.client.get('/api/diagnostics').get_json()
    assert body['taskLogWriteFailures'] == before + 1


# ---------------------------------------------------------------- 例 7：seq 校正

def test_seq_corrected_from_jsonl_after_crash(env):
    """例 7：快照 seq 落后 jsonl（jsonl 写了、快照没写的崩溃）→ 重启后
    task['seq'] = jsonl 最大 seq，新事件 seq 不重复。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        for i in range(9):
            store.repo.append_event(task, 'status', text='e%d' % i)
        task['seq'] = 3                    # 模拟崩溃：快照 seq 落后
        env.repo.save()
    repo2 = _restart(env)
    repo2.load()
    task2 = repo2.get(tid)
    assert task2['seq'] == 10              # = jsonl 最大 seq（建卡 1 + 9 条）
    ev = store.repo.append_event(task2, 'status', text='崩溃后第一条')
    assert ev['seq'] == 11
    seqs = [json.loads(line)['seq'] for line in
            (env.tmp / 'logs' / (tid + '.jsonl')).read_text(encoding='utf-8').splitlines()]
    assert seqs.count(11) == 1
    assert len(seqs) == len(set(seqs))
