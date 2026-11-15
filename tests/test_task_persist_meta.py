# -*- coding: utf-8 -*-
"""批次 PS1（任务持久化第 1 批，存储层）回归集：快照剥离与旧格式迁移。

覆盖：save 只落元数据（events 与 _ 开头运行时键不进 agent_tasks.json）、
253 任务/2 万事件的体积与 save p50、旧格式快照首次写盘前先留 .bak 原件、
备份失败按旧格式继续写、截断规则单一来源（写入端与回填同表）。
重启行为（/events 窗口、/wait、坏行、写失败计数、seq 校正）见
test_task_persist_restart.py。
"""
import json
import logging
import time
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


def _create(env, body=None):
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps(body or {'prompt': 'x'}),
                           content_type='application/json')
    return resp.get_json()['task']['id']


def _snapshotTasks(path):
    return json.loads(path.read_text(encoding='utf-8'))


# ---------------------------------------------------------------- 例 1：剥离 + 写盘量

def _synthTask(i, eventsPerTask):
    tid = 'syn%03d' % i
    events = []
    for j in range(eventsPerTask):
        seq = j + 1
        kind = j % 5
        if kind == 0:
            events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'assistant',
                           'text': '答' * 2000, 'truncated': False})
        elif kind == 1:
            events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'tool',
                           'name': 'Bash', 'args': '{"command": "%s"}' % ('x' * 400),
                           'callId': 'c%d' % j, 'status': 'completed',
                           'truncated': False})
        elif kind == 2:
            events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'tool_result',
                           'text': '出' * 800, 'callId': 'c%d' % j, 'name': 'Bash',
                           'truncated': False})
        elif kind == 3:
            events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'user',
                           'text': '问' * 100})
        else:
            events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'status',
                           'text': '这轮结束，等你继续'})
    return {'id': tid, 'title': '合成任务 %d' % i, 'prompt': 'p' * 200,
            'workspace': r'C:\syn\%d' % i, 'status': 'waiting',
            'createdAt': 1700000000, 'startedAt': 1700000000, 'endedAt': 0,
            'seq': eventsPerTask, 'events': events, 'lastText': '答' * 200,
            'resultFiles': [], 'error': '', 'sessionId': 'sess-%d' % i,
            'owner': '面板', 'lead': '', 'mode': 'default', 'model': '',
            'modelEffective': '', 'policy': {'default': 'ask'}, 'resume': True,
            'closedByUser': False, 'pendingPermission': '',
            'lastActiveAt': 1700000000, 'node': 'local',
            'cwd': r'C:\syn\%d' % i, 'sources': [],
            '_toolArgText': {'c%d' % k: 'x' * 400 for k in range(50)},
            '_lastToolCall': {'callId': 'c1', 'name': 'Bash', 'kind': 'execute',
                              'args': 'y' * 2000,
                              'rawArgs': {'command': 'z' * 500}},
            '_assistantBuf': 'b' * 3000}


def test_snapshot_strips_events_and_runtime_keys(tmp_path, caplog):
    """例 1 静态侧：save 只落元数据——events 与 _ 开头键不进 agent_tasks.json，
    内存里的窗口与运行时缓存不动。"""
    caplog.set_level(logging.WARNING, logger='tasks')
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'queued'}
    repo.put(task)
    repo.append_event(task, 'status', text='hello')
    task['_assistantBuf'] = '缓存只在内存'
    repo.save()
    data = _snapshotTasks(tmp_path / 'tasks.json')
    assert 'events' not in data['t1']
    assert not any(k.startswith('_') for k in data['t1'])
    assert data['t1']['seq'] == 1
    assert task['events'][0]['text'] == 'hello'      # 内存窗口原样
    assert task['_assistantBuf'] == '缓存只在内存'
    assert not [r for r in caplog.records if 'save tasks failed' in r.message]


def test_bulk_snapshot_size_and_save_p50(tmp_path, caplog):
    """例 1：253 任务 / 约 2 万事件，append_event 100 次——快照 <1MB、
    单次 save p50 <250ms、无 save tasks failed。"""
    caplog.set_level(logging.WARNING, logger='tasks')
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    perTask = 20000 // 253
    for i in range(253):
        repo.put(_synthTask(i, perTask))
    repo.save()
    store_path = tmp_path / 'tasks.json'
    assert store_path.stat().st_size < 1024 * 1024

    task = repo.get('syn000')
    for i in range(100):
        repo.append_event(task, 'status', text='轮次 %d' % i)
    durations = []
    for _ in range(100):
        t0 = time.perf_counter()
        repo.save()
        durations.append((time.perf_counter() - t0) * 1000)
    durations.sort()
    p50 = durations[len(durations) // 2]
    # 并行分组跑（tools/run_tests.py 缺省 6 路）时 CPU/磁盘被争抢：实测 p50
    # 92ms（串行约 40ms）。阈值放宽到 5 倍挡争抢抖动；真退化（丢原子写、
    # 快照变 O(n²)）是秒级，仍抓得到。
    assert p50 < 250, '单次 save p50 %.1fms' % p50
    assert store_path.stat().st_size < 1024 * 1024
    assert not [r for r in caplog.records if 'save tasks failed' in r.message]


# ---------------------------------------------------------------- 例 3：旧格式先备份再剥离

def _writeLegacyStore(path, logDir, taskCount=40, eventsPerTask=60):
    """合成旧格式快照（带 events 与 _ 键）+ 同内容 jsonl，返回原始字节。"""
    tasks = {}
    for i in range(taskCount):
        tid = 'old%03d' % i
        events = []
        for j in range(eventsPerTask):
            seq = j + 1
            if j % 4 == 0:
                events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'assistant',
                               'text': '旧答' * 1000, 'truncated': False})
            elif j % 4 == 1:
                events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'tool',
                               'name': 'Bash', 'args': '旧参数' * 100,
                               'callId': 'c%d' % j, 'status': 'completed',
                               'truncated': False})
            else:
                events.append({'seq': seq, 'at': 1700000000 + j, 'type': 'status',
                               'text': '旧状态 %d' % j})
        tasks[tid] = {'id': tid, 'title': '旧任务 %d' % i, 'prompt': '旧' * 100,
                      'workspace': r'C:\old\%d' % i, 'status': 'waiting',
                      'createdAt': 1700000000, 'startedAt': 1700000000, 'endedAt': 0,
                      'seq': eventsPerTask, 'events': events, 'lastText': '旧答旧答',
                      'resultFiles': [], 'error': '', 'sessionId': 'sess-%d' % i,
                      'owner': '面板', 'mode': 'default', 'policy': {},
                      'pendingPermission': '', 'lastActiveAt': 1700000000,
                      'node': 'local',
                      '_toolArgText': {'c%d' % k: 'x' * 300 for k in range(50)}}
        logDir.mkdir(parents=True, exist_ok=True)
        with open(logDir / (tid + '.jsonl'), 'w', encoding='utf-8') as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + '\n')
    raw = json.dumps(tasks, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    return raw


def test_legacy_store_backed_up_then_stripped(env, monkeypatch, caplog):
    """例 3：旧格式大文件交给新代码 load 再 save——先出现 .bak（字节一致），
    新文件 <1MB 且无 events 键，/event/<seq> 全文不变。"""
    caplog.set_level(logging.WARNING, logger='tasks')
    store_path = env.tmp / 'tasks.json'
    logDir = env.tmp / 'logs'
    raw = _writeLegacyStore(store_path, logDir)
    assert store_path.stat().st_size > 1024 * 1024
    # register 时 fixture 的 repo 已 load 过空目录——换一个全新的 repo 读旧文件
    fresh = TaskRepository(str(store_path), str(logDir))
    monkeypatch.setattr(store, 'repo', fresh)

    seq = 6                       # j=5 → j%4==1 → tool 事件，args 可核对全文
    before = env.client.get('/api/tasks/old000/event/%d' % seq, headers=H).get_json()
    assert before['event']['type'] == 'tool'
    assert before['event']['args'] == '旧参数' * 100

    baks = list(env.tmp.glob('tasks.json.pre-migration-*.bak'))
    assert len(baks) == 1, '旧格式首次 load 必须先备份'
    assert baks[0].read_bytes() == raw

    fresh.save()
    data = _snapshotTasks(store_path)
    assert store_path.stat().st_size < 1024 * 1024
    assert all('events' not in t for t in data.values())
    assert not any(k.startswith('_') for t in data.values() for k in t)
    assert data['old000']['seq'] == 60
    after = env.client.get('/api/tasks/old000/event/%d' % seq, headers=H).get_json()
    assert after == before
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_legacy_backup_failure_keeps_legacy_shape(env, monkeypatch, caplog):
    """例 3 保守侧：备份复制失败 → 本进程不剥离、按旧格式继续写（宁不瘦身
    不丢原件），且不留半截状态。"""
    caplog.set_level(logging.ERROR, logger='tasks')
    monkeypatch.setattr(task_events, 'backupLegacyStore', lambda path: False)
    store_path = env.tmp / 'tasks.json'
    logDir = env.tmp / 'logs'
    _writeLegacyStore(store_path, logDir, taskCount=3, eventsPerTask=5)
    fresh = TaskRepository(str(store_path), str(logDir))
    monkeypatch.setattr(store, 'repo', fresh)
    tid = _create(env)            # taskRoute → load → 备份失败 → 保持旧格式
    fresh.save()
    data = _snapshotTasks(store_path)
    assert 'events' in data['old000'] and data['old000']['events']
    assert data[tid]['events'], '新任务也按旧格式整包写'
    assert not list(env.tmp.glob('tasks.json.pre-migration-*.bak'))


# ---------------------------------------------------------------- 截断规则单一来源

def test_truncate_rules_table_matches_write_side():
    """截断规则核对表（写入端位置 → 规则）的函数级回归。"""
    assert task_events.MAX_TEXT == 8000 and task_events.MAX_TOOL_RESULT == 4000
    assert task_events.truncateEvent('assistant', 'a' * 8000) == ('a' * 8000, False)
    assert task_events.truncateEvent('assistant', 'a' * 8001) == ('a' * 8000, True)
    assert task_events.truncateEvent('tool', 'a' * 8001) == ('a' * 8000, True)
    assert task_events.truncateEvent('tool_result', 'a' * 4000) == ('a' * 4000, False)
    assert task_events.truncateEvent('tool_result', 'a' * 4001) == ('a' * 4000, True)
    assert task_events.truncateEvent('plan', 'a' * 9000) == ('a' * 8000, None)
    assert task_events.truncateEvent('user', 'a' * 90000) == ('a' * 90000, None)
    assert task_events.truncateEvent('status', 'a' * 90000) == ('a' * 90000, None)


def test_event_from_log_roundtrip_field_equal(tmp_path):
    """回填形状与写入端落卡逐字段相等：jsonl 全文行 → eventFromLog → 原事件。"""
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'waiting'}
    repo.put(task)
    repo.append_event(task, 'assistant', text='答' * 8000, full='答' * 9000,
                      truncated=True)
    repo.append_event(task, 'tool', name='Bash', args='a' * 8000, full='a' * 9500,
                      truncated=True, callId='c1')
    repo.append_event(task, 'tool_result', text='出' * 4000, full='出' * 5000,
                      truncated=True, callId='c1')
    repo.append_event(task, 'plan',
                      text='\n'.join('p%d' % i for i in range(9000))[:8000])
    repo.append_event(task, 'user', text='问' * 9000)
    repo.append_event(task, 'status', text='这轮结束')
    lines = (tmp_path / 'logs' / 't1.jsonl').read_text(encoding='utf-8').splitlines()
    hydrated = [task_events.eventFromLog(json.loads(line)) for line in lines]
    assert hydrated == list(task['events'])


def test_read_log_tail_caps_and_skips_chunks(tmp_path):
    """readLogTail：只取尾部 want 条非 chunk 事件；chunk/坏行不算数。"""
    path = tmp_path / 't.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        for i in range(2500):
            f.write(json.dumps({'seq': i + 1, 'at': 1, 'type': 'status',
                                'text': 'e%d' % i}, ensure_ascii=False) + '\n')
            f.write(json.dumps({'at': 1, 'type': 'assistant_chunk',
                                'text': 'chunk%d' % i}, ensure_ascii=False) + '\n')
        f.write('not json at all\n')
    tail = task_events.readLogTail(str(path), task_events.MAX_EVENTS)
    assert len(tail) == task_events.MAX_EVENTS
    assert tail[-1]['seq'] == 2500 and tail[0]['seq'] == 501
    assert all(e['type'] != 'assistant_chunk' for e in tail)
    assert len(task_events.readLogTail(str(path), 10)) == 10
    assert task_events.readLogTail(str(tmp_path / 'missing.jsonl'), 10) == []


def test_read_log_tail_with_patches(tmp_path):
    """readLogTailWithPatches：事件与 tool_args_patch 补丁行分流——事件同
    readLogTail，补丁行（不带 seq）按文件顺序收集；applyArgPatches 按
    targetSeq 应用（同一目标多次补丁以最后一次为准），补丁不进窗口。"""
    path = tmp_path / 't.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'seq': 1, 'at': 1, 'type': 'status',
                            'text': 'e1'}, ensure_ascii=False) + '\n')
        f.write(json.dumps({'seq': 2, 'at': 1, 'type': 'tool', 'name': 'Bash',
                            'args': '', 'text': '', 'truncated': False,
                            'callId': 'c1'}, ensure_ascii=False) + '\n')
        f.write(json.dumps({'type': 'tool_args_patch', 'targetSeq': 2,
                            'args': '{"command": "a"}', 'truncated': True,
                            'at': 2}, ensure_ascii=False) + '\n')
        f.write(json.dumps({'type': 'tool_args_patch', 'targetSeq': 2,
                            'args': '{"command": "b"}', 'truncated': False,
                            'at': 3}, ensure_ascii=False) + '\n')
        f.write(json.dumps({'seq': 3, 'at': 1, 'type': 'status',
                            'text': 'e3'}, ensure_ascii=False) + '\n')
    events, patches = task_events.readLogTailWithPatches(str(path), 10)
    assert [e['seq'] for e in events] == [1, 2, 3]
    assert [p['args'] for p in patches] == ['{"command": "a"}',
                                            '{"command": "b"}']
    assert [e['seq'] for e in task_events.readLogTail(str(path), 10)] == [1, 2, 3]
    hydrated = task_events.applyArgPatches(
        [task_events.eventFromLog(e) for e in events], patches)
    assert all(e['type'] != 'tool_args_patch' for e in hydrated)
    assert hydrated[1]['args'] == '{"command": "b"}'      # 最后一次为准
    assert hydrated[1]['truncated'] is False


def test_hydration_marker_stays_out_of_snapshot(tmp_path):
    """_eventsHydrated 回填标记只放内存：回填过的任务再 save 也不落盘。"""
    storePath = tmp_path / 'tasks.json'
    logDir = tmp_path / 'logs'
    repo = TaskRepository(str(storePath), str(logDir))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'waiting'}
    repo.put(task)
    repo.append_event(task, 'status', text='hello')
    repo2 = TaskRepository(str(storePath), str(logDir))
    repo2.load()
    task2 = repo2.get('t1')
    assert task2['_eventsHydrated'] is True
    assert task2['events'][0]['text'] == 'hello'
    repo2.save()
    data = _snapshotTasks(storePath)
    assert not any(k.startswith('_') for k in data['t1'])


def test_direct_tasks_subscript_hydrates(tmp_path):
    """存量直摸 repo.tasks[<id>] 的路径也拿到回填窗口（HydratingMap）。"""
    storePath = tmp_path / 'tasks.json'
    logDir = tmp_path / 'logs'
    repo = TaskRepository(str(storePath), str(logDir))
    task = {'id': 't1', 'seq': 0, 'events': [], 'status': 'waiting'}
    repo.put(task)
    repo.append_event(task, 'status', text='hello')
    repo2 = TaskRepository(str(storePath), str(logDir))
    repo2.load()
    assert repo2.tasks['t1']['events'][0]['text'] == 'hello'
    assert repo2.tasks.get('t1')['events'][0]['seq'] == 1
