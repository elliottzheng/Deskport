# -*- coding: utf-8 -*-
"""批次 G2 返工回归（独立验收打回项）：同步后验收对象、验收任务关闭接回、
接回失败新开、集成后通知、integration.log 路径。

夹具与辅助同 test_workitem_engine（桩任务 + 真 git 临时仓库）。
"""
import json
import os

import pytest
from types import SimpleNamespace

import config as configMod
from modules import agent_nodes, events as eventsMod
from modules import task_result, tasks_api, tasks_store as store
from modules import workitem_git, workitem_pipeline, workitem_projects
from modules import workitem_state, workitem_store
from modules import workitem_setup

from test_workitem_engine import (Stub, makeRepo, implDelivers, verdictOf,
                                  waitState)

IMPL_MODEL = 'kimi-impl'
VERIFY_MODEL = 'kimi-verify'


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
    repo.load()
    stub = Stub(SimpleNamespace(tmp=tmp_path, repo=repo))
    monkeypatch.setattr(tasks_api, 'createTaskResponse', stub.create)
    monkeypatch.setattr(tasks_api, 'deliverMessage', stub.deliver)
    return SimpleNamespace(tmp=tmp_path, repo=repo, stub=stub,
                           monkeypatch=monkeypatch)


def newWorkitem(env, title='返工回归', footprint=None):
    makeRepo(env.tmp, 'echo gates-ok', 'echo tests-ok', env.monkeypatch)
    payload, code = workitem_setup.create({
        'projectId': 'p1', 'title': title, 'specText': 'x',
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': footprint or ['*.py']})
    assert code == 201, payload
    return payload['workItem']['id']


def repo_path(env):
    return str(env.tmp / 'repo')


def mainAdvance(env, filename='other.py', content='o = 1\n'):
    """主仓 main 前进一步（模拟别人合并了改动）。"""
    repo = repo_path(env)
    with open(os.path.join(repo, filename), 'w', encoding='utf-8') as f:
        f.write(content)
    workitem_git.run(repo, ['add', '-A'])
    workitem_git.run(repo, ['commit', '-m', 'main 前进 %s' % filename])
    return workitem_git.head(repo)


# ---------------------------------------------------------------- 必须修 1

def test_verify_target_includes_main_after_sync(env):
    """§2b：验收对象是"已含最新 main 的提交"——同步后的 worktree 新 HEAD。"""
    wid = newWorkitem(env)
    mainHead = mainAdvance(env)                 # 切分支后 main 又前进
    wi = workitem_store.repo.get(wid)
    implDelivers(env, wi, env.stub)
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.VERIFYING
    vid = wi['verifyTasks'][-1]
    target = (wi.get('verifyMeta') or {}).get(vid) or {}
    # 验收对象包含 main 头（验方探针同款断言）
    assert target.get('target')
    assert workitem_git.isAncestor(repo_path(env), mainHead,
                                  target['target']), \
        '验收对象不含同步后的 main 头'
    # 基线 = main 头（diff 只看分支自己的改动）
    assert target.get('base') == mainHead
    # 验收 worktree 真的 detach 在这个 commit 上
    assert workitem_git.head(wi['verifyWorktree']) == target['target']
    # 交付时的旧 commit 也是它的祖先（合并没有丢实施方的改动）
    delivered = (wi.get('lastImplResult') or {}).get('commit')
    assert delivered and workitem_git.isAncestor(
        repo_path(env), delivered, target['target'])


# ---------------------------------------------------------------- 必须修 2

def _deliverAndRework(env, wid):
    """交付 → 建验收 → 验收打回 → 回到实施态（返回第一张验收任务卡）。"""
    wi = workitem_store.repo.get(wid)
    implDelivers(env, wi, env.stub)
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.VERIFYING
    verdictOf(env, wi, 'rework', ['改它'])
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.IMPLEMENTING
    return env.repo.get(wi['verifyTasks'][-1])


def test_verify_task_closed_readopts_session(env):
    """§2：返工后再验收时验收任务已关闭 → 先用 kimiSessionId 接回原会话。"""
    wid = newWorkitem(env)
    v1 = _deliverAndRework(env, wid)
    oldSession = v1['sessionId']
    tasks_api.closeTaskCore(v1)                 # 验方任务被关闭
    wi = workitem_store.repo.get(wid)
    implDelivers(env, wi, env.stub, content='x = 3\n')
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.VERIFYING
    assert len(wi['verifyTasks']) == 2         # 新的一张卡，不是复用旧会话消息
    adopted = [d for d in env.stub.created
               if d.get('expects') == 'verify'
               and d.get('kimiSessionId') == oldSession]
    assert adopted, '关闭的验收任务没有走 kimiSessionId 接回'
    assert adopted[0]['fork'] is False
    assert '重新验收' in adopted[0]['prompt']   # 接回后带的是重验指令
    assert wi['implTask']                      # 实施任务没被擅动


def test_verify_readopt_failure_falls_back_to_fresh(env):
    """接回失败（会话被占用/丢失）→ 才新开会话（平台验方模板，自含上下文）。"""
    wid = newWorkitem(env)
    v1 = _deliverAndRework(env, wid)
    env.stub.failKimi.add(v1['sessionId'])     # 接回一律 409
    tasks_api.closeTaskCore(v1)
    wi = workitem_store.repo.get(wid)
    implDelivers(env, wi, env.stub, content='x = 4\n')
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.VERIFYING
    verifies = [d for d in env.stub.created if d.get('expects') == 'verify']
    assert len(verifies) == 2
    fresh = verifies[-1]
    assert not fresh.get('kimiSessionId')      # 新开会话：没有接回参数
    assert '验方' in fresh['prompt']           # 平台模板自含规格/target/基线/次数


# ---------------------------------------------------------------- 建议修

def test_integrated_notifies_overlapping_workitem(env):
    """§2b：A 集成后，足迹有交集（A 实际 diff 撞上 B）、正在实施的 B 收到
    "提交前先 merge main"。声明不相交才并行——A 实际多改了 b.py。"""
    widA = newWorkitem(env, title='甲', footprint=['a.py'])
    widB = newWorkitem(env, title='乙', footprint=['b.py'])
    assert workitem_store.repo.get(widB)['state'] == workitem_state.IMPLEMENTING
    wiA = workitem_store.repo.get(widA)
    # 甲的交付实际动了 a.py 和 b.py（超出声明：footprintActual 校正的场景）
    worktree = wiA['worktree']
    for name in ('a.py', 'b.py'):
        with open(os.path.join(worktree, name), 'w', encoding='utf-8') as f:
            f.write('%s = 1\n' % name[0])
    workitem_git.run(worktree, ['add', '-A'])
    workitem_git.run(worktree, ['commit', '-m', 'impl a+b'])
    task = env.repo.get(wiA['implTask'])
    payload, code = task_result.submit(task, {
        'summary': '动了 a/b', 'commit': workitem_git.head(worktree),
        'branch': wiA['branch'],
        'tests': {'passed': 1, 'failed': 0, 'skipped': 0}}, via='test')
    assert code == 200, payload
    workitem_pipeline.tick()
    verdictOf(env, workitem_store.repo.get(widA), 'pass')
    workitem_pipeline.tick()
    wiA = waitState(env, widA, workitem_state.INTEGRATED)
    commit = (wiA.get('integration') or {}).get('commit')
    assert commit
    # integration.log 路径记进了工作项（§2：工作项记路径）
    assert wiA['integration']['log'] == workitem_store.logPath(widA)
    assert os.path.isfile(wiA['integration']['log'])
    assert 'gates' in workitem_store.readLog(widA)
    taskB = env.repo.get(workitem_store.repo.get(widB)['implTask'])
    notes = [m for m in env.stub.messages
             if m[0] == taskB['id'] and 'main 已含' in m[1]]
    assert notes, '集成后没有通知共享足迹的工作项'
    assert commit[:12] in notes[0][1]
    assert 'merge main' in notes[0][1]
