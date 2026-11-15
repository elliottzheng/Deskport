# -*- coding: utf-8 -*-
"""批次 L L2：下属提问直达主管 + answer 答复 + 队首消息当答复。

全部桩化：不起真 kimi——下属任务的审批挂起由 runner._handleAcpPermission
真跑（后台线程），answer/消息走真端点，投递由 _drainQueuedMessages 模拟
轮末消费。托管主管的唤醒断言它的假 agent 队列里收到了摘要。
"""
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import leads_inbox, leads_store
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'

QUESTION_PARAMS = {
    'toolCall': {'title': 'AskUserQuestion', 'toolCallId': 'c-q1',
                 'rawInput': {'questions': [
                     {'question': '用哪个方案上线？', 'header': '方案',
                      'options': [{'label': '甲方案'}, {'label': '乙方案'}]}]}},
    'options': [{'optionId': 'allow-1', 'name': '允许', 'kind': 'allow_once'},
                {'optionId': 'reject-1', 'name': '拒绝', 'kind': 'reject_once'}],
}


class FakeClient:
    """记 cancel 与 prompt 的客户端替身（node/stdio 同接口）。"""

    def __init__(self):
        self.cancels = 0
        self.prompts = []

    def cancel(self):
        self.cancels += 1

    def prompt(self, text, timeout=None):
        self.prompts.append(text)
        return {'stopReason': 'end_turn'}


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
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    # sweepOnce 打桩（L 返工）：sweeper 的 question 旁路 5s 内也会投递摘要，
    # 不打桩「立即唤醒」的断言会被它带绿——拆掉 wakeNow 测试仍旧全过的假象。
    # 要真 sweeper 的用例调 env.sweep()。在 create_app 前打桩，bindApp 拉起的
    # 扫掠线程从此空转
    realSweep = leads_inbox.sweepOnce
    monkeypatch.setattr(leads_inbox, 'sweepOnce', lambda: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302
    return SimpleNamespace(client=client, fresh=app.test_client(), repo=repo,
                           tmp=tmp_path, sweep=realSweep,
                           monkeypatch=monkeypatch)


def call(env, method, path, body=None, token=None):
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


def newTask(env, prompt='做件事', token=None):
    resp = call(env, 'post', '/api/tasks', {'prompt': prompt}, token=token)
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def seedAgent(env, tid):
    agent = {'queue': queue.Queue(), 'client': None, 'perm': None,
             'permDecision': None, 'permEvent': threading.Event(),
             'ready': True}
    env.repo.setAgent(tid, agent)
    return agent


def setRunning(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
        task['status'] = 'running'
        env.repo.save()
    return task


def startPermission(env, tid, agent, requestId='req-1', params=None):
    """后台真跑审批挂起（reader 线程的角色）。"""
    thread = threading.Thread(
        target=runner._handleAcpPermission,
        args=(env.repo.get(tid), agent, params or QUESTION_PARAMS, requestId),
        daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if env.repo.get(tid).get('pendingPermission'):
            return thread
        time.sleep(0.02)
    raise AssertionError('permission did not suspend')


def awaitCursor(env, leadId, key, value, timeout=5):
    """等主管记录的游标到位（投递与落游标之间有竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (leads_store.get(leadId) or {}).get(key) == value:
            return
        time.sleep(0.05)
    raise AssertionError('lead %s %s != %r' % (leadId, key, value))


def awaitEntry(env, leadId, kind, timeout=5):
    """等收件箱出现指定 kind 的条目（挂起标记先于 emit 落卡，有竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
        hit = [e for e in entries if e.get('kind') == kind]
        if hit:
            return hit[0]
        time.sleep(0.05)
    raise AssertionError('no %s entry in lead inbox' % kind)


def _hostedLead(env, name='甲', tokenName='T1'):
    tok = newToken(env, tokenName)
    L = newLead(env, name)
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    H = newTask(env, '托管会话')
    with env.repo.lock:
        env.repo.get(H)['status'] = 'waiting'
        env.repo.save()
    seedAgent(env, H)
    assert call(env, 'post', '/api/settings/leads/%s/task' % L,
                {'taskId': H}).status_code == 200
    return L, H, tok


def _subordinate(env, L, tok):
    """下属任务：lead=L、running、带假客户端与 agent。"""
    D = newTask(env, '下属任务', token=tok['token'])
    task = setRunning(env, D)
    client = FakeClient()
    agent = seedAgent(env, D)
    agent['client'] = client
    return D, task, agent, client


def test_question_enters_inbox_and_wakes_hosted_lead(env):
    """提问作为 kind='question' 进主管收件箱（问题+选项+requestId），
    托管主管被立即唤醒（不等合并窗口）。"""
    L, H, tok = _hostedLead(env)
    D, _task, agent, _client = _subordinate(env, L, tok)
    hostAgent = env.repo.agentOf(H)

    thread = startPermission(env, D, agent)
    try:
        entry = awaitEntry(env, L, 'question')
        assert '用哪个方案上线？' in entry['text']
        assert '甲方案' in entry['text'] and '乙方案' in entry['text']
        assert entry['ref'].get('requestId') == 'req-1'
        assert entry['ref'].get('task') == D
        # 立即唤醒：托管主管的队列里已有摘要（没调 sweep）
        item = hostAgent['queue'].get(timeout=5)
        assert item['type'] == 'message' and item['by'] == '收件箱'
        assert '用哪个方案上线？' in item['text']
        awaitCursor(env, L, 'inboxSeq', 1)
    finally:
        runner._denyPendingPermission(agent)
        thread.join(timeout=5)
    assert env.repo.get(D)['pendingPermission'] == ''


def test_answer_approves_and_steers_reply(env):
    """answer：审批按"允许"放行，答复以 steer 立即投递，任务继续跑。"""
    L, _H, tok = _hostedLead(env)
    D, _task, agent, client = _subordinate(env, L, tok)
    thread = startPermission(env, D, agent)

    resp = call(env, 'post', '/api/tasks/%s/answer' % D,
                {'requestId': 'req-1', 'text': '用甲方案，先灰度'})
    assert resp.status_code == 202, resp.get_json()
    body = resp.get_json()
    assert body.get('answered') is True and body.get('steered') is True
    thread.join(timeout=5)
    assert env.repo.get(D)['pendingPermission'] == ''
    results = [e for e in env.repo.get(D)['events']
               if e.get('type') == 'permission_result']
    assert results and results[-1]['status'] == 'approved'
    assert client.cancels == 1                # steer：打断当前轮
    mq = env.repo.get(D)['messageQueue']
    assert len(mq) == 1 and mq[0]['steer'] is True \
        and mq[0]['by'] == '主管答复'

    # 轮末 drain：答复作为下一轮 prompt 真的到执行方手里
    runner._drainQueuedMessages(env.repo.get(D), client)
    assert client.prompts == ['用甲方案，先灰度']
    users = [e for e in env.repo.get(D)['events'] if e.get('type') == 'user']
    assert users[-1]['text'] == '用甲方案，先灰度'
    assert users[-1]['steer'] is True and users[-1]['by'] == '主管答复'


def test_answer_rejects_foreign_and_unbound_tokens(env):
    """令牌通道：只有 task.lead 属于自己的令牌能答复；没绑主管/绑别人的 403。"""
    L, _H, tok = _hostedLead(env)
    D, _task, agent, _client = _subordinate(env, L, tok)
    other = newToken(env, 'T2')               # 没绑主管
    thread = startPermission(env, D, agent)
    try:
        resp = call(env, 'post', '/api/tasks/%s/answer' % D,
                    {'requestId': 'req-1', 'text': 'x'}, token=other['token'])
        assert resp.status_code == 403, resp.get_json()
        # 自己的令牌可以
        resp = call(env, 'post', '/api/tasks/%s/answer' % D,
                    {'requestId': 'req-1', 'text': '用甲方案'},
                    token=tok['token'])
        assert resp.status_code == 202, resp.get_json()
        assert resp.get_json().get('answered') is True
    finally:
        thread.join(timeout=5)


def test_answer_rejects_stale_request_id(env):
    """requestId 对不上当前挂起的审批 → 409（不误放行别人的审批）。"""
    L, _H, tok = _hostedLead(env)
    D, _task, agent, _client = _subordinate(env, L, tok)
    thread = startPermission(env, D, agent)
    try:
        resp = call(env, 'post', '/api/tasks/%s/answer' % D,
                    {'requestId': 'req-404', 'text': 'x'})
        assert resp.status_code == 409, resp.get_json()
        resp = call(env, 'post', '/api/tasks/%s/answer' % D,
                    {'requestId': 'req-1', 'text': '  '})
        assert resp.status_code == 400
    finally:
        runner._denyPendingPermission(agent)
        thread.join(timeout=5)


def test_queued_message_auto_answers_question(env):
    """挂起的是提问时，队列里的消息被当作答复：放行 + 打断 + 轮末投递，
    不再干等到审批超时。"""
    L, _H, tok = _hostedLead(env)
    D, _task, agent, client = _subordinate(env, L, tok)
    thread = startPermission(env, D, agent)

    resp = call(env, 'post', '/api/tasks/%s/message' % D, {'text': '选乙方案'})
    assert resp.status_code == 202, resp.get_json()   # running：入队
    deadline = time.time() + 5
    while time.time() < deadline:
        results = [e for e in env.repo.get(D)['events']
                   if e.get('type') == 'permission_result']
        if results:
            break
        time.sleep(0.05)
    thread.join(timeout=5)
    assert results and results[-1]['status'] == 'approved'
    assert client.cancels == 1
    assert env.repo.get(D)['pendingPermission'] == ''

    runner._drainQueuedMessages(env.repo.get(D), client)
    assert client.prompts == ['选乙方案']
    users = [e for e in env.repo.get(D)['events'] if e.get('type') == 'user']
    assert users[-1]['text'] == '选乙方案'


def test_plain_permission_still_escalates_not_question(env):
    """普通审批（非提问）不当提问：收件箱是 escalation（批次 A 审批先上交
    主管，合并 main 后替换同位置的 notice）、没有 question 条目、队列消息
    不当答复（仍等审批落定）。"""
    L, _H, tok = _hostedLead(env)
    D, _task, agent, client = _subordinate(env, L, tok)
    params = {'toolCall': {'title': 'Bash', 'toolCallId': 'c-b1',
                           'rawInput': {'command': 'ls'}},
              'options': [{'optionId': 'allow-1', 'name': '允许',
                           'kind': 'allow_once'},
                          {'optionId': 'reject-1', 'name': '拒绝',
                           'kind': 'reject_once'}]}
    thread = startPermission(env, D, agent, params=params)
    try:
        entry = awaitEntry(env, L, 'escalation')
        assert '请求审批' in entry['text']
        assert not [e for e in leads_inbox.entriesAfter(L, 0, 'up')[0]
                    if e.get('kind') == 'question']
        resp = call(env, 'post', '/api/tasks/%s/message' % D, {'text': '先等等'})
        assert resp.status_code == 202
        time.sleep(1.2)                       # 给等待循环几个跳：不能自动放行
        assert env.repo.get(D).get('pendingPermission') == 'req-1'
        assert client.cancels == 0
        assert env.repo.get(D)['messageQueue'][0]['text'] == '先等等'
    finally:
        runner._denyPendingPermission(agent)
        thread.join(timeout=5)
