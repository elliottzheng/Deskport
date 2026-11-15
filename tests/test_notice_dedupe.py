# -*- coding: utf-8 -*-
"""批次 N1：提醒去重与条件解除即收回（跨 lead_notices / leads_inbox 读接口 /
leads_escalation 的三处根因修复，指挥台代码零改动）。

一事一条（同族只留最新、数字最新）/ 条件解除即收回（ack、审批落定后下一轮
sweeper 收回，不需要用户点）/ 历史补标（终态或 pendingPermission 已变的旧
escalation → resolvedRequests，幂等）+ 指挥台接口快照（12 条合成重复提醒 →
每主管 1 条或 0 条）。收件箱只追加不改写：瘦身全在读侧。

返工增收的两条：markNoticeCleared 状态没变不写盘（验方建议：别每 5s 重写
leads.json）；CAP 逐出与收件箱截头错位的一次性幽灵提醒（补标前把超限文件
截回保留窗口）。全部桩化：config.statePath 与任务仓储指到 tmp，后台扫掠线程
在夹具里换成空操作、要真 sweeper 的用例调 env.sweep（照 test_leads_escalation）。
"""
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import lead_inbox_store, leads_escalation, leads_inbox, leads_store
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

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
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **kw: None)
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
                           repo=repo, tmp=tmp_path, app=app, sweep=realSweep,
                           monkeypatch=monkeypatch)


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


def newLead(env, name='甲'):
    resp = call(env, 'post', '/api/settings/leads', {'name': name})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['lead']['id']


def bind(env, leadId, tokenId):
    return call(env, 'post', '/api/settings/leads/%s/tokens' % leadId,
                {'tokenId': tokenId})


def newTask(env, prompt='做件事', token=None):
    resp = call(env, 'post', '/api/tasks', {'prompt': prompt}, token=token)
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def hostedViaApi(env, name='甲', duty='托管会话'):
    """真实托管形状：POST /api/settings/leads/hosted 建出来的任务 lead 为空。"""
    resp = call(env, 'post', '/api/settings/leads/hosted',
                {'name': name, 'duty': duty, 'cwd': ''})
    assert resp.status_code == 201, resp.get_json()
    out = resp.get_json()
    return str(out['lead']['id']), str(out['taskId'])


def rawDown(env, leadId):
    """未经过滤的 down 条目（N1：收件箱只追加不改写）。"""
    return [e for e in lead_inbox_store.readAll(leadId)
            if e.get('dir') == 'down']


def seedEscalation(env, leadId, taskId, requestId):
    """手工造一条 escalation（批次 L 之前的形状：从没写 resolvedRequests）。"""
    leads_inbox.append(leadId, 'up', 'escalation',
                       '任务 %s 请求审批：Bash（requestId %s）'
                       % (str(taskId)[:8], requestId),
                       {'task': str(taskId), 'requestId': str(requestId)})


def overview(env):
    resp = call(env, 'get', '/api/command/overview')
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


# ---------------------------------------------------------------- 一事一条（快照）

def test_duplicate_notices_collapse_in_overview(env):
    """N1 一事一条：12 条重复提醒的历史数据（合成）→ overview 里每主管只剩
    1 条或 0 条，数字为最新。瘦身在 leads_inbox 读接口（entriesAfter 的 down
    分支），指挥台代码零改动；收件箱只追加不改写，原始 12 条都在。"""
    login(env.client)
    spam = newLead(env, '刷屏主管')
    for i in range(12):
        leads_inbox.append(spam, 'down', 'notice',
                           '主管 刷屏主管 有 %d 条下属消息超过 10 分钟未处理'
                           % (i + 1), {'lead': spam})
    quiet = newLead(env, '已解除主管')
    for i in range(12):
        leads_inbox.append(quiet, 'down', 'notice',
                           '主管 已解除主管 有 %d 条下属消息超过 10 分钟未处理'
                           % (i + 1), {'lead': quiet})
    # 条件早已解除：sweeper 收过（noticeCleared 记在主管记录上）
    leads_store.markNoticeCleared(quiet, 'response', 12)

    data = overview(env)
    byName = {l['name']: l for l in data['leads']}
    rows = [p for p in data['pending'] if p['kind'] == 'notice']
    spamRows = [p for p in rows if p['leadId'] == spam]
    quietRows = [p for p in rows if p['leadId'] == quiet]
    assert len(spamRows) == 1 and len(quietRows) == 0
    assert spamRows[0]['seq'] == 12 and '12 条下属消息' in spamRows[0]['detail']
    # 未读数 / 卡片徽标 / 「最近一条」只算没被收走的
    assert byName['刷屏主管']['unreadReports'] == 1
    assert byName['刷屏主管']['state'] == 'waiting_you'
    assert byName['刷屏主管']['lastReport']['seq'] == 12
    assert byName['已解除主管']['unreadReports'] == 0
    assert byName['已解除主管']['state'] == 'asleep'
    assert byName['已解除主管']['lastReport'] is None
    assert len(rawDown(env, spam)) == 12       # 只追加不改写


# ---------------------------------------------------------------- 历史补标

def test_backfill_marks_settled_escalations(env):
    """N1 历史补标：任务已关闭 / pendingPermission 已变的旧 escalation 补标
    （by='backfill'），仍挂着的不补——线上 174 条"未落定"实际一条都不挂。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    closed = newTask(env, token=tok['token'])
    changed = newTask(env, token=tok['token'])
    hanging = newTask(env, token=tok['token'])
    seedEscalation(env, L, closed, 'rq-closed')
    seedEscalation(env, L, changed, 'rq-changed')
    seedEscalation(env, L, hanging, 'rq-hang')
    with env.repo.lock:
        env.repo.get(closed)['status'] = 'closed'
        env.repo.get(changed)['pendingPermission'] = 'rq-other'
        env.repo.get(hanging)['pendingPermission'] = 'rq-hang'
        env.repo.get(hanging)['status'] = 'waiting'
        env.repo.save()

    assert leads_escalation.backfillResolved() == 2
    resolved = leads_store.get(L).get('resolvedRequests') or {}
    assert set(resolved) == {'rq-closed', 'rq-changed'}
    assert all(v['by'] == 'backfill' for v in resolved.values())
    # 仍挂着的不补；读侧也不把它当已落定
    entry = [e for e in leads_inbox.entriesAfter(L, 0, 'up')[0]
             if e['ref']['requestId'] == 'rq-hang'][0]
    assert leads_inbox.entryResolved(leads_store.get(L), entry) is False


def test_backfill_idempotent(env):
    """重复运行幂等：第二遍一条都不补，resolvedRequests 逐字不变。"""
    login(env.client)
    tok = newToken(env, 'T1')
    L = newLead(env, '甲')
    assert bind(env, L, tok['id']).status_code == 200
    tid = newTask(env, token=tok['token'])
    seedEscalation(env, L, tid, 'rq-old')
    with env.repo.lock:
        env.repo.get(tid)['status'] = 'failed'
        env.repo.save()

    assert leads_escalation.backfillResolved() == 1
    first = leads_store.get(L)['resolvedRequests']
    assert leads_escalation.backfillResolved() == 0
    assert leads_store.get(L)['resolvedRequests'] == first


def test_backfill_trims_oversized_inbox_no_ghost(env):
    """N1 返工（验方建议）：CAP 逐出与截头错位的一次性幽灵提醒。

    直接播种出 501 条已了结的旧 escalation（违背 ≤500 不变式的形状）→ 补标
    前先截回保留窗口：窗口外那条不在收件箱里、不标；盘上没有"未落定"的
    escalation → sweeper 不出幽灵「有 1 条下属消息未处理」。
    """
    login(env.client)
    L = newLead(env, '甲')
    old = time.time() - 10 ** 6
    rows = [{'seq': i + 1, 'dir': 'up', 'kind': 'escalation', 'at': old,
             'text': '任务 t%04d 请求审批（requestId rq-%04d）' % (i + 1, i + 1),
             'ref': {'task': 't%04d' % (i + 1),
                     'requestId': 'rq-%04d' % (i + 1)}}
            for i in range(501)]
    lead_inbox_store.writeAll(lead_inbox_store.inboxPath(L), rows)

    assert leads_escalation.backfillResolved() == 500
    kept = lead_inbox_store.readAll(L)
    assert len(kept) == 500                    # 截回保留窗口（rq-0001 出局）
    resolved = leads_store.get(L).get('resolvedRequests') or {}
    assert len(resolved) == 500 and 'rq-0001' not in resolved
    env.sweep(now=time.time())                 # 全落定：不出幽灵提醒
    assert leads_inbox.entriesAfter(L, 0, 'down')[0] == []


# ---------------------------------------------------------------- 其它 notice（要求 4）

def test_own_permission_notice_latest_only_then_cleared(env):
    """N1 要求 4：主管自己待批提示同类只留最新、条件解除即收回（sweeper
    每轮复算，不需要用户点；条目本身不删）。"""
    login(env.client)
    L, H = hostedViaApi(env)
    task = env.repo.get(H)
    with env.repo.lock:
        task['status'] = 'running'
        task['pendingPermission'] = 'rq-a'
        env.repo.save()
    # 同一任务顺序撞两次审批：写两条，只显示最新那条
    leads_escalation.noticeOwnPermission(task, 'Bash', {'command': 'ls'})
    leads_escalation.noticeOwnPermission(task, 'Bash',
                                         {'command': 'rm -rf build'})
    down = leads_inbox.entriesAfter(L, 0, 'down')[0]
    assert len(down) == 1 and 'rm -rf build' in down[0]['text']
    assert len(rawDown(env, L)) == 2           # 只追加不改写

    # 条件解除：审批落定（runner 先清 pendingPermission 后落定）+ sweeper 一轮
    with env.repo.lock:
        task['pendingPermission'] = ''
        env.repo.save()
    env.sweep(now=time.time() + 601)
    assert leads_inbox.entriesAfter(L, 0, 'down')[0] == []
    assert len(rawDown(env, L)) == 2


# ---------------------------------------------------------------- 返工：收回标记不空写

def test_mark_notice_cleared_skips_unchanged(env):
    """N1 返工（验方建议）：状态没变不写盘——sweeper 5s 一跳，别每跳重写
    leads.json、还顶掉 updatedAt（从没用过令牌的托管主管会被拖成"无动静"）。"""
    login(env.client)
    L = newLead(env, '甲')
    saves = []
    realSave = leads_store._save

    def counting(data):
        saves.append(data)
        realSave(data)

    env.monkeypatch.setattr(leads_store, '_save', counting)
    assert leads_store.markNoticeCleared(L, 'response', 7)[1] == ''
    assert len(saves) == 1
    assert leads_store.markNoticeCleared(L, 'response', 7)[1] == ''
    assert len(saves) == 1                      # 同族同 seq：不写盘
    assert leads_store.markNoticeCleared(L, 'response', 8)[1] == ''
    assert len(saves) == 2                      # 换了条：写
