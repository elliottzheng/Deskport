# -*- coding: utf-8 -*-
"""批次 PS2（任务持久化第 2 批，削峰）回归集：1 秒 flush 合并落盘。

覆盖规格验收例 1–5（例 6 = 全量 run_tests.py，不在此文件）：
  1. 154 事件/秒 × 20 秒爆发：快照写盘 ≤25（每秒一拍 + 关键变迁）、
     append_event p50 <5ms、jsonl 行数 = 事件数（修前红：每事件一次 save，
     实测 3080 次写盘）
  2. kill -9 后重启：事件不丢（jsonl）、seq = jsonl 最大 seq、最后 ≤1 秒的
     lastActiveAt/lastText 可丢
  3. 轮中改 status / 挂审批 / 入队消息 / 关闭 / 派主管 / 改名：立即落盘
  4. 进程正常退出：atexit 的最后一次 flush 生效
  5. jsonl 句柄：任务 close 后已关闭（Windows 上文件可改名/删除）
另覆盖第 1 批复验的两条建议：补丁应用只留一份（_applyPatch）、jsonl 回填的
'tool_args_patch' 廉价预筛不误伤事件行。存储层静态侧见 test_task_persist_meta.py，
重启行为见 test_task_persist_restart.py。
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from modules import store_io
from modules import task_events
from modules import task_state
from modules.tasks_store import TaskRepository

RATE = 154            # 事件/秒（单任务 streaming 峰值，方案 §1.4）
SECONDS = 20
TOTAL = RATE * SECONDS

# Windows 没有 SIGKILL；os.kill(pid, SIGTERM) 走 TerminateProcess，同样不跑
# atexit、不等 flush——对本测试要的"硬杀"语义等价（返回码 15）
_HARD_KILL = int(getattr(signal, 'SIGKILL', signal.SIGTERM))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _countingWrites(monkeypatch, storePath):
    """把 saveSnapshot 包一层数写盘次数（只数放行了序号门的、且路径匹配的）。"""
    real = store_io.saveSnapshot
    state = {'n': 0}

    def counting(path, payload, seq, gate):
        before = gate.savedSeq
        result = real(path, payload, seq, gate)
        if result != before and os.path.abspath(path) == os.path.abspath(storePath):
            state['n'] += 1
        return result

    monkeypatch.setattr(store_io, 'saveSnapshot', counting)
    return state


def _burstEvent(i):
    """轮换合成五种事件（与真实 streaming 同型：tool/tool_result 带脱敏）。"""
    kind = i % 5
    if kind == 0:
        return ('assistant', {'text': '答' * 300, 'full': '答' * 3000,
                              'truncated': False})
    if kind == 1:
        return ('tool', {'name': 'Bash', 'args': '{"command": "echo %d"}' % i,
                         'full': '{"command": "echo %d"}' % i,
                         'truncated': False, 'callId': 'c%d' % i,
                         'status': 'completed'})
    if kind == 2:
        return ('tool_result', {'text': '出' * 200, 'full': '出' * 2000,
                                'truncated': False, 'callId': 'c%d' % i,
                                'name': 'Bash'})
    if kind == 3:
        return ('plan', {'text': '\n'.join('步骤 %d' % j for j in range(50))})
    return ('status', {'text': '轮次 %d' % i})


def _newRepo(tmp_path, taskId='t1'):
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': taskId, 'seq': 0, 'events': [], 'status': 'running'}
    repo.put(task)
    return repo, task


# ---------------------------------------------------------------- 例 1：削峰

def test_burst_merges_writes_within_one_second(tmp_path, monkeypatch):
    """例 1：154 事件/秒 × 20 秒爆发——写盘 ≤25（每秒一拍）、append_event
    p50 <5ms、jsonl 行数 = 事件数。修前红：每事件一次 save，写盘 3080 次。"""
    repo, task = _newRepo(tmp_path, 'burst-1')
    writes = _countingWrites(monkeypatch, str(tmp_path / 'tasks.json'))
    durations = []
    interval = 1.0 / RATE
    t0 = time.perf_counter()
    for i in range(TOTAL):
        etype, kw = _burstEvent(i)
        s = time.perf_counter()
        repo.append_event(task, etype, **kw)
        durations.append((time.perf_counter() - s) * 1000)
        nxt = t0 + (i + 1) * interval      # 按目标间隔 pacing，欠账不累积
        delay = nxt - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    durations.sort()
    p50 = durations[len(durations) // 2]
    repo.flush()                            # 收尾一拍（爆发已结束）
    logPath = tmp_path / 'logs' / 'burst-1.jsonl'
    lines = logPath.read_text(encoding='utf-8').splitlines()
    assert writes['n'] <= 25, '写盘 %d 次（规格 ≤25）' % writes['n']
    assert p50 < 5, 'append_event p50 %.2fms（规格 <5ms）' % p50
    assert len(lines) == TOTAL
    assert [json.loads(l)['seq'] for l in lines] == list(range(1, TOTAL + 1))


def test_touch_defers_and_flush_writes_once(tmp_path, monkeypatch):
    """例 1 机制侧：append_event 只置脏不写盘；flush 写一次；无新变更时再
    flush 不写。"""
    repo, task = _newRepo(tmp_path)
    writes = _countingWrites(monkeypatch, str(tmp_path / 'tasks.json'))
    for i in range(50):
        repo.append_event(task, 'status', text='e%d' % i)
    assert writes['n'] == 0, 'append_event 不该触发写盘'
    assert repo._dirty is True
    assert repo.flush() is True
    assert writes['n'] == 1
    data = json.loads((tmp_path / 'tasks.json').read_text(encoding='utf-8'))
    assert data['t1']['seq'] == 50            # 非关键变更已随 flush 落盘
    assert repo.flush() is False and writes['n'] == 1   # 没脏不再写


# ---------------------------------------------------------------- 例 2：kill -9

_KILL9_CHILD = """
import os, sys
sys.path.insert(0, {repo!r})
os.environ['APPDATA'] = {appdata!r}
from modules.tasks_store import TaskRepository
repo = TaskRepository({store!r}, {logdir!r})
task = {{'id': 'k9', 'seq': 0, 'events': [], 'status': 'running'}}
repo.put(task)
repo.save()                       # 建卡即落盘（与 create 路径同构）
for i in range({count}):
    repo.append_event(task, 'status', text='e%d' % i)
os.kill(os.getpid(), {sig})       # 硬杀：不等 flush、不跑 atexit
"""


def test_kill9_restart_keeps_events_and_seq(tmp_path, monkeypatch):
    """例 2：kill -9 后重启——事件不丢（jsonl 是真相）、seq = jsonl 最大 seq
    （flush 丢失窗口内的元数据不导致重号）、最后 ≤1 秒的 lastActiveAt/lastText
    允许丢（快照可以落后）。"""
    store = str(tmp_path / 'tasks.json')
    logdir = str(tmp_path / 'logs')
    appdata = str(tmp_path / 'appdata')
    os.makedirs(appdata, exist_ok=True)
    monkeypatch.setenv('APPDATA', appdata)   # 子进程 import modules 前指开
    code = _KILL9_CHILD.format(repo=REPO_ROOT, appdata=appdata, store=store,
                               logdir=logdir, count=40, sig=_HARD_KILL)
    proc = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0, '子进程应当被硬杀（无 atexit）'
    logPath = tmp_path / 'logs' / 'k9.jsonl'
    seqs = [json.loads(l)['seq'] for l in
            logPath.read_text(encoding='utf-8').splitlines()]
    assert seqs == list(range(1, 41)), 'kill -9 不该丢任何一条事件（jsonl）'

    repo2 = TaskRepository(store, logdir)     # 同路径新 repo = 重启
    repo2.load()
    task = repo2.get('k9')
    assert task['seq'] == 40                  # = jsonl 最大 seq（校正生效）
    assert len(task['events']) == 40
    ev = repo2.append_event(task, 'status', text='崩溃后第一条')
    assert ev['seq'] == 41                    # 不重号
    after = [json.loads(l)['seq'] for l in
             logPath.read_text(encoding='utf-8').splitlines()]
    assert after.count(41) == 1 and len(after) == len(set(after))
    # 快照允许落后 ≤1 秒（丢的只是 lastActiveAt/lastText 这类非关键元数据）
    snap = json.loads((tmp_path / 'tasks.json').read_text(encoding='utf-8'))
    assert snap['k9']['seq'] <= 40


# ---------------------------------------------------------------- 例 3：关键变迁立即落盘

def test_key_transitions_save_immediately(tmp_path, monkeypatch):
    """例 3：六个关键字段（status/pendingPermission/messageQueue/closedByUser/
    lead/title）变化即落盘，不等 flush；同期的非关键变更搭车落盘。"""
    repo, task = _newRepo(tmp_path)
    writes = _countingWrites(monkeypatch, str(tmp_path / 'tasks.json'))

    def snap():
        return json.loads((tmp_path / 'tasks.json').read_text(encoding='utf-8'))

    task_state.transition(task, task_state.TaskState.WAITING, 'test')
    repo.save()
    assert writes['n'] == 1 and snap()['t1']['status'] == 'waiting'

    task['pendingPermission'] = 'req-1'
    repo.save()
    assert writes['n'] == 2 and snap()['t1']['pendingPermission'] == 'req-1'

    task.setdefault('messageQueue', []).append({'id': 'm1', 'text': '排队'})
    repo.save()
    assert writes['n'] == 3 and snap()['t1']['messageQueue'][0]['id'] == 'm1'

    task['closedByUser'] = True
    repo.save()
    assert writes['n'] == 4 and snap()['t1']['closedByUser'] is True

    task['lead'] = 'lead-7'
    repo.save()
    assert writes['n'] == 5 and snap()['t1']['lead'] == 'lead-7'

    task['title'] = '改过的名字'
    repo.save()
    assert writes['n'] == 6 and snap()['t1']['title'] == '改过的名字'

    # 非关键变更不触发写盘，等 flush（例 1 机制侧已细验，这里确认不抢跑）
    repo.append_event(task, 'status', text='只进内存窗口')
    assert writes['n'] == 6


# ---------------------------------------------------------------- 例 4：正常退出

_EXIT_CHILD = """
import os, sys
sys.path.insert(0, {repo!r})
os.environ['APPDATA'] = {appdata!r}
import config
# 子进程与父进程一样不读仓库里的 config.json：父进程由 conftest 把 CONFIG_PATH
# 指到临时配置，子进程照做（指到一个不存在的路径 → config.load 回退 DEFAULTS，
# stateDirName = 缺省 web-platform）。否则 dev checkout 的 config.json 会把状态
# 目录改名（如 web-platform-dev），退出 flush 落到别的路径，本用例按缺省名断言。
config.CONFIG_PATH = os.path.join({appdata!r}, 'no-such-config.json')
from modules import tasks_store as store
store.repo.load()                  # 物化模块级仓库（服务就这一个）
task = {{'id': 'exit-1', 'seq': 0, 'events': [], 'status': 'running'}}
store.repo.put(task)
store.repo.save()                  # 建卡落盘
for i in range(7):
    store.repo.append_event(task, 'status', text='e%d' % i)
# 正常退出：不显式 flush——由 atexit 的 shutdown 做最后一次
"""


def test_exit_flush_persists_last_changes(tmp_path, monkeypatch):
    """例 4：进程正常退出——atexit 注册的 shutdown 把最后一批非关键变更
    （seq/lastActiveAt/lastText）落盘，jsonl 句柄也全部关闭。"""
    appdata = str(tmp_path / 'appdata')
    os.makedirs(appdata, exist_ok=True)
    monkeypatch.setenv('APPDATA', appdata)
    code = _EXIT_CHILD.format(repo=REPO_ROOT, appdata=appdata)
    proc = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    # 子进程走模块级仓库：状态目录 = %APPDATA%\web-platform（config 缺省名；
    # 子进程已把 CONFIG_PATH 指开，仓库里的 config.json 不改名——见 _EXIT_CHILD）
    storePath = os.path.join(appdata, 'web-platform', 'agent_tasks.json')
    data = json.loads(open(storePath, encoding='utf-8').read())
    assert data['exit-1']['seq'] == 7          # atexit flush 生效
    assert data['exit-1']['lastActiveAt'] > 0


# ---------------------------------------------------------------- 例 5：句柄随 close 关闭

def test_jsonl_handle_closed_after_close(tmp_path):
    """例 5：任务 close 后 jsonl 句柄已关——Windows 上文件可改名/删除。
    三条关闭路径都验：末条事件落卡即关（log_write 见到终态）、save 扫描关
    （无后续事件的终态）、记录被删时关（leads_hosted 回滚要 unlink）。"""
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    task = {'id': 'c1', 'seq': 0, 'events': [], 'status': 'running'}
    repo.put(task)
    repo.save()
    repo.append_event(task, 'status', text='建卡')
    assert 'c1' in repo._jsonl.openTaskIds()
    # 路径一：终态迁移后还有末条事件（closeTaskCore 的形状）
    with repo.lock:
        task['closedByUser'] = True
        task_state.transition(task, task_state.TaskState.CLOSED)
    repo.append_event(task, 'status', text='会话已结束')
    assert 'c1' not in repo._jsonl.openTaskIds()
    os.rename(str(tmp_path / 'logs' / 'c1.jsonl'),
              str(tmp_path / 'logs' / 'c1.jsonl.gone'))   # 句柄没关会 PermissionError

    # 路径二：终态后没有后续事件，靠 save 扫描关（会话线程 finally 的形状）
    task2 = {'id': 'c2', 'seq': 0, 'events': [], 'status': 'running'}
    repo.put(task2)
    repo.append_event(task2, 'status', text='建卡')
    assert 'c2' in repo._jsonl.openTaskIds()
    with repo.lock:
        task_state.transition(task2, task_state.TaskState.FAILED)
    repo.save()
    assert 'c2' not in repo._jsonl.openTaskIds()

    # 路径三：任务记录被删（repo.tasks.pop）后 save 扫描关句柄，文件可删
    task3 = {'id': 'c3', 'seq': 0, 'events': [], 'status': 'queued'}
    repo.put(task3)
    repo.append_event(task3, 'status', text='建卡即回滚')
    assert 'c3' in repo._jsonl.openTaskIds()
    with repo.lock:
        repo.tasks.pop('c3', None)
    repo.save()
    assert 'c3' not in repo._jsonl.openTaskIds()
    os.unlink(str(tmp_path / 'logs' / 'c3.jsonl'))


# ---------------------------------------------------------------- 第 1 批复验两条建议

def test_apply_patch_helper_shared_and_prefilter(tmp_path):
    """补丁应用只留一份（_applyPatch：回填与 /event/<seq> 共用）；jsonl 回填的
    'tool_args_patch' 廉价预筛不误伤——事件正文里提到这个词的事件照常回填。"""
    apply = task_events._applyPatch
    assert apply({'args': '{}', 'truncated': False},
                 {'args': '{"a":1}', 'truncated': True}) \
        == {'args': '{"a":1}', 'truncated': True}
    assert apply({'seq': 2, 'args': 'x'}, {'targetSeq': 2}) == {'seq': 2,
                                                                'args': 'x'}
    path = tmp_path / 't.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'seq': 1, 'at': 1, 'type': 'status',
                            'text': 'e1'}, ensure_ascii=False) + '\n')
        # 正文里带 tool_args_patch 的事件：预筛在 _parseLine 之后，不会误判
        f.write(json.dumps({'seq': 2, 'at': 1, 'type': 'assistant',
                            'text': '让我看看 tool_args_patch 补丁行是什么',
                            'truncated': False}, ensure_ascii=False) + '\n')
        f.write(json.dumps({'seq': 3, 'at': 1, 'type': 'tool', 'name': 'Bash',
                            'args': '', 'callId': 'c1'}, ensure_ascii=False) + '\n')
        f.write(json.dumps({'type': 'tool_args_patch', 'targetSeq': 3,
                            'args': '{"command": "ls"}', 'truncated': False,
                            'at': 2}, ensure_ascii=False) + '\n')
    events, patches = task_events.readLogTailWithPatches(str(path), 10)
    assert [e['seq'] for e in events] == [1, 2, 3]
    assert events[1]['text'] == '让我看看 tool_args_patch 补丁行是什么'
    assert len(patches) == 1
    hydrated = task_events.applyArgPatches(
        [task_events.eventFromLog(e) for e in events], patches)
    assert hydrated[2]['args'] == '{"command": "ls"}'
    assert task_events.eventBySeq(str(path), 3)['args'] == '{"command": "ls"}'
    assert task_events.eventBySeq(str(path), 2)['text'] == events[1]['text']
