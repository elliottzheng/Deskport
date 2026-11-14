# -*- coding: utf-8 -*-
"""批次 P3 收件箱：存储与触发点（例子 2/9–12/15/16/18）。

从 test_leads_inbox.py 按主题拆出（批次 P3b，断言逐字未改）：runner 触发点
落的条目（轮末 report / 空转·静默 failure / 挂起审批 notice / 线程退出 /
用户关闭）、存储上限与并发写、web 重启恢复不误报。全部桩化：config.statePath
与任务仓储指到 tmp，唤醒不起真 kimi；后台扫掠线程（首个 create_app 时起的
进程级线程）在夹具里换成空操作——否则 5s 一跳的后台 tick 会和断言竞速。
"""
import json
import os
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import leads_inbox, leads_store
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'


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
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    realSweep = leads_inbox.sweepOnce
    monkeypatch.setattr(leads_inbox, 'sweepOnce', lambda: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           audit=audit, repo=repo, tmp=tmp_path, app=app,
                           sweep=realSweep, monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None, token=None):
    """管理面走会话态 client；给了 token 走 fresh（无 Cookie）令牌通道。"""
    client = env.fresh if token else env.client
    kw = {'headers': {'X-Agent-Token': token}} if token else {}
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


def newTask(env, prompt='做件事', token=None):
    """建任务（令牌通道记 task.lead；不传 token 走网页会话，lead 为空）。"""
    resp = call(env, 'post', '/api/tasks', {'prompt': prompt}, token=token)
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def setRunning(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'running'
        env.repo.save()
    return task


def inboxEntries(env, leadId, after=0, direction='up'):
    entries, lastSeq = leads_inbox.entriesAfter(leadId, after, direction)
    return entries, lastSeq


def fileLines(env, leadId):
    path = os.path.join(str(env.tmp), 'state', 'lead_inbox',
                        '%s.jsonl' % leadId)
    if not os.path.isfile(path):
        return []
    return [l for l in open(path, encoding='utf-8').readlines() if l.strip()]


# ---------------------------------------------------------------- 桩客户端

class ReportingClient:
    """调一个工具、落一句终稿：正常轮。"""

    def __init__(self, task):
        self.task = task

    def prompt(self, text, timeout=None):
        runner._handleAcpUpdate(self.task, {'sessionUpdate': 'tool_call',
                                            'toolCallId': 'c-1', 'title': 'Bash',
                                            'kind': 'execute',
                                            'rawInput': {'command': 'ls'}})
        runner._appendAssistantText(self.task, '干完了，报告在此')
        return {'stopReason': 'end_turn'}


class SilentClient:
    """0 工具 0 文本：空转轮（早夭形状）。"""

    def prompt(self, text, timeout=None):
        return {'stopReason': 'end_turn'}


class BusyClient:
    """调了工具但一句终稿都没落：静默轮。"""

    def __init__(self, task):
        self.task = task

    def prompt(self, text, timeout=None):
        runner._handleAcpUpdate(self.task, {'sessionUpdate': 'tool_call',
                                            'toolCallId': 'c-2', 'title': 'Bash',
                                            'kind': 'execute',
                                            'rawInput': {'command': 'ls'}})
        return {'stopReason': 'end_turn'}


class ExplodingClient:
    """prompt 抛异常：会话线程失败退出。"""
    stderr_lines = []

    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=0):
        pass

    def start(self, session_id=None):
        return 'sess-boom'

    def prompt(self, text, timeout=None):
        raise RuntimeError('kimi 进程炸了\n第二行细节')

    def close(self):
        pass


def permParams(callId, title='Bash'):
    """真实形状：request_permission 不带 kind、不带 rawInput。"""
    return {'sessionId': 'sess_x',
            'toolCall': {'title': title, 'toolCallId': callId},
            'options': [{'optionId': 'opt-allow', 'name': 'Allow once',
                         'kind': 'allow_once'},
                        {'optionId': 'opt-reject', 'name': 'Reject',
                         'kind': 'reject_once'}]}


# ---------------------------------------------------------------- 触发点（例 2）

def test_unbound_task_writes_nothing(env):
    """例 2：没绑主管的任务跑完 → 不产生任何收件箱条目（行为零变化）。"""
    login(env.client)
    tok = newToken(env, 'T2')
    tid = newTask(env, token=tok['token'])
    assert env.repo.get(tid).get('lead') == ''
    task = setRunning(env, tid)
    runner._promptOnce(task, ReportingClient(task), 'x', False)
    assert fileLines(env, 'ld_whatever') == []
    assert not os.path.isdir(os.path.join(str(env.tmp), 'state', 'lead_inbox'))


# ---------------------------------------------------------------- 例 9–12（触发点）

def test_permission_suspend_appends_notice(env):
    """例 9：挂起审批 → 箱内一条 notice，含工具名与 requestId。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    task = setRunning(env, tid)
    task['policy'] = {'timeoutSec': 1}     # 挂起 1s 超时拒绝，不必真等审批方
    agent = {'queue': queue.Queue(), 'perm': None,
             'permDecision': None,
             'permEvent': threading.Event()}
    value = runner._handleAcpPermission(task, agent, permParams('call-9'),
                                        'req-9')
    assert value is None                  # 超时未批 → None（拒绝）
    entries, _ = inboxEntries(env, L)
    assert len(entries) == 1
    entry = entries[0]
    assert entry['kind'] == 'notice'
    assert tid[:8] in entry['text'] and 'Bash' in entry['text']
    assert 'requestId req-9' in entry['text'] and '纯提醒' in entry['text']


def test_empty_turn_appends_failure(env):
    """例 10：空转轮（0 工具 0 文本）→ failure；同轮轮末 report 也落（规格
    两条触发点各自满足，互不抵消）。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    task = setRunning(env, tid)
    runner._promptOnce(task, SilentClient(), '继续干完', False)

    entries, _ = inboxEntries(env, L)
    assert [e['kind'] for e in entries] == ['failure', 'report']
    assert '空转（0 工具 0 文本）' in entries[0]['text']
    assert tid[:8] in entries[0]['text']


def test_silent_turn_appends_failure(env):
    """例 10 补：静默轮（有工具无终稿）→ failure。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    task = setRunning(env, tid)
    runner._promptOnce(task, BusyClient(task), '继续', False)

    entries, _ = inboxEntries(env, L)
    assert [e['kind'] for e in entries] == ['failure', 'report']
    assert '静默（有工具无终稿）' in entries[0]['text']


def test_thread_exit_appends_failure(env):
    """例 11：会话线程异常退出（failed）→ failure，含 error 首句。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    task = setRunning(env, tid)
    th = threading.Thread(target=runner._sessionThread, args=(task, []),
                          kwargs={'clientFactory': ExplodingClient},
                          daemon=True)
    th.start()
    th.join(timeout=10)
    assert env.repo.get(tid)['status'] == 'failed'

    entries, _ = inboxEntries(env, L)
    assert len(entries) == 1
    assert entries[0]['kind'] == 'failure'
    assert 'failed' in entries[0]['text']
    assert 'kimi 进程炸了' in entries[0]['text']
    assert '第二行细节' not in entries[0]['text']   # 只取首句


def test_user_close_appends_failure(env):
    """例 12：网页会话关掉下属任务 → 一条 failure（被关闭）；重复关闭不重复报。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'waiting'
        env.repo.save()
    resp = env.client.post('/api/tasks/%s/close' % tid)
    assert resp.status_code == 200 and resp.get_json()['status'] == 'closed'

    entries, _ = inboxEntries(env, L)
    assert len(entries) == 1
    assert entries[0]['kind'] == 'failure'
    assert '被关闭' in entries[0]['text']
    assert env.client.post('/api/tasks/%s/close' % tid).status_code == 200
    assert len(inboxEntries(env, L)[0]) == 1


# ---------------------------------------------------------------- 例 15–18（存储）

def test_cap_500_truncates_head(env):
    """例 15：连发 501 条 → 只剩最后 500 条，seq 连续不断档。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    for i in range(501):
        leads_inbox.append(L, 'up', 'notice', '消息 %d' % i)
    entries, lastSeq = inboxEntries(env, L)
    assert len(entries) == 500 and lastSeq == 501
    assert [e['seq'] for e in entries] == list(range(2, 502))
    assert entries[0]['text'] == '消息 1' and entries[-1]['text'] == '消息 500'
    assert len(fileLines(env, L)) == 500


def test_concurrent_appends_stay_consistent(env):
    """例 18：10 个线程各 append 60 条 → 最终 500 条、seq 连续、每行合法 JSON。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200

    def worker(n):
        for i in range(60):
            leads_inbox.append(L, 'up', 'notice', '线程%d-%d' % (n, i))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    entries, lastSeq = inboxEntries(env, L)
    assert len(entries) == 500 and lastSeq == 600
    assert [e['seq'] for e in entries] == list(range(101, 601))
    for line in fileLines(env, L):
        assert isinstance(json.loads(line), dict)


def test_restart_paused_no_failure_entry(env):
    """例 16：web 重启（load 恢复）把在飞下属转 paused，不产生 failure 条目，
    游标不丢。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'running'
        task['sessionId'] = 'sess-live'
        env.repo.save()
    leads_store.setCursor(L, inboxSeq=2, readSeq=1)

    # 模拟重启：新仓储实例 load()（恢复逻辑在这）
    repo2 = TaskRepository(str(env.tmp / 'tasks.json'),
                           str(env.tmp / 'logs'))
    repo2.load()
    # 有 sessionId → paused（可续聊）；error 只在 interrupted（不可续）时写
    assert repo2.get(tid)['status'] == 'paused'
    assert repo2.get(tid)['resume'] is True

    assert inboxEntries(env, L)[0] == []          # 不刷 failure
    lead = leads_store.get(L)
    assert lead['inboxSeq'] == 2 and lead['readSeq'] == 1
