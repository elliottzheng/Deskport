# -*- coding: utf-8 -*-
"""批次 G2 段 3：工作项 API 与项目配置端点（鉴权同任务）。

夹具同 test_worklines：config.statePath 与各仓储指到 tmp，唤醒不起真
kimi；流水线的建任务/投递打桩（桩在 test_workitem_engine），git 用临时真仓库。
覆盖：创建/列表/详情/决策/取消的鉴权（主管只看自己的、网页全看、越权 403）
与项目配置增删改（内置不可删、node 非 local 拒绝）。
"""
import json
import os

import pytest
from types import SimpleNamespace

import config as configMod
from modules import agent_nodes, events as eventsMod, leads_store
from modules import task_result, tasks_store as store, tasks_api
from modules import workitem_projects, workitem_setup, workitem_state
from modules import workitem_store

from test_workitem_engine import Stub, makeRepo, implDelivers, verdictOf

PASSWORD = 'pw-123456'


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
    repo.load()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    stub = Stub(SimpleNamespace(tmp=tmp_path, repo=repo))
    origCreate = tasks_api.createTaskResponse
    monkeypatch.setattr(tasks_api, 'createTaskResponse', stub.create)
    monkeypatch.setattr(tasks_api, 'deliverMessage', stub.deliver)
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           repo=repo, tmp=tmp_path, app=app, stub=stub,
                           origCreate=origCreate, monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None, token=None):
    client = env.fresh if token else env.client
    kw = {'headers': {'X-Agent-Token': token}} if token else {}
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(client, method)(path, **kw)


def newLeadToken(env, name='甲'):
    resp = call(env, 'post', '/api/settings/agent-tokens', {'name': name})
    assert resp.status_code == 200, resp.get_json()
    token = resp.get_json().get('token') or ''
    lead, _err = leads_store.create(name)
    # 令牌绑定主管（leads_api 的绑定入口）
    bound = call(env, 'post', '/api/settings/leads/%s/tokens' % lead['id'],
                 {'tokenId': resp.get_json().get('id')})
    assert bound.status_code == 200, bound.get_json()
    return token, lead['id']


def newWorkitem(env, lead=''):
    makeRepo(env.tmp, 'echo ok', 'echo ok', env.monkeypatch)
    body = {'projectId': 'p1', 'title': 'API', 'specText': 'x',
            'implModel': 'm1', 'verifyModel': 'm2', 'footprint': ['*.py']}
    if lead:
        body['lead'] = lead
    resp = call(env, 'post', '/api/workitems', body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['workItem']['id']


# ---------------------------------------------------------------- 创建与鉴权

def test_create_list_detail_web_session(env):
    login(env.client)
    wid = newWorkitem(env)
    listed = call(env, 'get', '/api/workitems').get_json()['workItems']
    assert [w['id'] for w in listed] == [wid]
    detail = call(env, 'get', '/api/workitems/%s' % wid).get_json()['workItem']
    assert detail['state'] == workitem_state.IMPLEMENTING
    assert detail['stage'] == 'impl'
    assert detail['implTask'] and detail['tasks']
    # 未登录 401
    assert env.fresh.get('/api/workitems').status_code == 401


def test_lead_token_sees_only_own(env):
    login(env.client)
    token, leadId = newLeadToken(env)
    mine = newWorkitem(env, lead=leadId)
    otherLead, _err = leads_store.create('乙')
    theirs = newWorkitem(env, lead=otherLead['id'])
    listed = call(env, 'get', '/api/workitems', token=token).get_json()
    assert [w['id'] for w in listed['workItems']] == [mine]
    assert call(env, 'get', '/api/workitems/%s' % theirs,
                token=token).status_code == 403
    assert call(env, 'get', '/api/workitems/%s' % mine,
                token=token).status_code == 200
    # 网页会话全看
    assert len(call(env, 'get', '/api/workitems').get_json()['workItems']) == 2


def test_decision_requires_judgment_and_ownership(env):
    login(env.client)
    wid = newWorkitem(env)
    # 非 needs_judgment 状态决策 → 409
    resp = call(env, 'post', '/api/workitems/%s/decision' % wid,
                {'action': 'cancel'})
    assert resp.status_code == 409
    # 造一个 needs_judgment：实施结果带 questions
    wi = workitem_store.repo.get(wid)
    task = env.repo.get(wi['implTask'])
    worktree = wi['worktree']
    with open(os.path.join(worktree, 'a.py'), 'w', encoding='utf-8') as f:
        f.write('a = 1\n')
    from modules import workitem_git, workitem_pipeline
    workitem_git.run(worktree, ['add', '-A'])
    workitem_git.run(worktree, ['commit', '-m', 'a'])
    task_result.submit(task, {
        'summary': 's', 'commit': workitem_git.head(worktree),
        'tests': {'passed': 1, 'failed': 0, 'skipped': 0},
        'questions': ['用哪个库？']}, via='test')
    workitem_pipeline.tick()
    assert workitem_store.repo.get(wid)['state'] == workitem_state.NEEDS_JUDGMENT
    # 越权决策：别的主管令牌 403
    otherToken, _otherId = newLeadToken(env, '丙')
    resp = call(env, 'post', '/api/workitems/%s/decision' % wid,
                {'action': 'cancel'}, token=otherToken)
    assert resp.status_code == 403, resp.get_json()
    # 网页会话决策：cancel → 终态
    resp = call(env, 'post', '/api/workitems/%s/decision' % wid,
                {'action': 'cancel'})
    assert resp.status_code == 200, resp.get_json()
    assert workitem_store.repo.get(wid)['state'] == workitem_state.CANCELLED


def test_cancel_endpoint(env):
    login(env.client)
    wid = newWorkitem(env)
    resp = call(env, 'post', '/api/workitems/%s/cancel' % wid)
    assert resp.status_code == 200
    assert workitem_store.repo.get(wid)['state'] == workitem_state.CANCELLED
    # 重复取消幂等
    assert call(env, 'post', '/api/workitems/%s/cancel' % wid).status_code == 200
    assert call(env, 'get', '/api/workitems/nope').status_code == 404


# ---------------------------------------------------------------- 项目配置

def test_projects_crud_web_only(env):
    login(env.client)
    projects = call(env, 'get', '/api/settings/projects').get_json()['projects']
    assert [p['id'] for p in projects] == ['deskport']
    # 令牌不能碰项目配置（/api/settings 不走令牌通道）
    assert env.fresh.get('/api/settings/projects').status_code == 401
    # 新增
    repo = env.tmp / 'repo2'
    (repo / '.git').mkdir(parents=True)
    resp = call(env, 'post', '/api/settings/projects', {
        'id': 'other', 'name': '别的仓库', 'repoPath': str(repo),
        'worktreeRoot': str(env.tmp / 'wt2')})
    assert resp.status_code == 200, resp.get_json()
    assert len(call(env, 'get', '/api/settings/projects').get_json()
               ['projects']) == 2
    # 改
    resp = call(env, 'post', '/api/settings/projects', {
        'id': 'other', 'name': '改名了', 'repoPath': str(repo),
        'worktreeRoot': str(env.tmp / 'wt2')})
    assert resp.status_code == 200
    assert workitem_projects.repo.get('other')['name'] == '改名了'
    # 非法：node 非 local / repoPath 不存在
    resp = call(env, 'post', '/api/settings/projects', {
        'id': 'bad', 'name': 'x', 'repoPath': str(repo),
        'worktreeRoot': str(env.tmp / 'wt3'), 'node': 'node-1'})
    assert resp.status_code == 400 and 'node' in resp.get_json()['fields']
    # 删（内置不可删）
    resp = call(env, 'delete', '/api/settings/projects/deskport')
    assert resp.status_code == 400
    resp = call(env, 'delete', '/api/settings/projects/other')
    assert resp.status_code == 200
    assert workitem_projects.repo.get('other') is None
    assert call(env, 'delete', '/api/settings/projects/other').status_code == 404


def test_engine_creates_task_without_request_context(env):
    """批次 G2 回归：流水线在后台线程建任务只有 app 上下文（没有请求）——
    createTaskResponse 里的审计调用不能读 request.remote_addr 炸掉。"""
    from flask import g
    makeRepo(env.tmp, 'echo ok', 'echo ok', env.monkeypatch)
    workitem_projects.repo.load()
    env.monkeypatch.setattr(tasks_api, 'createTaskResponse', env.origCreate)
    env.monkeypatch.setattr(configMod, 'getWorkDirs', lambda: [str(env.tmp)])
    with env.app.app_context():
        g.agentTokenName = '流水线'      # 流水线建卡的署名（触发审计分支）
        g.agentLead = ''
        payload, code = tasks_api.createTaskResponse({
            'prompt': '流水线建卡回归', 'cwd': str(env.tmp),
            'expects': '', 'workItemId': 'wi-regres'}, start=False)
    assert code == 202, (code, payload)
    task = (payload.get('task') or {})
    assert task.get('owner') == '流水线'
    stored = env.repo.get(task['id'])
    assert stored.get('workItemId') == 'wi-regres'
    assert stored.get('expects') == ''
