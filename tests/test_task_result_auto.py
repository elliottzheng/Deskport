# -*- coding: utf-8 -*-
"""批次 G1：审批自动放行（submit_result 不弹审批）与自动追问/result_missing。

与 test_task_result_inject（注入两路）同属 runner 行为，按主题拆文件（门禁
400 行预算）。桩化：config/仓储指 tmp，不起真 kimi。
"""
import json
import queue
import threading

import pytest
from types import SimpleNamespace

import config as configMod
from modules import events as eventsMod
from modules import task_permission, task_result
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
SUBMIT_TITLE = 'mcp__deskport__submit_result'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD,
                                   'agentMcpUrl': 'http://127.0.0.1:6788'}),
                       encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path,
                           app=app, monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def newTask(env, expects='impl', leadToken=None):
    headers = {'X-Agent-Token': leadToken} if leadToken else {}
    resp = env.client.post('/api/tasks',
                           data=json.dumps({'prompt': '干活', 'expects': expects}),
                           content_type='application/json', headers=headers)
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def newLeadToken(env, name='甲'):
    """建主管并返回 (leadId, token)：令牌走无 cookie 客户端建任务才记 lead。"""
    tok = env.client.post('/api/settings/agent-tokens',
                          data=json.dumps({'name': 'LT'}),
                          content_type='application/json').get_json()
    leadId = env.client.post('/api/settings/leads',
                             data=json.dumps({'name': name}),
                             content_type='application/json').get_json()['lead']['id']
    env.client.post('/api/settings/leads/%s/tokens' % leadId,
                    data=json.dumps({'tokenId': tok['id']}),
                    content_type='application/json')
    return leadId, tok['token']


def userEvents(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
        return [e for e in task.get('events') or [] if e.get('type') == 'user']


# ---------------------------------------------------------------- 自动放行

def _agentDict():
    return {'queue': __import__('queue').Queue(), 'perm': None,
            'permDecision': None, 'permEvent': threading.Event()}


def permParams(callId, title):
    return {'sessionId': 'sess_x',
            'toolCall': {'title': title, 'toolCallId': callId},
            'options': [{'optionId': 'opt-allow', 'name': 'Allow once',
                         'kind': 'allow_once'},
                        {'optionId': 'opt-reject', 'name': 'Reject',
                         'kind': 'reject_once'}]}


def test_submit_result_auto_approved(env):
    """submit_result 的权限请求自动放行：不挂起、不留 pendingPermission。"""
    login(env.client)
    tid = newTask(env, 'impl')
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'running'
        env.repo.save()
    agent = _agentDict()
    value = task_permission.handleAcpPermission(
        task, agent, permParams('c-sub', SUBMIT_TITLE), 'req-sub')
    assert value == 'opt-allow'
    with env.repo.lock:
        assert task.get('pendingPermission') == ''
        autos = [e for e in task.get('events') or []
                 if e.get('type') == 'auto_approved']
    assert autos and autos[0]['rule'] == 'submit-result'
    assert autos[0]['name'] == SUBMIT_TITLE
    # 兼容别名同行为（存量测试引用 runner._handleAcpPermission）
    agent2 = _agentDict()
    assert runner._handleAcpPermission(
        task, agent2, permParams('c-sub2', SUBMIT_TITLE), 'req-sub2') == 'opt-allow'


# ---------------------------------------------------------------- 自动追问

class ResultClient:
    """一轮内就提交结果：不该被追问。"""

    def __init__(self, task):
        self.task = task

    def prompt(self, text, timeout=None):
        task_result.submit(self.task, {'summary': '交了', 'commit': 'abc1234',
                                       'tests': {'passed': 1, 'failed': 0,
                                                 'skipped': 0}}, via='mcp')
        return {'stopReason': 'end_turn'}


class SilentClient:
    def prompt(self, text, timeout=None):
        return {'stopReason': 'end_turn'}


def test_nudge_once_then_result_missing(env):
    """没交结果：轮末追一次（固定文案）；再一轮仍没有 → 收件箱 result_missing。"""
    login(env.client)
    leadTok = env.client.post('/api/settings/agent-tokens',
                              data=json.dumps({'name': 'LT'}),
                              content_type='application/json').get_json()
    leadId = env.client.post('/api/settings/leads',
                             data=json.dumps({'name': '甲'}),
                             content_type='application/json').get_json()['lead']['id']
    env.client.post('/api/settings/leads/%s/tokens' % leadId,
                    data=json.dumps({'tokenId': leadTok['id']}),
                    content_type='application/json')
    fresh = env.app.test_client()      # 无 cookie：走令牌通道才会记 task.lead
    tid = fresh.post('/api/tasks',
                     data=json.dumps({'prompt': '干活', 'expects': 'impl'}),
                     content_type='application/json',
                     headers={'X-Agent-Token': leadTok['token']}).get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
    runner._promptOnce(task, SilentClient(), '第一轮', False)
    nudges = [e for e in userEvents(env, tid) if e.get('by') == task_result.NUDGE_BY]
    assert len(nudges) == 1
    assert 'submit_result' in nudges[0]['text'] and 'expects=impl' in nudges[0]['text']
    assert env.repo.get(tid).get('resultNudged') is True
    # 第二轮（=追发那一轮）仍没有 → result_missing 投主管
    runner._promptOnce(task, SilentClient(), '第二轮', False)
    assert len([e for e in userEvents(env, tid)
                if e.get('by') == task_result.NUDGE_BY]) == 1   # 只追一次
    path = '%s/state/lead_inbox/%s.jsonl' % (env.tmp, leadId)
    entries = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
    kinds = [e['kind'] for e in entries]
    assert 'result_missing' in kinds
    missing = [e for e in entries if e['kind'] == 'result_missing']
    assert len(missing) == 1 and '未提交结构化结果' in missing[0]['text']


def test_no_nudge_when_result_present(env):
    """一轮内交了结果：不追问、不记 result_missing。"""
    login(env.client)
    tid = newTask(env, 'impl')
    with env.repo.lock:
        task = env.repo.get(tid)
    runner._promptOnce(task, ResultClient(task), '干活', False)
    assert [e for e in userEvents(env, tid)
            if e.get('by') == task_result.NUDGE_BY] == []
    assert env.repo.get(tid).get('resultNudged') is None
    runner._promptOnce(task, SilentClient(), '再干', False)
    assert [e for e in userEvents(env, tid)
            if e.get('by') == task_result.NUDGE_BY] == []


def test_no_nudge_without_expects(env):
    """没有 expects 的任务：轮末什么都不追加。"""
    login(env.client)
    tid = newTask(env, expects=None)
    with env.repo.lock:
        task = env.repo.get(tid)
    before = len(userEvents(env, tid))
    runner._promptOnce(task, SilentClient(), '干活', False)
    assert len(userEvents(env, tid)) == before
    assert env.repo.get(tid).get('resultNudged') is None

