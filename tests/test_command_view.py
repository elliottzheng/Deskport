# -*- coding: utf-8 -*-
"""批次 U 指挥台：overview 分组/裁剪、pending 三类、reports-read 只增不减、
鉴权只认网页会话、响应体积；U 返工追加：停用主管名字（retiredLeads）与
「更多」截断标记（/api/tasks truncated）。全部桩化：config.statePath 与任务
仓储指到 tmp，后台扫掠线程在夹具里换成空操作（照 test_leads_inbox_api.py）。

N1 追加面：重复提醒的合成历史数据 → overview 里每主管只剩 1 条或 0 条
（读侧瘦身，指挥台代码零改动）。
"""
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import command_view, events as eventsMod, lead_inbox_store, leads_inbox, leads_store
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
NOW = 1_800_000_000.0            # 固定"现在"，24h 窗口断言不看真时钟


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
    monkeypatch.setattr(leads_inbox, 'sweepOnce', lambda: None)
    # 冻结 command_view 的"现在"：24h 窗口与在忙判定不看真时钟
    monkeypatch.setattr(command_view, 'time',
                        SimpleNamespace(time=lambda: NOW))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           repo=repo, tmp=tmp_path, app=app)


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


def putTask(env, tid, **kw):
    """直接往仓储放一个任务（不跑 runner）；默认 waiting + 无主管。"""
    task = {'id': tid, 'title': tid, 'prompt': 'p', 'status': 'waiting',
            'events': [], 'seq': 1, 'lead': '', 'node': 'local',
            'mode': 'default', 'resultFiles': [], 'workspace': 'ws',
            'createdAt': NOW - 3600, 'lastActiveAt': NOW - 60, 'endedAt': 0}
    task.update(kw)
    env.repo.put(task)
    return task


def permEvent(task, requestId='rq-1', name='Bash', args=None):
    event = {'seq': 9, 'at': NOW - 30, 'type': 'permission_request',
             'requestId': requestId, 'name': name,
             'args': args if args is not None else {'command': 'ls -la /tmp'},
             'options': [{'optionId': 'o1', 'name': '允许', 'kind': 'allow_once'},
                         {'optionId': 'o2', 'name': '总是允许',
                          'kind': 'allow_always'}]}
    task['events'].append(event)
    task['pendingPermission'] = requestId
    return event


def patchLead(env, leadId, **fields):
    """改 leads.json 里的记录字段（lastSeenAt 之类没有公开 setter 的）。"""
    path = configMod.statePath('leads.json')
    with open(path, encoding='utf-8') as f:
        table = json.load(f)
    table[leadId].update(fields)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(table, f)


def overview(env):
    resp = call(env, 'get', '/api/command/overview')
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


# ---------------------------------------------------------------- 例子 1 / 分组

def test_overview_empty(env):
    """空库：三块全空，pending 是空列表（不是 null）。"""
    login(env.client)
    data = overview(env)
    assert data['pending'] == []
    assert data['leads'] == []
    assert data['unassigned'] == {'count': 0, 'running': 0,
                                  'closedHidden': 0, 'tasks': []}


def test_overview_groups_by_lead_and_unassigned(env):
    """例 1：两个主管（一个托管、一个外部）各 3 个下属，另有 2 个无主任务。"""
    login(env.client)
    hosted = newLead(env, '甲')
    external = newLead(env, '乙')
    for i in range(3):
        putTask(env, 'h%d' % i, lead=hosted)
        putTask(env, 'e%d' % i, lead=external)
    leads_store.bindTask(hosted, 'h0')      # 甲是托管主管，h0 是它的会话
    putTask(env, 'u1')
    putTask(env, 'u2')
    data = overview(env)
    assert [l['name'] for l in data['leads']] == ['甲', '乙']
    assert [l['kind'] for l in data['leads']] == ['hosted', 'external']
    assert data['leads'][0]['taskId'] == 'h0'
    assert [len(l['tasks']) for l in data['leads']] == [3, 3]
    assert data['unassigned']['count'] == 2
    assert len(data['unassigned']['tasks']) == 2


def test_closed_tasks_trimmed_to_five(env):
    """裁剪：未关闭全部显示，已关闭只留最近 5 条，其余记 closedHidden。"""
    login(env.client)
    lead = newLead(env, '甲')
    for i in range(3):                      # 未关闭
        putTask(env, 'open%d' % i, lead=lead, status='waiting')
    for i in range(7):                      # 已关闭，endedAt 越大的越新
        putTask(env, 'closed%d' % i, lead=lead, status='closed',
                endedAt=NOW - 1000 + i * 10)
    data = overview(env)
    row = data['leads'][0]
    ids = [t['id'] for t in row['tasks']]
    assert len(ids) == 8 and row['closedHidden'] == 2
    assert set(ids[:3]) == {'open0', 'open1', 'open2'}
    assert ids[3:] == ['closed6', 'closed5', 'closed4', 'closed3', 'closed2']
    assert all(t['lead'] == lead for t in row['tasks'])


# ---------------------------------------------------------------- pending 审批

def test_pending_permission_row(env):
    """例 2：挂起审批 → 待办一行，含工具名/参数摘要/options/所属主管。"""
    login(env.client)
    lead = newLead(env, '甲')
    task = putTask(env, 't1', lead=lead, status='running', owner='kimi-code')
    permEvent(task, args={'command': 'ls -la', 'description': '看看目录里有什么'})
    data = overview(env)
    rows = [p for p in data['pending'] if p['kind'] == 'permission']
    assert len(rows) == 1
    row = rows[0]
    assert row['taskId'] == 't1' and row['leadId'] == lead
    assert row['requestId'] == 'rq-1' and row['title'] == 'Bash'
    # U3：命令单独一条（不再把 command 和其它值糊成一行），用途/派活令牌也带上
    assert row['detail'] == 'ls -la'
    assert row['purpose'] == '看看目录里有什么'
    assert row['owner'] == 'kimi-code'
    assert [o['optionId'] for o in row['options']] == ['o1', 'o2']
    # 名下有审批 → 主管卡片标「等你」
    assert data['leads'][0]['state'] == 'waiting_you'


def test_pending_permission_detail_truncated(env):
    """参数摘要 ≤80 字：长值截断，不把待办行撑爆。"""
    login(env.client)
    task = putTask(env, 't1', status='running')
    permEvent(task, args={'command': 'y' * 200, 'path': '/tmp/b.txt'})
    row = [p for p in overview(env)['pending']
           if p['kind'] == 'permission'][0]
    assert len(row['detail']) == 80 and set(row['detail']) == {'y'}


def test_pending_permission_without_event(env):
    """审批事件被事件流上限挤掉：行还在（工具名兜底），options 为空。"""
    login(env.client)
    task = putTask(env, 't1', status='running')
    task['pendingPermission'] = 'rq-gone'
    data = overview(env)
    rows = [p for p in data['pending'] if p['kind'] == 'permission']
    assert len(rows) == 1
    assert rows[0]['title'] == '工具调用' and rows[0]['options'] == []


# ---------------------------------------------------------------- pending 汇报

def test_pending_reports_and_read_clears(env):
    """例 3：两条未读汇报 → 两行；已读到较新一条 → 两行都消失。"""
    login(env.client)
    lead = newLead(env, '乙')
    assert leads_inbox.append(lead, 'down', 'report', '第一段：跑完了') == 1
    assert leads_inbox.append(lead, 'down', 'report',
                              '第二段：\n换行只显示首行') == 2
    data = overview(env)
    rows = [p for p in data['pending'] if p['kind'] == 'report']
    bySeq = {r['seq']: r for r in rows}
    assert set(bySeq) == {1, 2}
    assert bySeq[1]['detail'] == '第一段：跑完了'
    assert bySeq[2]['detail'] == '第二段：'
    assert all(r['title'] == '乙' and r['leadId'] == lead for r in rows)
    assert data['leads'][0]['unreadReports'] == 2
    assert data['leads'][0]['state'] == 'waiting_you'

    resp = call(env, 'post', '/api/command/leads/%s/reports/read' % lead,
                {'seq': 2})
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'reportSeq': 2}
    data = overview(env)
    assert [p for p in data['pending'] if p['kind'] == 'report'] == []
    assert data['leads'][0]['unreadReports'] == 0
    # 已读不删条目：最近一条汇报仍在卡片上
    assert data['leads'][0]['lastReport']['seq'] == 2


def test_down_notice_keeps_its_own_kind(env):
    """L 返工：down 侧的 notice（L1 主管超时提醒 / 批次 A 主管自己待批
    提示）按条目自己的 kind 进指挥台，不能一律显示成「汇报」。"""
    login(env.client)
    lead = newLead(env, '乙')
    assert leads_inbox.append(lead, 'down', 'notice',
                              '主管 乙 有 1 条下属消息超过 10 分钟未处理') == 1
    data = overview(env)
    rows = [p for p in data['pending'] if p['seq'] == 1]
    assert len(rows) == 1 and rows[0]['kind'] == 'notice'
    assert rows[0]['title'] == '乙' and '未处理' in rows[0]['detail']


def test_duplicate_notices_collapse_in_overview(env):
    """N1 指挥台快照：12 条重复提醒的历史数据（合成）→ 每主管只剩 1 条或
    0 条，数字为最新。瘦身在 leads_inbox 读侧（entriesAfter 的 down 过滤），
    指挥台代码零改动；收件箱只追加不改写，原始 12 条都在。"""
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
    # 未读数/卡片徽标/「最近一条」只算没被收走的
    assert byName['刷屏主管']['unreadReports'] == 1
    assert byName['刷屏主管']['state'] == 'waiting_you'
    assert byName['刷屏主管']['lastReport']['seq'] == 12
    assert byName['已解除主管']['unreadReports'] == 0
    assert byName['已解除主管']['state'] == 'asleep'
    assert byName['已解除主管']['lastReport'] is None
    # 只追加不改写：原始 12 条都在盘上
    assert len([e for e in lead_inbox_store.readAll(spam)
                if e.get('dir') == 'down']) == 12


def test_reports_read_only_increases(env):
    """reports-read 只增不减；坏入参与不存在的主管有明确错误。"""
    login(env.client)
    lead = newLead(env, '乙')
    for seq in (3, 1, 2):
        resp = call(env, 'post', '/api/command/leads/%s/reports/read' % lead,
                    {'seq': seq})
        assert resp.get_json()['reportSeq'] == 3
    assert call(env, 'post', '/api/command/leads/ld_nope/reports/read',
                {'seq': 1}).status_code == 404
    assert call(env, 'post', '/api/command/leads/%s/reports/read' % lead,
                {}).status_code == 400
    assert call(env, 'post', '/api/command/leads/%s/reports/read' % lead,
                {'seq': 'abc'}).status_code == 400
    assert call(env, 'post', '/api/command/leads/%s/reports/read' % lead,
                {'seq': 5}).get_json()['reportSeq'] == 5


# ---------------------------------------------------------------- pending 出问题

def test_pending_problems_and_window(env):
    """例 4：failed/interrupted/空转/静默都进待办，超 24h 的不进。"""
    login(env.client)
    lead = newLead(env, '甲')
    putTask(env, 'f1', lead=lead, status='failed',
            error='连接崩了\n第二行不该出现', endedAt=NOW - 3600)
    putTask(env, 'i1', lead=lead, status='interrupted', endedAt=NOW - 7200)
    putTask(env, 'old', lead=lead, status='failed', error='太久了',
            endedAt=NOW - 25 * 3600)
    putTask(env, 'silent', lead=lead, status='done',
            lastTurn={'silent': True}, endedAt=NOW - 7200)
    putTask(env, 'empty-old', lead=lead, status='done',
            lastTurn={'empty': True}, endedAt=NOW - 25 * 3600)
    data = overview(env)
    rows = {p['taskId']: p for p in data['pending'] if p['kind'] == 'problem'}
    assert set(rows) == {'f1', 'i1', 'silent'}
    assert rows['f1']['detail'] == '连接崩了'
    assert rows['i1']['detail'].startswith('任务中断了')
    assert '终稿' in rows['silent']['detail']
    assert all(p['leadId'] == lead for p in rows.values())


# ---------------------------------------------------------------- 主管状态

def test_lead_states(env):
    """状态徽标：已暂停 > 等你 > 在忙 > 睡着；托管看任务、外部看 lastSeenAt。"""
    login(env.client)
    busyHosted = newLead(env, '忙主管')
    leads_store.bindTask(busyHosted, 'own-running')
    putTask(env, 'own-running', lead=busyHosted, status='running')
    idleHosted = newLead(env, '闲主管')
    leads_store.bindTask(idleHosted, 'own-waiting')
    putTask(env, 'own-waiting', lead=idleHosted, status='waiting')
    seen = newLead(env, '刚上线')
    patchLead(env, seen, lastSeenAt=NOW - 60)
    sleepy = newLead(env, '睡着了')
    patchLead(env, sleepy, lastSeenAt=NOW - 400)
    paused = newLead(env, '已暂停')
    leads_store.update(paused, {'status': 'paused'})
    states = {l['name']: l['state'] for l in overview(env)['leads']}
    assert states == {'忙主管': 'busy', '闲主管': 'asleep', '刚上线': 'busy',
                      '睡着了': 'asleep', '已暂停': 'paused'}


def test_retired_lead_not_on_board(env):
    """停用的主管不上指挥台（历史归设置页管）。"""
    login(env.client)
    lead = newLead(env, '老甲')
    putTask(env, 't1', lead=lead)
    leads_store.update(lead, {'status': 'retired'})
    data = overview(env)
    assert data['leads'] == []
    assert data['unassigned']['count'] == 0


def test_retired_lead_name_still_available(env):
    """U 返工：停用主管不上卡片，但 retiredLeads 带出 id→名字——名下刚失败的
    任务仍进「等你处理」，前端据此补主管名；详情面板显示「已停用的主管」。"""
    login(env.client)
    lead = newLead(env, '老甲')
    putTask(env, 't1', lead=lead, status='failed', error='连接超时',
            endedAt=NOW - 600)
    leads_store.update(lead, {'status': 'retired'})
    data = overview(env)
    assert data['leads'] == []
    assert data['retiredLeads'] == {lead: '老甲'}
    rows = [p for p in data['pending'] if p['kind'] == 'problem']
    assert len(rows) == 1 and rows[0]['leadId'] == lead
    assert data['unassigned']['count'] == 0        # 有主的任务不落「其它任务」


def test_more_tasks_truncated_flag(env):
    """U 返工：「更多」拉的 /api/tasks?full=1&lead= 截前 50 条时带
    truncated=true（前端显示置灰「仅显示最近 50 条」，不静默消失）；
    旧字段一个不改。"""
    login(env.client)
    lead = newLead(env, '甲')
    for i in range(55):
        putTask(env, 't%03d' % i, lead=lead, status='waiting')
    data = call(env, 'get', '/api/tasks?full=1&lead=' + lead).get_json()
    assert len(data['tasks']) == 50 and data['truncated'] is True
    assert set(data) >= {'tasks', 'running', 'queued', 'limit', 'countScope'}
    small = call(env, 'get', '/api/tasks?full=1&lead=ld_none').get_json()
    assert small['tasks'] == [] and small['truncated'] is False


# ---------------------------------------------------------------- 鉴权与体积

def test_command_endpoints_web_session_only(env):
    """只认网页会话：未登录与智能体令牌一律 401（authGuard 统一档）。"""
    login(env.client)
    lead = newLead(env, '甲')
    assert overview(env)['leads'][0]['name'] == '甲'
    tok = newToken(env, 'T9')['token']
    assert env.fresh.get('/api/command/overview').status_code == 401
    assert call(env, 'get', '/api/command/overview', token=tok).status_code == 401
    assert call(env, 'post', '/api/command/leads/%s/reports/read' % lead,
                {'seq': 1}, token=tok).status_code == 401


def test_response_size_under_200kb(env):
    """例 11：300 个任务的合成数据下 overview < 200KB（U2 加了 tree 字段，
    体积要求不变——树与旧字段承载同一批任务，故树的任务节点只带行内字段）。"""
    login(env.client)
    for i in range(2):
        lead = newLead(env, '主管%02d' % i)
        for j in range(55):                  # 未关闭的全部要出现在响应里
            putTask(env, 'L%d-o%03d' % (i, j), lead=lead, status='waiting',
                    lastText='t' * 200)
        for j in range(40):
            putTask(env, 'L%d-c%03d' % (i, j), lead=lead, status='closed',
                    endedAt=NOW - j, error='e' * 100)
    for j in range(97):                      # 无主任务：未关闭
        putTask(env, 'U-o%03d' % j, status='failed', error='x' * 100)
    for j in range(13):                      # 无主任务：已关闭（只露 5 条）
        putTask(env, 'U-c%03d' % j, status='closed', endedAt=NOW - j)
    assert len(env.repo.list()) == 300
    resp = call(env, 'get', '/api/command/overview')
    body = resp.get_data(as_text=True)
    size = len(body.encode('utf-8'))
    assert size < 200 * 1024, size
    data = json.loads(body)
    assert len(data['unassigned']['tasks']) == 97 + 5
    assert data['unassigned']['closedHidden'] == 8
    assert data['unassigned']['running'] == 0
