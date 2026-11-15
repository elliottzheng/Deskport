# -*- coding: utf-8 -*-
"""批次 G1 结构化结果：schema 校验、HTTP 提交面、会话令牌、存储与主管收件箱。

夹具同 test_leads_inbox_store（env/login/call/newToken/newLead）：config.statePath
与任务仓储指到 tmp，唤醒不起真 kimi。提交面（POST /api/tasks/<id>/result）与
MCP 面（tests/test_task_result_mcp.py）共用 modules/task_result.submit。
"""
import json
import os

import pytest
from types import SimpleNamespace

import config as configMod
from modules import events as eventsMod
from modules import leads_inbox, task_result, task_result_mcp
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'

IMPL_OK = {'summary': '交付了 X', 'commit': 'abc1234',
           'tests': {'passed': 12, 'failed': 0, 'skipped': 1}}
VERIFY_OK = {'verdict': 'pass', 'target': '0badf00d', 'summary': '验过了'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def fakeLog(action, target='', result='ok', detail='', ip=''):
        audit.append({'action': action, 'target': target, 'detail': detail})

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', fakeLog)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           audit=audit, repo=repo, tmp=tmp_path, app=app,
                           monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None, token=None, headers=None):
    client = env.fresh if (token or headers) else env.client
    kw = {'headers': dict(headers or {})}
    if token:
        kw['headers']['X-Agent-Token'] = token
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(client, method)(path, **kw)


def newToken(env, name='T1'):
    resp = call(env, 'post', '/api/settings/agent-tokens', {'name': name})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def newLead(env, name='甲', **kw):
    body = {'name': name}
    body.update(kw)
    resp = call(env, 'post', '/api/settings/leads', body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['lead']['id']


def bind(env, leadId, tokenId):
    return call(env, 'post', '/api/settings/leads/%s/tokens' % leadId,
                {'tokenId': tokenId})


def newTask(env, prompt='做件事', token=None, expects=None):
    body = {'prompt': prompt}
    if expects:
        body['expects'] = expects
    resp = call(env, 'post', '/api/tasks', body, token=token)
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def resultEvents(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
        return [e for e in task.get('events') or [] if e.get('type') == 'result']


def logText(env, tid):
    path = os.path.join(str(env.tmp), 'logs', tid + '.jsonl')
    if not os.path.isfile(path):
        return ''
    return open(path, encoding='utf-8').read()


def inboxKinds(env, leadId):
    path = os.path.join(str(env.tmp), 'state', 'lead_inbox',
                        '%s.jsonl' % leadId)
    if not os.path.isfile(path):
        return []
    return [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]


# ---------------------------------------------------------------- 建卡与 schema

def test_create_task_expects_field(env):
    """expects 建卡时传：impl/verify 收，其它值 400 带提示。"""
    login(env.client)
    assert call(env, 'post', '/api/tasks',
                {'prompt': 'x', 'expects': 'impl'}).status_code == 202
    assert call(env, 'post', '/api/tasks',
                {'prompt': 'x', 'expects': 'verify'}).status_code == 202
    resp = call(env, 'post', '/api/tasks',
                {'prompt': 'x', 'expects': 'audit'})
    assert resp.status_code == 400
    assert 'impl' in resp.get_json()['error']


def test_impl_schema_field_errors(env):
    """impl：必填缺失/类型错/超长 → 400 逐字段带字段名。"""
    login(env.client)
    tid = newTask(env, expects='impl')
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, {})
    assert resp.status_code == 400
    fields = resp.get_json()['fields']
    assert 'summary' in fields and 'commit' in fields and 'tests' in fields
    bad = dict(IMPL_OK, commit='zzz', tests={'passed': -1, 'failed': 0, 'skipped': 0},
               gates={'structure': 'maybe'}, summary='x' * 301)
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, bad)
    fields = resp.get_json()['fields']
    assert 'commit' in fields and 'tests.passed' in fields
    assert 'gates.structure' in fields and 'summary' in fields
    assert env.repo.get(tid).get('result') is None      # 错的没落


def test_verify_schema_field_errors(env):
    """verify：verdict 必填且枚举、target 必填；证据超长报字段。"""
    login(env.client)
    tid = newTask(env, expects='verify')
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, {'verdict': 'pass'})
    fields = resp.get_json()['fields']
    assert 'verdict' not in fields and 'target' in fields
    resp = call(env, 'post', '/api/tasks/%s/result' % tid,
                dict(VERIFY_OK, verdict='ok', evidence=['y' * 301]))
    fields = resp.get_json()['fields']
    assert 'verdict' in fields and 'evidence[0]' in fields


def test_no_expects_freeform(env):
    """无 expects：summary 必填，data 自由但 ≤8KB。"""
    login(env.client)
    tid = newTask(env)
    assert call(env, 'post', '/api/tasks/%s/result' % tid,
                {'summary': '完了', 'data': {'anything': [1, 2]}}).status_code == 200
    resp = call(env, 'post', '/api/tasks/%s/result' % tid,
                {'summary': 'x', 'data': {'big': 'y' * 9000}})
    assert resp.status_code == 400 and 'data' in resp.get_json()['fields']


def test_submit_stores_snapshot_event_and_count(env):
    """合法提交 → 200：快照最新精简版 + result 事件进 jsonl + resultCount。"""
    login(env.client)
    tid = newTask(env, expects='impl')
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK)
    assert resp.status_code == 200
    assert resp.get_json()['resultCount'] == 1
    task = env.repo.get(tid)
    assert task['result']['commit'] == 'abc1234'
    assert task['result']['tests'] == {'passed': 12, 'failed': 0, 'skipped': 1}
    assert task['resultCount'] == 1
    events = resultEvents(env, tid)
    assert len(events) == 1 and events[0]['summary'] == '交付了 X'
    assert '实施结果' in events[0]['text']
    assert json.loads(logText(env, tid).splitlines()[-1])['type'] == 'result'
    detail = call(env, 'get', '/api/tasks/%s' % tid).get_json()['task']
    assert detail['result']['commit'] == 'abc1234' and detail['resultCount'] == 1


def test_resubmit_keeps_history_latest_wins(env):
    """同一任务多次提交：历史全留（事件），最新为准（快照）。"""
    login(env.client)
    tid = newTask(env, expects='impl')
    call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK)
    second = dict(IMPL_OK, summary='第二版', commit='def5678')
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, second)
    assert resp.get_json()['resultCount'] == 2
    assert env.repo.get(tid)['result']['commit'] == 'def5678'
    events = resultEvents(env, tid)
    assert [e['commit'] for e in events] == ['abc1234', 'def5678']


# ---------------------------------------------------------------- 鉴权

def test_session_token_only_own_task(env):
    """会话令牌：只收本任务；拿去交别的任务 401（守卫层拦）。"""
    login(env.client)
    tid, other = newTask(env, expects='impl'), newTask(env, expects='impl')
    token = task_result_mcp.issueToken(tid, 'impl')
    headers = {task_result_mcp.TOKEN_HEADER: token}
    assert call(env, 'post', '/api/tasks/%s/result' % tid,
                IMPL_OK, headers=headers).status_code == 200
    resp = call(env, 'post', '/api/tasks/%s/result' % other,
                IMPL_OK, headers=headers)
    assert resp.status_code == 401          # 令牌与路径任务不匹配
    resp = call(env, 'post', '/api/tasks/%s/result' % tid,
                IMPL_OK, headers={task_result_mcp.TOKEN_HEADER: 'bogus'})
    assert resp.status_code == 401


def test_token_dies_with_task_close(env):
    """任务关闭 → 会话令牌立即失效（closeTaskCore 吊销）。"""
    login(env.client)
    tid = newTask(env, expects='impl')
    token = task_result_mcp.issueToken(tid, 'impl')
    headers = {task_result_mcp.TOKEN_HEADER: token}
    assert call(env, 'post', '/api/tasks/%s/close' % tid).status_code == 200
    resp = call(env, 'post', '/api/tasks/%s/result' % tid,
                IMPL_OK, headers=headers)
    assert resp.status_code == 401


def test_token_never_in_events_or_logs(env):
    """令牌不出现在任务事件与 jsonl 流水里（规格红线）。"""
    login(env.client)
    tid = newTask(env, expects='impl')
    token = task_result_mcp.issueToken(tid, 'impl')
    call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK,
         headers={task_result_mcp.TOKEN_HEADER: token})
    assert token not in json.dumps(env.repo.get(tid).get('events') or [])
    assert token not in logText(env, tid)


def test_owner_lead_web_auth(env):
    """三种身份：属主令牌 / 主管令牌 / 网页会话可交；别的智能体 403。"""
    login(env.client)
    owner = newToken(env, 'owner-tok')
    other = newToken(env, 'other-tok')
    leadTok = newToken(env, 'lead-tok')
    leadId = newLead(env, '甲')
    bind(env, leadId, leadTok['id'])
    # 属主路径：owner 建的任务（无主管），owner 自己能交
    tid = newTask(env, expects='impl', token=owner['token'])
    assert env.repo.get(tid)['lead'] == ''
    assert call(env, 'post', '/api/tasks/%s/result' % tid,
                IMPL_OK, token=owner['token']).status_code == 200
    assert call(env, 'post', '/api/tasks/%s/result' % tid,
                IMPL_OK, token=other['token']).status_code == 403
    # 主管路径：主管令牌建的任务（带 lead），主管可交、别人不可
    lid = newTask(env, expects='impl', token=leadTok['token'])
    assert env.repo.get(lid)['lead'] == leadId
    assert call(env, 'post', '/api/tasks/%s/result' % lid,
                IMPL_OK, token=other['token']).status_code == 403
    assert call(env, 'post', '/api/tasks/%s/result' % lid,
                IMPL_OK, token=leadTok['token']).status_code == 200
    assert call(env, 'post', '/api/tasks/%s/result' % lid,
                IMPL_OK).status_code == 200          # 网页会话


def test_closed_task_rejects_submit(env):
    """已关闭/已取消的任务不再收结果（409）——属主令牌也不行。"""
    login(env.client)
    owner = newToken(env, 'owner-tok')
    tid = newTask(env, expects='impl', token=owner['token'])
    assert call(env, 'post', '/api/tasks/%s/close' % tid).status_code == 200
    resp = call(env, 'post', '/api/tasks/%s/result' % tid,
                IMPL_OK, token=owner['token'])
    assert resp.status_code == 409


# ---------------------------------------------------------------- 主管收件箱

def test_inbox_entries_and_judgment(env):
    """结果提交 → 主管收件箱 task_result；需判断时 task_judgment。"""
    login(env.client)
    leadTok = newToken(env, 'lead-tok')
    leadId = newLead(env, '甲')
    bind(env, leadId, leadTok['id'])
    tid = newTask(env, expects='impl', token=leadTok['token'])
    call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK)
    entries = inboxKinds(env, leadId)
    assert [e['kind'] for e in entries] == ['task_result']
    assert 'abc1234' in entries[0]['text'] and '交付了 X' in entries[0]['text']
    # questions 非空 → task_judgment（需判断）
    vid = newTask(env, expects='verify', token=leadTok['token'])
    call(env, 'post', '/api/tasks/%s/result' % vid,
         dict(VERIFY_OK, verdict='needs_judgment'))
    kinds = [e['kind'] for e in inboxKinds(env, leadId)]
    assert kinds == ['task_result', 'task_judgment']


def test_unbound_task_no_inbox(env):
    """没绑主管的任务提交 → 不收件箱条目（行为零变化）。"""
    login(env.client)
    tid = newTask(env, expects='impl')
    call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK)
    assert not os.path.isdir(os.path.join(str(env.tmp), 'state', 'lead_inbox'))
