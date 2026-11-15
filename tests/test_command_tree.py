# -*- coding: utf-8 -*-
"""批次 U2 指挥树：一级节点分组、托管任务即节点本身、子主管递归与计数汇总、
折层、停用主管的 orphan 组、已关闭裁剪；旧字段不受影响。全部桩化（照
tests/test_command_view.py：config.statePath 与任务仓储指到 tmp，扫掠线程换空操作）。
"""
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import command_view, events as eventsMod, leads_inbox, leads_store
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
NOW = 1_800_000_000.0            # 固定"现在"，24h 窗口与在忙判定不看真时钟


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
    monkeypatch.setattr(command_view, 'time',
                        SimpleNamespace(time=lambda: NOW))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           repo=repo, tmp=tmp_path, app=app)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None):
    kw = {}
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(env.client, method)(path, **kw)


def newLead(env, name='甲', **kw):
    body = {'name': name}
    body.update(kw)
    resp = call(env, 'post', '/api/settings/leads', body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['lead']['id']


def putTask(env, tid, **kw):
    """直接往仓储放一个任务（不跑 runner）；默认 waiting + 无主管 + 面板派的。"""
    task = {'id': tid, 'title': tid, 'titleSource': 'given', 'prompt': 'p',
            'status': 'waiting', 'events': [], 'seq': 1, 'lead': '',
            'owner': '面板', 'node': 'local', 'mode': 'default',
            'resultFiles': [], 'workspace': 'ws',
            'createdAt': NOW - 3600, 'lastActiveAt': NOW - 60, 'endedAt': 0}
    task.update(kw)
    env.repo.put(task)
    return task


def patchLead(env, leadId, **fields):
    """改 leads.json 里的记录字段（parent 之类没有公开 setter 的）。"""
    path = configMod.statePath('leads.json')
    with open(path, encoding='utf-8') as f:
        table = json.load(f)
    table[leadId].update(fields)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(table, f)


def tree(env):
    data = call(env, 'get', '/api/command/overview').get_json()
    return data['tree']


def flat(nodes, depth=0, out=None):
    """深度优先摊平：[(node, depth)]，用来断层级与顺序。"""
    out = [] if out is None else out
    for node in nodes or []:
        out.append((node, depth))
        flat(node.get('children') or [], depth + 1, out)
    return out


def top(treeNodes):
    return [n['name'] for n in treeNodes]


# ---------------------------------------------------------------- 例 1 / 一级分组

def test_example1_four_top_level_groups(env):
    """例 1：托管主管 A（3 个下属）、外部主管 B（2 个）、未登记令牌 kimi-code
    派的 2 个、面板自己派的 1 个 → 4 个一级节点，顺序即规格草图。"""
    login(env.client)
    hosted = newLead(env, '甲')
    external = newLead(env, '乙')
    for i in range(3):
        putTask(env, 'h%d' % i, lead=hosted)
    for i in range(2):
        putTask(env, 'e%d' % i, lead=external)
    leads_store.bindTask(hosted, 'h0')      # 甲是托管主管，h0 是它的会话
    for i in range(2):
        putTask(env, 'k%d' % i, owner='kimi-code')
    putTask(env, 'mine')
    rows = flat(tree(env))
    assert [n['name'] for n, d in rows if d == 0] == ['甲', '乙', 'kimi-code',
                                                     '你直接派的']
    assert [n['type'] for n, d in rows if d == 0] == ['lead', 'lead', 'owner',
                                                     'direct']
    # 托管任务是节点本身：不在子项里重复，但计数含它（3 个下属都在跑/等待）
    a = [n for n, d in rows if d == 0][0]
    assert a['taskId'] == 'h0' and a['kind'] == 'hosted'
    assert [c['id'] for c in a['children']] == ['h1', 'h2']
    assert a['counts'] == {'running': 0, 'waiting': 3, 'waiting_you': 3,
                           'failed': 0}
    assert a['children'][0]['type'] == 'task'
    assert a['children'][0]['key'] == 'task:h1'
    assert a['children'][0]['title'] == 'h1'       # 行内标题
    # 未登记指挥者带 id（= 令牌名），面板自己派的 id 为空
    owner = [n for n, d in rows if d == 0][2]
    assert owner['id'] == 'kimi-code' and owner['key'] == 'owner:kimi-code'
    assert [c['id'] for c in owner['children']] == ['k0', 'k1']
    direct = [n for n, d in rows if d == 0][3]
    assert direct['id'] == '' and direct['key'] == 'direct'
    assert [c['id'] for c in direct['children']] == ['mine']


def test_no_leads_still_grouped(env):
    """例 7：没有任何主管 → 树仍按「未登记的指挥者 / 你直接派的」分层，
    不是一张平列表。"""
    login(env.client)
    putTask(env, 'k1', owner='kimi-code')
    putTask(env, 'k2', owner='kimi-code')
    putTask(env, 'x1', owner='external-2')
    putTask(env, 'mine')
    rows = flat(tree(env))
    assert [n['name'] for n, d in rows if d == 0] == ['kimi-code', 'external-2',
                                                     '你直接派的']
    # 任务全部挂在组下面（深度 ≥ 1），没有任务是顶级节点
    assert all(d >= 1 for n, d in rows if n['type'] == 'task')
    data_leads_still_intact(env)


def data_leads_still_intact(env):
    """旧字段没被树顶掉：leads/unassigned/pending 照旧（前端回滚用）。"""
    data = call(env, 'get', '/api/command/overview').get_json()
    assert data['leads'] == [] and data['retiredLeads'] == {}
    assert [t['id'] for t in data['unassigned']['tasks']] == ['k1', 'k2', 'x1',
                                                              'mine']


# ---------------------------------------------------------------- 例 2 / 子主管

def test_example2_sub_lead_and_rollup(env):
    """例 2：A 下有子主管 A1（parent=A）及其 2 个下属 → A1 是 A 的子节点，
    层级正确，A1 的任务在第三层，计数汇总到 A。"""
    login(env.client)
    a = newLead(env, '甲')
    a1 = newLead(env, '甲一')
    patchLead(env, a1, parent=a)
    putTask(env, 'a-own', lead=a)
    putTask(env, 'a1-t0', lead=a1, status='running')
    putTask(env, 'a1-t1', lead=a1, status='failed', endedAt=NOW - 60)
    rows = flat(tree(env))
    byId = {n['id']: (n, d) for n, d in rows}
    assert byId[a][1] == 0 and byId[a1][1] == 1
    assert byId['a1-t0'][1] == 2 and byId['a1-t1'][1] == 2
    assert [c['id'] for c in byId[a][0]['children']] == ['a-own', a1]
    # 逐级汇总：A 的计数 = 自己名下 1 个 + A1 子树 2 个
    assert byId[a][0]['counts'] == {'running': 1, 'waiting': 1, 'waiting_you': 1,
                                    'failed': 1}
    assert byId[a1][0]['counts'] == {'running': 1, 'waiting': 0, 'waiting_you': 0,
                                     'failed': 1}


def test_depth_over_three_folds_into_more(env):
    """最多 3 层：第四层起折成「更多层级 N」，只给汇总计数，不再展开。"""
    login(env.client)
    a = newLead(env, '甲')
    a1 = newLead(env, '甲一')
    a2 = newLead(env, '甲二')
    a3 = newLead(env, '甲三')
    patchLead(env, a1, parent=a)
    patchLead(env, a2, parent=a1)
    patchLead(env, a3, parent=a2)
    putTask(env, 't1', lead=a1, status='running')
    putTask(env, 't2', lead=a2, status='running')
    putTask(env, 't3', lead=a3, status='waiting')
    rows = flat(tree(env))
    byId = {n['id']: (n, d) for n, d in rows}
    assert byId[a2][1] == 2 and a3 not in byId      # 第四层被折起来，不上树
    folded = [c for c in byId[a2][0]['children'] if c['type'] == 'more']
    assert len(folded) == 1 and folded[0]['name'] == '更多层级 1'
    assert folded[0]['children'] == []
    # 折起来的计数含第四层主管自己的任务
    assert folded[0]['counts'] == {'running': 0, 'waiting': 1, 'waiting_you': 1,
                                   'failed': 0}
    # 顶层计数一路汇总上来
    assert byId[a][0]['counts'] == {'running': 2, 'waiting': 1, 'waiting_you': 1,
                                    'failed': 0}


# ---------------------------------------------------------------- 例 3 / 等你

def test_example3_pending_permission_on_tree(env):
    """例 3：a2 挂起审批 → 待办行带 taskId/leadId，A 标「等你」，a2 在 A 底下。"""
    login(env.client)
    a = newLead(env, '甲')
    task = putTask(env, 'a2', lead=a, status='running')
    task['events'].append({'seq': 9, 'at': NOW - 30, 'type': 'permission_request',
                           'requestId': 'rq-1', 'name': 'Bash',
                           'args': {'command': 'ls'},
                           'options': [{'optionId': 'o1', 'name': '允许',
                                        'kind': 'allow_once'}]})
    task['pendingPermission'] = 'rq-1'
    env.repo.put(task)
    data = call(env, 'get', '/api/command/overview').get_json()
    row = [p for p in data['pending'] if p['kind'] == 'permission'][0]
    assert row['taskId'] == 'a2' and row['leadId'] == a
    rows = flat(data['tree'])
    assert (a, 0) in [(n['id'], d) for n, d in rows]
    node = [n for n, _d in rows if n.get('id') == 'a2'][0]
    assert node['type'] == 'task' and node['key'] == 'task:a2'
    # 「等你」= 名下有挂起审批（_leadState）；前端再按 pending 的 taskId 给
    # 任务行补「等你」标（JS 单测钉住），后端只保证 id 对得上
    assert data['leads'][0]['state'] == 'waiting_you'
    assert data['tree'][0]['state'] == 'waiting_you'
    assert {p['taskId'] for p in data['pending']} == {'a2'}


# ---------------------------------------------------------------- 停用主管 / 裁剪

def test_retired_lead_tasks_become_orphan_group(env):
    """停用主管不上树，但它名下的任务不消失：并进 orphan 组，名字用停用前的名。"""
    login(env.client)
    old = newLead(env, '老甲')
    putTask(env, 't1', lead=old)
    putTask(env, 't2', lead=old, status='closed', endedAt=NOW - 10)
    leads_store.update(old, {'status': 'retired'})
    rows = flat(tree(env))
    groups = [n for n, d in rows if d == 0]
    assert [g['type'] for g in groups] == ['orphan']
    assert groups[0]['name'] == '老甲' and groups[0]['id'] == old
    assert [c['id'] for c in groups[0]['children']] == ['t1', 't2']
    assert groups[0]['closedHidden'] == 0


def test_orphan_group_for_dangling_lead_id(env):
    """主管记录被删干净（悬空 lead）也进树，名字退回「已停用的主管 <id>」。"""
    login(env.client)
    putTask(env, 't1', lead='ld_gone')
    groups = [n for n, d in flat(tree(env)) if d == 0]
    assert [g['type'] for g in groups] == ['orphan']
    assert groups[0]['name'] == '已停用的主管 ld_gone'


def test_closed_trimmed_per_node(env):
    """每个节点下默认只显示最近 5 条已关闭，其余记 closedHidden。"""
    login(env.client)
    a = newLead(env, '甲')
    for i in range(3):
        putTask(env, 'open%d' % i, lead=a, status='waiting')
    for i in range(7):
        putTask(env, 'closed%d' % i, lead=a, status='closed',
                endedAt=NOW - 1000 + i * 10)
    node = tree(env)[0]
    ids = [c['id'] for c in node['children']]
    assert ids[:3] == ['open0', 'open1', 'open2']
    assert ids[3:] == ['closed6', 'closed5', 'closed4', 'closed3', 'closed2']
    assert node['closedHidden'] == 2


def test_keys_unique_and_stable(env):
    """key 全树唯一（前端拿它记展开状态与高亮），同数据两次请求逐字一致。"""
    login(env.client)
    a = newLead(env, '甲')
    a1 = newLead(env, '甲一')
    patchLead(env, a1, parent=a)
    putTask(env, 't1', lead=a1)
    putTask(env, 't2', owner='kimi-code')
    putTask(env, 't3')
    rows = flat(tree(env))
    keys = [n['key'] for n, _d in rows]
    assert len(keys) == len(set(keys))
    assert set(keys) == {'lead:' + a, 'lead:' + a1, 'task:t1', 'task:t2',
                         'task:t3', 'owner:kimi-code', 'direct'}
    assert tree(env) == tree(env)


def test_group_nodes_carry_no_task_payload(env):
    """分组节点只带展示字段：全量任务对象（events/workspace…）不得混进响应——
    U2 走查时发现 dict(bucket, …) 把 bucket['tasks'] 原样带出去了。"""
    login(env.client)
    putTask(env, 'k1', owner='kimi-code', lastText='x' * 200)
    putTask(env, 'mine', lastText='y' * 200)
    data = call(env, 'get', '/api/command/overview').get_json()
    for node in data['tree']:
        assert 'tasks' not in node, node['type']
        assert 'events' not in node and 'workspace' not in node
    # 旧字段仍带全量 brief（回滚用），树只带行内字段
    assert [t['lastText'] for t in data['unassigned']['tasks']] == ['x' * 200,
                                                                    'y' * 200]
    assert 'lastText' not in data['tree'][0]['children'][0]


def test_old_fields_unchanged_by_tree(env):
    """旧字段形状不变：leads/unassigned 仍带全量 brief，树只带行内字段。"""
    login(env.client)
    a = newLead(env, '甲')
    putTask(env, 't1', lead=a, lastText='x' * 200, error='boom')
    data = call(env, 'get', '/api/command/overview').get_json()
    brief = data['leads'][0]['tasks'][0]
    assert brief['lastText'] == 'x' * 200 and brief['error'] == 'boom'
    node = data['tree'][0]['children'][0]
    assert node['id'] == 't1' and 'lastText' not in node and 'error' not in node
    assert node['status'] == 'waiting' and node['node'] == 'local'


def test_tree_auth_web_session_only(env):
    """树跟着 overview 走：未登录与智能体令牌一律 401。"""
    login(env.client)
    newLead(env, '甲')
    assert call(env, 'get', '/api/command/overview').status_code == 200
    assert env.fresh.get('/api/command/overview').status_code == 401
    tok = call(env, 'post', '/api/settings/agent-tokens',
               {'name': 'T9'}).get_json()['token']
    resp = env.fresh.get('/api/command/overview',
                         headers={'X-Agent-Token': tok})
    assert resp.status_code == 401
