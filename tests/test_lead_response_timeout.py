# -*- coding: utf-8 -*-
"""批次 L L1：主管反应超时提醒——可注入时钟覆盖四个面。

未读超时提醒 / 已读不提醒 / 同一主管每阈值最多一条 / 托管主管"已投递但
没反应"同样提醒。时钟经 sweepOnce(now=...) 注入（sweeper 原用墙上时钟），
条目年龄由 append 时的真实时间与注入的 now 之差造出。
"""
import json
import queue
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import leads_inbox, leads_store
from modules import tasks_api
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


def downEntries(env, leadId):
    entries, _last = leads_inbox.entriesAfter(leadId, 0, 'down')
    return entries


def test_unread_entries_notice_after_timeout(env):
    """下属消息超时没投递也没读 → down 一条 notice（指挥台待办可见）。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    tid = newTask(env, token=tok['token'])
    base = time.time()
    leads_inbox.append(L, 'up', 'report', '任务 %s 本轮结束' % tid[:8],
                            {'task': tid})

    env.sweep(now=base + 599)             # 差一秒：不提醒
    assert downEntries(env, L) == []
    env.sweep(now=base + 601)             # 超阈值：提醒
    entries = downEntries(env, L)
    assert len(entries) == 1
    notice = entries[0]
    assert notice['kind'] == 'notice' and notice['dir'] == 'down'
    assert '主管 甲' in notice['text'] and '超过 10 分钟未处理' in notice['text']
    assert tid[:8] in notice['text'] and 'report' in notice['text']


def test_read_entries_no_notice(env):
    """外部主管读过了（readSeq 推进）→ 不提醒。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    tid = newTask(env, token=tok['token'])
    base = time.time()
    leads_inbox.append(L, 'up', 'report', '任务 %s 本轮结束' % tid[:8],
                            {'task': tid})
    assert leads_store.setCursor(L, readSeq=1)[1] == ''

    env.sweep(now=base + 3600)
    assert downEntries(env, L) == []


def test_notice_throttled_per_window(env):
    """同一主管每 leadResponseSec 最多一条。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    newTask(env, token=tok['token'])
    base = time.time()
    leads_inbox.append(L, 'up', 'report', '甲任务结束')

    env.sweep(now=base + 601)
    assert len(downEntries(env, L)) == 1
    env.sweep(now=base + 900)             # 窗口内：不重复
    assert len(downEntries(env, L)) == 1
    env.sweep(now=base + 1300)            # 过了一个阈值：可以再来
    assert len(downEntries(env, L)) == 2


def test_hosted_delivered_but_unreacted_notices(env):
    """托管主管：条目已投递、唤醒却堵在队列里没人消费 → 同样提醒。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    tid = newTask(env, '托管会话')
    base = time.time()
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'running'    # 轮中：投递走持久化队列
        env.repo.save()
    agent = {'queue': queue.Queue(), 'client': None, 'perm': None,
             'permDecision': None, 'permEvent': __import__('threading').Event(),
             'ready': True}
    env.repo.setAgent(tid, agent)
    assert call(env, 'post', '/api/settings/leads/%s/task' % L,
                {'taskId': tid}).status_code == 200
    sub = newTask(env, '下属任务', token=tok['token'])
    leads_inbox.append(L, 'up', 'report', '任务 %s 本轮结束' % sub[:8],
                            {'task': sub})
    env.monkeypatch.setattr(configMod, 'getLeadWakeMergeSec', lambda: 0)
    env.sweep(now=base)
    assert leads_store.get(L)['inboxSeq'] == 1
    with env.repo.lock:
        host = env.repo.get(tid)
        assert [m.get('by') for m in host['messageQueue']] == [leads_inbox.INBOX_BY]
        # 轮结束了但唤醒没人消费（被挂起审批挡住 drain 闸门的形状）
        host['status'] = 'waiting'
        host['lastActiveAt'] = base
        env.repo.save()

    env.sweep(now=base + 601)
    entries = downEntries(env, L)
    assert len(entries) == 1 and entries[0]['kind'] == 'notice'
    assert '超过 10 分钟未处理' in entries[0]['text'] and sub[:8] in entries[0]['text']


def test_hosted_reacted_no_notice(env):
    """托管主管反应过了（唤醒已被消费、队列里没有堵着的唤醒）→ 不提醒。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    tid = newTask(env, '托管会话')
    base = time.time()
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'running'
        env.repo.save()
    agent = {'queue': queue.Queue(), 'client': None, 'perm': None,
             'permDecision': None, 'permEvent': __import__('threading').Event(),
             'ready': True}
    env.repo.setAgent(tid, agent)
    assert call(env, 'post', '/api/settings/leads/%s/task' % L,
                {'taskId': tid}).status_code == 200
    sub = newTask(env, '下属任务', token=tok['token'])
    leads_inbox.append(L, 'up', 'report', '任务 %s 本轮结束' % sub[:8],
                            {'task': sub})
    env.monkeypatch.setattr(configMod, 'getLeadWakeMergeSec', lambda: 0)
    env.sweep(now=base)
    assert leads_store.get(L)['inboxSeq'] == 1
    # 主管跑了一轮把唤醒消费掉：队列清空、有后续事件、回到 waiting
    with env.repo.lock:
        host = env.repo.get(tid)
        host['messageQueue'] = []
        host['status'] = 'waiting'
        env.repo.save()
    env.repo.append_event(env.repo.get(tid), 'user',
                          text='【收件箱】1 条新消息（seq 1–1）',
                          by=leads_inbox.INBOX_BY)
    env.repo.append_event(env.repo.get(tid), 'status', text='这轮结束，等你继续')

    env.sweep(now=base + 3600)
    assert downEntries(env, L) == []


def test_disabled_by_config(env):
    """leadResponseSec=0：关闭，不提醒。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % L,
                {'tokenId': tok['id']}).status_code == 200
    newTask(env, token=tok['token'])
    leads_inbox.append(L, 'up', 'report', '甲任务结束')
    env.monkeypatch.setattr(configMod, 'getLeadResponseSec', lambda: 0)

    env.sweep(now=time.time() + 3600)
    assert downEntries(env, L) == []
