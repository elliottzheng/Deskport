# -*- coding: utf-8 -*-
"""批次 G2 巡检单测：任务起不来 / 提前收工 / 执行卡死 / 上下文换手。

时钟可注入（fake time）：退避 10s/60s、卡死 600s、settle 30s 全是墙上
时钟，测试里推着走。任务面仍是桩（见 test_workitem_engine 的 Stub），
cancel 也打桩——只验证"流水线做了什么"，不起真会话。
"""
import json
import os

import pytest
from types import SimpleNamespace

import config as configMod
from modules import agent_nodes, events as eventsMod, leads_store
from modules import task_result, tasks_store as store, tasks_api
from modules import task_state
from modules import workitem_core as core
from modules import workitem_git, workitem_pipeline, workitem_projects
from modules import workitem_state, workitem_store, workitem_watch
from modules import workitem_actions, workitem_setup

from test_workitem_engine import Stub, makeRepo


class FakeTime:
    """可推的时钟：watch 里的 time.time()/monotonic() 都走它。"""

    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, _sec):
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': 'pw-123456'}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    configMod._cache.update(mtime=None, data=None)
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: {})
    repo = store.TaskRepository(str(tmp_path / 'state' / 'tasks.json'),
                                str(tmp_path / 'state' / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(workitem_store, 'repo',
                        workitem_store.WorkitemRepository(
                            str(tmp_path / 'state' / 'workitems.json')))
    monkeypatch.setattr(workitem_projects, 'repo',
                        workitem_projects.ProjectRepository(
                            str(tmp_path / 'state' / 'projects.json')))
    monkeypatch.setattr(agent_nodes, 'shutdownNodeSession',
                        lambda node, sid: True)
    repo.load()      # 先加载（生产同：register 时加载）——否则首次 append_event
                     # 触发惰性 load 的"重启恢复"，把刚建的 waiting 任务打成 paused
    clock = FakeTime()
    monkeypatch.setattr(workitem_watch, 'time', clock)
    return SimpleNamespace(tmp=tmp_path, repo=repo, clock=clock,
                           monkeypatch=monkeypatch)


def newWorkitem(env, monkeypatch, **kw):
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo ok', 'echo ok', monkeypatch)
    body = {'projectId': 'p1', 'title': '巡检', 'specText': 'x',
            'implModel': 'kimi-impl', 'verifyModel': 'kimi-verify',
            'footprint': ['*.py']}
    body.update(kw)
    payload, code = workitem_setup.create(body)
    assert code == 201, payload
    return payload['workItem']['id'], stub


# ---------------------------------------------------------------- 起不来

def test_start_failure_retries_then_judgment(env, monkeypatch):
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    # 会话从没起来（无 sessionId 就终态）
    task['sessionId'] = ''
    task['startedAt'] = 0
    task_state.transition(task, task_state.TaskState.INTERRUPTED, '起不来')

    workitem_watch.sweep()
    wi = workitem_store.repo.get(wid)
    assert wi['startFails'] == 1, \
        'startFails=%s history=%s' % (wi['startFails'],
                                      json.dumps(wi.get('history')[-3:],
                                                 ensure_ascii=False))
    impls = [d for d in stub.created if d.get('expects') == 'impl']
    assert len(impls) == 2                      # 重试：换了张新任务卡
    assert wi['implTask'] != task['id']
    assert wi['state'] == workitem_state.IMPLEMENTING
    # 重试的这张卡也没起来（没起过会话：无 sessionId/startedAt）
    second = env.repo.get(wi['implTask'])
    second['sessionId'] = ''
    second['startedAt'] = 0
    task_state.transition(second, task_state.TaskState.INTERRUPTED, '又没起来')

    # 退避窗口内（<60s）不再重试
    workitem_watch.sweep()
    assert len([d for d in stub.created if d.get('expects') == 'impl']) == 2

    # 60s 后第二次重试；再失败即到顶 → needs_judgment
    env.clock.now += 61
    workitem_watch.sweep()
    assert workitem_store.repo.get(wid)['startFails'] == 2
    third = env.repo.get(workitem_store.repo.get(wid)['implTask'])
    third['sessionId'] = ''
    third['startedAt'] = 0
    task_state.transition(third, task_state.TaskState.INTERRUPTED, '还没起来')
    workitem_watch.sweep()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.NEEDS_JUDGMENT
    assert '没起来' in wi['judgmentReason']


# ---------------------------------------------------------------- 提前收工

def test_empty_turn_nudges_then_switches_model(env, monkeypatch):
    wid, stub = newWorkitem(env, monkeypatch)
    project = workitem_projects.repo.get('p1')
    project['fallbackModels'] = ['kimi-alt']
    workitem_projects.repo.put(project)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    task['status'] = 'waiting'
    task['seq'] = 10
    task['lastTurn'] = {'tools': 0, 'finalLen': 0, 'empty': True, 'silent': False}

    workitem_watch.sweep()
    nudges = [m for m in stub.messages if '后台任务' in m[1]]
    assert len(nudges) == 1, \
        'nudges=%s created=%s' % ([m[0][:8] for m in stub.messages],
                                  [d.get('title') for d in stub.created])
    assert nudges[0][0] == wi['implTask'], \
        'nudge->%s implTask=%s created=%s streak=%s earlyFinish=%s' % (
            nudges[0][0][:8], wi['implTask'][:8],
            [(d.get('expects'), d.get('model')) for d in stub.created],
            wi.get('earlyStreak'), wi.get('earlyFinish'))
    # 同一轮（seq 没变）不重复追
    workitem_watch.sweep()
    assert len([m for m in stub.messages if '后台任务' in m[1]]) == 1

    # 第二轮又空转（seq 抬上去）：连续 2 次 → 换备选模型新开会话
    task['seq'] = 20
    workitem_watch.sweep()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.IMPLEMENTING
    assert wi['implModel'] == 'kimi-alt'
    alt = [d for d in stub.created
           if d.get('expects') == 'impl' and d.get('model') == 'kimi-alt']
    assert alt, '没有按备选模型换手'
    assert '接棒' in alt[0]['prompt'] or '连续提前收工' in alt[0]['prompt']


def test_background_tool_counts_as_early_finish(env, monkeypatch):
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    task['status'] = 'waiting'
    task['seq'] = 5
    task['lastTurn'] = {'tools': 2, 'finalLen': 30, 'empty': False,
                        'silent': False}
    store.repo.append_event(task, 'tool', name='Bash',
                            args='{"command": "sleep 999", '
                                 '"run_in_background": true}')
    workitem_watch.sweep()
    nudges = [m for m in stub.messages if '后台任务' in m[1]]
    assert nudges, '本轮有会话自带后台任务：应追发'


def test_normal_turn_clears_streak(env, monkeypatch):
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    wi['earlyStreak'] = 1
    workitem_store.repo.save()
    task = env.repo.get(wi['implTask'])
    task['status'] = 'waiting'
    task['seq'] = 9
    task['lastTurn'] = {'tools': 3, 'finalLen': 50, 'empty': False,
                        'silent': False}
    workitem_watch.sweep()
    assert workitem_store.repo.get(wid)['earlyStreak'] == 0
    assert not stub.messages


# ---------------------------------------------------------------- 卡死

def test_stall_cancels_then_resends(env, monkeypatch):
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    task['status'] = 'running'
    task['lastActiveAt'] = env.clock.now - 700      # 11 分钟无事件
    task['_currentTool'] = None
    cancelled = []

    def fakeCancel(taskId):
        cancelled.append(taskId)
        task['status'] = 'waiting'
        return ({'ok': True, 'status': 'waiting'}, 200)

    monkeypatch.setattr(tasks_api, 'cancelTask', fakeCancel)

    workitem_watch.sweep()
    assert cancelled == [task['id']]               # 先 cancel
    assert workitem_store.repo.get(wid)['stalls'] == 1
    # 30s 内只等，不重发
    env.clock.now += 5
    workitem_watch.sweep()
    assert not [m for m in stub.messages if '平台重发' in m[1]]
    # 30s 后回到 waiting：重发未完成指令
    env.clock.now += 30
    workitem_watch.sweep()
    resent = [m for m in stub.messages if '平台重发' in m[1]]
    assert resent and resent[0][0] == task['id']


def test_stall_cancel_ineffective_revives(env, monkeypatch):
    """cancel 叫不醒（30s 还 running）→ close + kimiSessionId 接回。"""
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    oldSession = task['sessionId']
    task['status'] = 'running'
    task['lastActiveAt'] = env.clock.now - 700

    def fakeCancel(taskId):
        return ({'ok': True, 'status': 'running'}, 200)   # cancel 无效

    monkeypatch.setattr(tasks_api, 'cancelTask', fakeCancel)
    workitem_watch.sweep()
    env.clock.now += 31
    workitem_watch.sweep()
    adopted = [d for d in stub.created
               if d.get('kimiSessionId') == oldSession]
    assert adopted, '卡死 cancel 无效后应接回原会话'
    assert workitem_store.repo.get(wid)['implTask'] != task['id']


# ---------------------------------------------------------------- 换手

def test_context_handoff_closes_and_reopens(env, monkeypatch):
    """上下文 ≥50%：不在原会话追发，关掉它新开会话接棒。"""
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    task['usage'] = {'used': 60, 'size': 100}
    ok = workitem_actions.sendToImpl(wi, '继续改', '返工')
    assert ok
    assert env.repo.get(task['id'])['status'] == 'closed'    # 旧会话关掉
    wi = workitem_store.repo.get(wid)
    assert wi['implTask'] != task['id']
    assert wi['handoffs'] == 1
    fresh = [d for d in stub.created if d.get('expects') == 'impl'][-1]
    assert '接棒' in fresh['prompt']
    assert '继续改' in fresh['prompt']              # 未完成的指令带着走
    # 出过事故后阈值降到 30%
    wi['stalls'] = 1
    workitem_store.repo.save()
    task2 = env.repo.get(wi['implTask'])
    task2['usage'] = {'used': 35, 'size': 100}
    workitem_actions.sendToImpl(wi, '再改', '返工')
    assert workitem_store.repo.get(wid)['handoffs'] == 2


def test_ended_task_rework_readopts(env, monkeypatch):
    """实施任务已结束：返工消息走 kimiSessionId 接回，不新建空会话。"""
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    oldSession = task['sessionId']
    tasks_api.closeTaskCore(task)
    workitem_actions.sendToImpl(wi, '【验收退回】修', '返工')
    adopted = [d for d in stub.created
               if d.get('kimiSessionId') == oldSession]
    assert adopted and adopted[0]['fork'] is False


def test_acp_process_exit_readopts_and_resends(env, monkeypatch):
    """提前收工③（确定性）：会话进程中途退出（有 sessionId 的 failed）→
    用 kimiSessionId 接回原会话，未完成的指令重发（规格 §2）。"""
    wid, stub = newWorkitem(env, monkeypatch)
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    oldSession = task['sessionId']
    store.repo.append_event(task, 'user', text='【目标】改 README\n【规格】加一行')
    task['error'] = 'ACP 进程已退出'
    task_state.transition(task, task_state.TaskState.FAILED, 'ACP 进程已退出')
    workitem_watch.sweep()
    adopted = [d for d in stub.created
               if d.get('kimiSessionId') == oldSession]
    assert adopted, 'ACP 退出后没有接回原会话'
    assert adopted[0]['fork'] is False
    assert '【规格】加一行' in adopted[0]['prompt']   # 未完成的指令重发
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.IMPLEMENTING
    assert wi['earlyStreak'] == 1                    # 计入连续事故
    assert wi['implTask'] != task['id']


def test_acp_exit_twice_switches_model(env, monkeypatch):
    """连续两次进程退出 → 换项目备选模型新开会话接棒。"""
    wid, stub = newWorkitem(env, monkeypatch)
    project = workitem_projects.repo.get('p1')
    project['fallbackModels'] = ['kimi-alt']
    workitem_projects.repo.put(project)
    for round_no in (1, 2):
        wi = workitem_store.repo.get(wid)
        task = env.repo.get(wi['implTask'])
        store.repo.append_event(task, 'user', text='第 %d 轮指令' % round_no)
        task['error'] = 'ACP 进程已退出'
        task_state.transition(task, task_state.TaskState.FAILED, '退出')
        workitem_watch.sweep()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.IMPLEMENTING
    assert wi['implModel'] == 'kimi-alt'
    alt = [d for d in stub.created
           if d.get('expects') == 'impl' and d.get('model') == 'kimi-alt']
    assert alt, '连续退出后没有换备选模型'
