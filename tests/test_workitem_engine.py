# -*- coding: utf-8 -*-
"""批次 G2 流水线集成测试：桩任务 + 真 git 仓库，走完整条流水线。

链路灯（规格 §5）：实施交付 → 自动建验收任务（模型/expects/cwd 正确）→
rework 回同一实施任务（断言没有新建任务）→ 再交付 → 复用同一验收会话 →
pass → 集成（临时 git 仓库真跑 merge/gates/test 桩命令）→ integrated；
集成失败 → abort 且返工回实施任务。另覆盖：已关闭任务返工走
kimiSessionId 接回、足迹排队、重启恢复幂等。

桩替换两层：tasks_api.createTaskResponse（建卡不起真会话）与
tasks_api.deliverMessage（投递只记账）；结果提交走真的
task_result.submit（存储/事件/收件箱全是真链路），git 全程真跑。
"""
import json
import os
import subprocess
import time

import pytest
from types import SimpleNamespace

import config as configMod
from modules import agent_nodes, events as eventsMod
from modules import leads_store, task_result, tasks_store as store
from modules import tasks_api
from modules import workitem_git, workitem_pipeline, workitem_projects
from modules import workitem_recover, workitem_setup, workitem_state
from modules import workitem_store
from modules import workitem_actions

PASSWORD = 'pw-123456'
IMPL_MODEL = 'kimi-impl'
VERIFY_MODEL = 'kimi-verify'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

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
    repo.load()      # 先加载（生产同：register 时加载）：否则首次 append_event
    # 会触发惰性 load 的"重启恢复"，把刚建的任务打成 paused
    return SimpleNamespace(tmp=tmp_path, repo=repo, monkeypatch=monkeypatch)


class Stub:
    """桩任务面：建卡不起会话、投递只记账；返回的形状与真链路一致。"""

    def __init__(self, env):
        self.env = env
        self.created = []        # 建卡入参（按序）
        self.messages = []       # (taskId, text, by)

    def install(self, monkeypatch):
        monkeypatch.setattr(tasks_api, 'createTaskResponse', self.create)
        monkeypatch.setattr(tasks_api, 'deliverMessage', self.deliver)

    def create(self, data, start=True):
        import uuid
        self.created.append(dict(data))
        taskId = uuid.uuid4().hex[:12]
        task = {
            'id': taskId, 'title': data.get('title') or '',
            'prompt': data.get('prompt') or '',
            'workspace': os.path.join(self.env.tmp, 'ws', taskId),
            'status': 'waiting', 'createdAt': time.time(),
            'startedAt': time.time(), 'seq': 0, 'events': [], 'lastText': '',
            'resultFiles': [], 'error': '',
            'sessionId': 'session-%s' % uuid.uuid4().hex,
            'kimiSessionId': data.get('kimiSessionId') or '',
            'kimiSessionFork': bool(data.get('fork')),
            'owner': '面板', 'lead': '', 'mode': data.get('mode') or 'default',
            'model': data.get('model') or '',
            'modelEffective': data.get('model') or '',
            'policy': data.get('policy') or {}, 'resume': True,
            'closedByUser': False, 'pendingPermission': '',
            'lastActiveAt': time.time(), 'node': data.get('node') or 'local',
            'cwd': data.get('cwd') or '', 'sources': [],
            'expects': data.get('expects') or '',
            'workItemId': data.get('workItemId') or '', 'endedAt': 0,
        }
        os.makedirs(task['workspace'], exist_ok=True)
        self.env.repo.put(task)
        self.env.repo.save()
        self.env.repo.append_event(task, 'status', text='任务已创建（桩）')
        return {'task': self.env.repo.summary(task), 'paths': [],
                'titleSource': 'stub'}, 202

    def deliver(self, task, text, mode='queue', waitSec=None, by=''):
        self.messages.append((task.get('id') or '', text, by))
        self.env.repo.append_event(task, 'user', text=text, by=by or '面板')
        return {'ok': True, 'status': task.get('status') or ''}, 202


def makeRepo(tmp_path, gatesCmd, testCmd, monkeypatch):
    """临时 git 仓库 + 指向它的项目（worktreeRoot 在仓库外）。"""
    repo = tmp_path / 'repo'
    repo.mkdir()
    for args in (['init', '-b', 'main'], ['config', 'user.email', 't@t'],
                 ['config', 'user.name', 't']):
        subprocess.run(['git'] + args, cwd=repo, check=True,
                       capture_output=True)
    (repo / 'README.md').write_text('base\n', encoding='utf-8')
    for args in (['add', '-A'], ['commit', '-m', 'base']):
        subprocess.run(['git'] + args, cwd=repo, check=True, capture_output=True)
    clean, errors = workitem_projects.validate({
        'id': 'p1', 'name': '测试项目', 'repoPath': str(repo),
        'worktreeRoot': str(tmp_path / 'wt'), 'mainBranch': 'main',
        'gatesCmd': gatesCmd, 'testCmd': testCmd,
        'setupNotes': '测试边界：只改声明过的足迹。'})
    assert errors == {}, errors
    workitem_projects.repo.put(clean)
    return str(repo)


def implDelivers(env, wi, stub, filename='impl.py', content='x = 1\n'):
    """实施方"交付"：真在 worktree 里提交，再走真 submit_result。"""
    worktree = wi['worktree']
    with open(os.path.join(worktree, filename), 'w', encoding='utf-8') as f:
        f.write(content)
    workitem_git.run(worktree, ['add', '-A'])
    workitem_git.run(worktree, ['commit', '-m', 'impl %s' % filename])
    commit = workitem_git.head(worktree)
    task = env.repo.get(wi['implTask'])
    payload, code = task_result.submit(task, {
        'summary': '交付了 %s' % filename, 'commit': commit,
        'branch': wi['branch'],
        'tests': {'passed': 3, 'failed': 0, 'skipped': 0}}, via='test')
    assert code == 200, payload
    return commit


def verdictOf(env, wi, verdict, mustFix=None):
    task = env.repo.get(wi['verifyTasks'][-1])
    target = ((wi.get('verifyMeta') or {}).get(task['id']) or {}).get('target') \
        or wi.get('lastImplCommit')
    payload, code = task_result.submit(task, {
        'verdict': verdict, 'target': target,
        'summary': '%s by stub' % verdict,
        'mustFix': mustFix or []}, via='test')
    assert code == 200, payload
    return payload


def waitState(env, wid, wanted, timeout=60):
    """等工作项到 wanted（集成作业在后台线程里跑，回调直接改状态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = (workitem_store.repo.get(wid) or {}).get('state')
        if state == wanted:
            return workitem_store.repo.get(wid)
        time.sleep(0.05)
    wi = workitem_store.repo.get(wid) or {}
    raise AssertionError('等不到 %s（现在 %s，history=%s）'
                         % (wanted, wi.get('state'),
                            json.dumps(wi.get('history')[-4:], ensure_ascii=False)))


# ---------------------------------------------------------------- 主链路

def test_full_pipeline_pass(env, monkeypatch):
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo gates-ok', 'echo tests-ok', monkeypatch)
    payload, code = workitem_setup.create({
        'projectId': 'p1', 'title': '存储改造', 'specText': '在仓库里加一个文件',
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': ['*.py']})
    assert code == 201, payload
    wid = payload['workItem']['id']
    wi = workitem_store.repo.get(wid)
    # 开工：worktree/分支/实施任务（模型/expects/cwd 正确）
    assert wi['state'] == workitem_state.IMPLEMENTING
    assert wi['attempt'] == 1
    assert os.path.isdir(wi['worktree'])
    implData = [d for d in stub.created if d.get('expects') == 'impl']
    assert len(implData) == 1
    assert implData[0]['model'] == IMPL_MODEL
    assert implData[0]['cwd'] == wi['worktree']
    assert implData[0]['workItemId'] == wid
    assert 'submit_result' in implData[0]['prompt']
    implTaskId = wi['implTask']

    commit = implDelivers(env, wi, stub)
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    # 自动建验收任务：模型/expects/cwd 正确，worktree detach 到交付 commit
    assert wi['state'] == workitem_state.VERIFYING
    verifyData = [d for d in stub.created if d.get('expects') == 'verify']
    assert len(verifyData) == 1
    assert verifyData[0]['model'] == VERIFY_MODEL
    assert verifyData[0]['cwd'] == wi['verifyWorktree']
    assert workitem_git.head(wi['verifyWorktree']) == commit
    assert wi['lastImplCommit'] == commit

    # rework：回同一实施任务（没有新建任务），attempt+1
    verdictOf(env, wi, 'rework', ['把变量名改对'])
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.IMPLEMENTING
    assert wi['attempt'] == 2
    assert len([d for d in stub.created if d.get('expects') == 'impl']) == 1
    assert wi['implTask'] == implTaskId
    rework = [m for m in stub.messages if '把变量名改对' in m[1]]
    assert rework and rework[0][0] == wi['implTask']

    # 再交付 → 复用同一验收会话（没有新建验收任务），worktree 切到新 commit
    commit2 = implDelivers(env, wi, stub, filename='impl.py', content='x = 2\n')
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert len([d for d in stub.created if d.get('expects') == 'verify']) == 1
    assert workitem_git.head(wi['verifyWorktree']) == commit2
    reverify = [m for m in stub.messages if '重新验收' in m[1]]
    assert reverify and reverify[0][0] == wi['verifyTasks'][-1]

    # pass → 集成（真 merge + 桩 gates/tests）→ integrated
    verdictOf(env, wi, 'pass')
    workitem_pipeline.tick()
    wi = waitState(env, wid, workitem_state.INTEGRATED)
    assert wi['integration']['commit'] and wi['integration']['gates'] == 'pass'
    # 主仓真收到了合并提交，且内容在
    log = subprocess.run(['git', 'log', '--oneline', '-3'],
                         cwd=env.tmp / 'repo', capture_output=True, text=True,
                         encoding='utf-8').stdout
    assert 'Merge workitem %s' % wid in log
    assert (env.tmp / 'repo' / 'impl.py').read_text(encoding='utf-8') == 'x = 2\n'
    # 收尾：任务关闭、worktree 拆掉
    assert env.repo.get(wi['implTask'])['status'] in ('closed', 'cancelled')
    assert not os.path.isdir(wi['worktree'])


def test_integration_failure_aborts_and_reworks(env, monkeypatch):
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo GATES-RED 1>&2 & exit 1', 'echo tests-ok', monkeypatch)
    payload, _code = workitem_setup.create({
        'projectId': 'p1', 'title': '门禁会红', 'specText': '加文件',
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': ['*.py']})
    wid = payload['workItem']['id']
    wi = workitem_store.repo.get(wid)
    implDelivers(env, wi, stub)
    workitem_pipeline.tick()
    verdictOf(env, workitem_store.repo.get(wid), 'pass')
    workitem_pipeline.tick()
    wi = waitState(env, wid, workitem_state.IMPLEMENTING)
    # 失败输出作为返工发回实施任务；attempt+1；主仓回到合并前
    assert wi['attempt'] == 2
    rework = [m for m in stub.messages if '集成失败' in m[1]]
    assert rework, [m[1][:40] for m in stub.messages]
    assert 'GATES-RED' in rework[0][1]
    head = subprocess.run(['git', 'log', '--oneline'], cwd=env.tmp / 'repo',
                          capture_output=True, text=True,
                          encoding='utf-8').stdout
    assert 'Merge workitem' not in head
    assert not (env.tmp / 'repo' / 'impl.py').exists()
    log = workitem_store.readLog(wid)
    assert 'gates' in log and 'GATES-RED' in log


def test_rework_after_impl_task_closed_readopts_session(env, monkeypatch):
    """已关闭的实施任务返工 → 走 kimiSessionId 接回原会话（规格 §2）。"""
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo ok', 'echo ok', monkeypatch)
    payload, _code = workitem_setup.create({
        'projectId': 'p1', 'title': '接回', 'specText': '加文件',
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': ['*.py']})
    wid = payload['workItem']['id']
    wi = workitem_store.repo.get(wid)
    implDelivers(env, wi, stub)
    workitem_pipeline.tick()
    verdictOf(env, workitem_store.repo.get(wid), 'rework', ['修它'])
    # 先把实施任务关掉（模拟"任务已关闭"），再让流水线处理返工
    implTask = env.repo.get(wi['implTask'])
    oldSession = implTask['sessionId']
    tasks_api.closeTaskCore(implTask)
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.IMPLEMENTING
    # 新任务卡接回了原 kimi 会话（fork=false），且是同一 worktree
    adopted = [d for d in stub.created if d.get('kimiSessionId') == oldSession]
    assert adopted, '没有用 kimiSessionId 接回原会话'
    assert adopted[0]['fork'] is False
    assert adopted[0]['cwd'] == wi['worktree']
    assert wi['implTask'] != implTask['id']


def test_footprint_conflict_queues_second(env, monkeypatch):
    """§2b：同一项目足迹相交的工作项不并行实施——第二个排队并显示等谁。"""
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo ok', 'echo ok', monkeypatch)
    def newWi(title, footprint):
        payload, _code = workitem_setup.create({
            'projectId': 'p1', 'title': title, 'specText': 'x',
            'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
            'footprint': footprint})
        return payload['workItem']['id']

    widA, widB = newWi('甲', ['modules/*.py']), newWi('乙', ['modules/workitem_*.py'])
    wiA, wiB = workitem_store.repo.get(widA), workitem_store.repo.get(widB)
    assert wiA['state'] == workitem_state.IMPLEMENTING
    assert wiB['state'] == workitem_state.QUEUED
    assert wiB['queuedFor'] == widA
    assert wiB['queuedShare']
    assert len([d for d in stub.created if d.get('expects') == 'impl']) == 1
    # 无交集的第三个直接并行
    third, _code = workitem_setup.create({
        'projectId': 'p1', 'title': '丙', 'specText': 'x',
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': ['docs/*.md']})
    assert workitem_store.repo.get(third['workItem']['id'])['state'] \
        == workitem_state.IMPLEMENTING
    assert len([d for d in stub.created if d.get('expects') == 'impl']) == 2


def test_questions_go_to_judgment_with_inbox(env, monkeypatch):
    """实施结果带 questions → needs_judgment + 主管收件箱 workitem_judgment。"""
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo ok', 'echo ok', monkeypatch)
    lead, _err = leads_store.create('甲主管')
    payload, _code = workitem_setup.create({
        'projectId': 'p1', 'title': '有疑问', 'specText': 'x', 'lead': lead['id'],
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': ['*.py']})
    assert 'workItem' in payload, payload
    wid = payload['workItem']['id']
    wi = workitem_store.repo.get(wid)
    worktree = wi['worktree']
    with open(os.path.join(worktree, 'a.py'), 'w', encoding='utf-8') as f:
        f.write('a = 1\n')
    workitem_git.run(worktree, ['add', '-A'])
    workitem_git.run(worktree, ['commit', '-m', 'a'])
    task = env.repo.get(wi['implTask'])
    payload2, code = task_result.submit(task, {
        'summary': '做了一半，有问题', 'commit': workitem_git.head(worktree),
        'tests': {'passed': 1, 'failed': 0, 'skipped': 0},
        'questions': ['用哪个数据库？']}, via='test')
    assert code == 200
    workitem_pipeline.tick()
    wi = workitem_store.repo.get(wid)
    assert wi['state'] == workitem_state.NEEDS_JUDGMENT
    assert wi['judgmentFrom'] == workitem_state.IMPLEMENTING
    inbox = (env.tmp / 'state' / 'lead_inbox' / (lead['id'] + '.jsonl')).read_text(
        encoding='utf-8')
    assert 'workitem_judgment' in inbox
    assert '用哪个数据库' in inbox
    _result, code = workitem_actions.decide(wid, 'cancel')   # cancel → 终态+收尾
    assert code == 200
    assert workitem_store.repo.get(wid)['state'] == workitem_state.CANCELLED


def test_restart_recovery_is_idempotent(env, monkeypatch):
    """重启恢复：指针从台账重建，不重复建任务（幂等键）。"""
    stub = Stub(env)
    stub.install(monkeypatch)
    makeRepo(env.tmp, 'echo ok', 'echo ok', monkeypatch)
    payload, _code = workitem_setup.create({
        'projectId': 'p1', 'title': '重启', 'specText': 'x',
        'implModel': IMPL_MODEL, 'verifyModel': VERIFY_MODEL,
        'footprint': ['*.py']})
    wid = payload['workItem']['id']
    wi = workitem_store.repo.get(wid)
    implTaskId = wi['implTask']
    # 模拟进程死在"建了任务还没落指针"之后
    wi['implTask'] = ''
    workitem_store.repo.save()
    assert workitem_recover.recover()['adopted'] == 1
    assert workitem_store.repo.get(wid)['implTask'] == implTaskId
    # 再恢复一次：没有新任务、指针不动（幂等）
    before = len(stub.created)
    assert workitem_recover.recover()['adopted'] == 0
    assert len(stub.created) == before
    assert workitem_store.repo.get(wid)['implTask'] == implTaskId
