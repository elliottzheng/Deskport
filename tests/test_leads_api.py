# -*- coding: utf-8 -*-
"""主管记录 + 令牌绑定 + 任务自动记归属（批次 Q P1）：规格「例子」表 15 行 + 校验/绑定/审计；核心验收是没绑主管的令牌与网页会话行为零变化。全部桩化，config.statePath 与任务/工作线仓储都指到 tmp。
"""
import io
import json
import re
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import leads_store
from modules import tasks_api
from modules import tasks_store as store
from modules import worklines_store
from modules.tasks_store import TaskRepository
from modules.worklines_store import WorklineRepository

PASSWORD = 'pw-123456'
LEAD_ID_RE = re.compile(r'^ld_[0-9a-f]{8}$')
LEAD_KEYS = {'id', 'name', 'project', 'kind', 'parent', 'status', 'taskId', 'workline',
             'ceiling', 'createdAt', 'updatedAt', 'lastSeenAt'}


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
        audit.append({'action': action, 'target': target})

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', fakeLog)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    wlRepo = WorklineRepository(str(tmp_path / 'worklines.json'),
                                str(tmp_path / 'wl-docs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    monkeypatch.setattr(worklines_store, 'repo', wlRepo)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           audit=audit, repo=repo, tmp=tmp_path,
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


def createLead(env, name='甲', **kw):
    body = {'name': name}
    body.update(kw)
    return call(env, 'post', '/api/settings/leads', body)


def newLead(env, name='甲', **kw):
    """建主管，返回记录 id。"""
    resp = createLead(env, name, **kw)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['lead']['id']


def bind(env, leadId, tokenId):
    return call(env, 'post', '/api/settings/leads/%s/tokens' % leadId, {'tokenId': tokenId})


def unbind(env, leadId, tokenId):
    return call(env, 'delete', '/api/settings/leads/%s/tokens/%s' % (leadId, tokenId))


def setStatus(env, leadId, status):
    return call(env, 'patch', '/api/settings/leads/' + leadId, {'status': status})


def task(env, token, body=None):
    # 令牌请求必须走 fresh：带会话的 client 会进网页会话分支，
    # g.agentLead 永远是 ''，就测不到令牌通道了
    return call(env, 'post', '/api/tasks', body or {'prompt': 'x'}, token=token)


def leadsOf(env):
    return call(env, 'get', '/api/settings/leads').get_json()['leads']


def auditOf(env, action):
    return [a['target'] for a in env.audit if a['action'] == action]


def seedOldTask(env):
    """没有 lead 键的老任务（规格验收 #14）。"""
    old = {'id': 'old11111111', 'title': '老任务', 'prompt': '老描述',
           'status': 'waiting', 'sessionId': 'sess-1', 'events': [], 'seq': 1,
           'owner': '面板', 'node': 'local', 'mode': 'default', 'resultFiles': [],
           'createdAt': time.time() - 100, 'lastActiveAt': time.time() - 50,
           'workspace': str(env.tmp / 'ws')}
    env.repo.put(old)
    env.repo.save()
    return old


# ---------------------------------------------------------------- 管理面鉴权与校验

def test_management_endpoints_require_session(env):
    """验收 #10：管理面只认网页会话，全权令牌打过来一样 401。"""
    login(env.client)
    token = newToken(env, 'Claude')['token']
    for method, path, body in (
            ('get', '/api/settings/leads', None), ('post', '/api/settings/leads', {'name': 'x'}),
            ('patch', '/api/settings/leads/ld_x', {'status': 'paused'}), ('delete', '/api/settings/leads/ld_x', None),
            ('post', '/api/settings/leads/ld_x/tokens', {'tokenId': 't'})):
        assert call(env, method, path, body, token=token).status_code == 401
    assert env.fresh.get('/api/settings/leads').status_code == 401


def test_create_and_patch_lead_validation(env):
    login(env.client)
    for bad in ('', '   ', 'x' * 25, 'a<b', 'a"b', "a'b", 'a\nb', 123):
        resp = createLead(env, bad)
        assert resp.status_code == 400 and 'name' in resp.get_json()['error']
    assert createLead(env, '甲', project='x' * 41).status_code == 400
    assert 'project' in createLead(env, '甲', project='x' * 41).get_json()['error']
    assert createLead(env, '甲', kind='ghost').status_code == 400
    assert leadsOf(env) == []          # 校验失败不落盘
    lead = call(env, 'post', '/api/settings/leads',
                {'name': '主管甲', 'project': '课题', 'kind': 'hosted'}).get_json()['lead']
    assert set(lead) == LEAD_KEYS
    assert LEAD_ID_RE.match(lead['id'])
    assert lead['parent'] == 'human' and lead['status'] == 'active'
    assert lead['kind'] == 'hosted' and lead['project'] == '课题'
    assert lead['taskId'] == lead['workline'] == '' and lead['ceiling'] == {}
    assert lead['lastSeenAt'] == 0 and lead['createdAt'] > 0
    stored = json.loads((env.tmp / 'state' / 'leads.json').read_text('utf-8'))
    assert set(stored) == {lead['id']}          # {id: record} 形状
    assert createLead(env, ' 主管甲 ').status_code == 400      # 重名（strip 后）
    setStatus(env, lead['id'], 'retired')
    assert createLead(env, '主管甲').status_code == 201        # retired 名字可回收
    url = '/api/settings/leads/' + newLead(env, '乙')
    assert call(env, 'patch', url, {'title': 'x'}).status_code == 400
    assert call(env, 'patch', url, {'status': 'gone'}).status_code == 400
    newLead(env, '丙')
    assert call(env, 'patch', url, {'name': '丙'}).status_code == 400
    got = call(env, 'patch', url, {'name': '乙2', 'project': None}).get_json()['lead']
    assert got['name'] == '乙2' and got['project'] == ''
    assert call(env, 'patch', '/api/settings/leads/ld_nope', {'status': 'paused'}
                ).status_code == 404


def test_delete_lead_guards_and_suggests_retired(env):
    """验收 #9：还绑着令牌或有任务引用 → 409 并建议 retired。"""
    login(env.client)
    assert call(env, 'delete', '/api/settings/leads/' + newLead(env, '甲')).status_code == 200
    lead2, tok = newLead(env, '乙'), newToken(env, 'Kimi')
    assert bind(env, lead2, tok['id']).status_code == 200
    resp = call(env, 'delete', '/api/settings/leads/' + lead2)
    assert resp.status_code == 409 and 'retired' in resp.get_json()['error']
    assert '令牌' in resp.get_json()['error']
    assert task(env, tok['token']).status_code == 202   # 绑着时建 → 归属乙
    assert unbind(env, lead2, tok['id']).status_code == 200
    resp = call(env, 'delete', '/api/settings/leads/' + lead2)
    assert resp.status_code == 409 and 'retired' in resp.get_json()['error']
    assert '名下有 1 个任务' in resp.get_json()['error']
    assert call(env, 'delete', '/api/settings/leads/ld_nope').status_code == 404


def test_leads_list_shape(env):
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'Kimi')
    row = leadsOf(env)[0]
    assert set(row) == LEAD_KEYS | {'tokens', 'tasks'}
    assert row['tokens'] == [] and row['tasks'] == {'total': 0, 'running': 0, 'waiting': 0}
    assert bind(env, leadId, tok['id']).status_code == 200
    assert task(env, tok['token']).status_code == 202
    row = leadsOf(env)[0]
    assert row['tokens'] == ['Kimi']
    # 新任务是 queued：进 total，不进 running/waiting
    assert row['tasks'] == {'total': 1, 'running': 0, 'waiting': 0}


def test_audit_events(env):
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'Kimi')
    assert auditOf(env, 'lead.created') == ['甲']
    setStatus(env, leadId, 'paused')
    assert auditOf(env, 'lead.updated') == ['甲']
    assert bind(env, leadId, tok['id']).status_code == 200
    assert auditOf(env, 'lead.token.bound') == ['甲']
    assert unbind(env, leadId, tok['id']).status_code == 200
    assert auditOf(env, 'lead.token.unbound') == ['甲']
    assert call(env, 'delete', '/api/settings/leads/' + leadId).status_code == 200
    assert auditOf(env, 'lead.deleted') == ['甲']


# ---------------------------------------------------------------- 绑定规则

def test_bind_and_unbind_rules(env):
    login(env.client)
    lead, lead2 = newLead(env, '甲'), newLead(env, '乙')
    tok = newToken(env, 'Kimi')
    assert bind(env, lead, tok['id']).status_code == 200
    assert bind(env, lead, tok['id']).status_code == 200   # 幂等
    resp = bind(env, lead2, tok['id'])                     # 验收 #8
    assert resp.status_code == 409 and '解绑' in resp.get_json()['error']
    assert bind(env, lead, 'nope').status_code == 404
    assert bind(env, 'ld_nope', tok['id']).status_code == 404
    assert call(env, 'post', '/api/settings/leads/%s/tokens' % lead, {}).status_code == 400
    assert unbind(env, lead2, tok['id']).status_code == 409   # 绑的不是这边
    assert unbind(env, lead, tok['id']).status_code == 200
    assert unbind(env, lead, tok['id']).status_code == 200    # 本来就没绑：幂等
    assert unbind(env, lead, 'nope').status_code == 404
    assert unbind(env, 'ld_nope', tok['id']).status_code == 404
    stored = json.loads((env.tmp / 'config.json').read_text('utf-8'))
    assert 'lead' not in stored['agentTokens'][0]      # 解绑后键消失


def test_revoke_token_unbinds_lead(env):
    """验收 #13：吊销令牌自然解除绑定（条目没了）。"""
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'Kimi')
    newToken(env, 'Kimi2')          # 留一条全权兜底，否则吊销被拒
    assert bind(env, leadId, tok['id']).status_code == 200
    assert {t['name']: t['lead'] for t in
            call(env, 'get', '/api/settings/agent-tokens').get_json()['tokens']}['Kimi'] == leadId
    assert call(env, 'delete', '/api/settings/agent-tokens/' + tok['id']).status_code == 200
    assert leadsOf(env)[0]['tokens'] == []
    listed = call(env, 'get', '/api/settings/agent-tokens').get_json()['tokens']
    assert [t['lead'] for t in listed] == ['']


def test_agent_lead_self_check(env):
    """验收 #11：T1 看到主管六字段，T2 看到 null。"""
    login(env.client)
    leadId = newLead(env, '甲', project='课题')
    t1, t2 = newToken(env, 'T1'), newToken(env, 'T2')
    assert bind(env, leadId, t1['id']).status_code == 200
    body = call(env, 'get', '/api/agent/lead', token=t1['token']).get_json()
    assert body['lead'] == {'id': leadId, 'name': '甲', 'project': '课题',
                            'kind': 'external', 'parent': 'human', 'status': 'active'}
    assert call(env, 'get', '/api/agent/lead', token=t2['token']).get_json() == {'lead': None}
    assert call(env, 'get', '/api/agent/lead').status_code == 401


def test_dangling_binding_null_then_403(env):
    """手改配置绑到不存在的主管：自查视作没绑（Q4），建任务 403 不 500（#15）。"""
    login(env.client)
    tok = newToken(env, 'T1')
    cfg = json.loads((env.tmp / 'config.json').read_text('utf-8'))
    cfg['agentTokens'][0]['lead'] = 'ld_deadbeef'
    (env.tmp / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
    configMod._cache.update(mtime=None, data=None)
    assert call(env, 'get', '/api/agent/lead', token=tok['token']).get_json() == {'lead': None}
    resp = task(env, tok['token'])
    assert resp.status_code == 403
    body = resp.get_json()
    assert body['lead'] == 'ld_deadbeef' and body['status'] == ''
    assert '不存在' in body['error']


# ---------------------------------------------------------------- Q5 归属

def test_bound_token_task_records_lead(env):
    """验收 #1/#2/#14：T1 建的任务 lead == L.id；T2、网页会话与老任务为 ''。"""
    login(env.client)
    leadId = newLead(env, '甲')
    t1, t2 = newToken(env, 'T1'), newToken(env, 'T2')
    assert bind(env, leadId, t1['id']).status_code == 200
    first = task(env, t1['token']).get_json()['task']
    assert first['lead'] == leadId
    detail = call(env, 'get', '/api/tasks/%s' % first['id'], token=t1['token']).get_json()
    assert detail['task']['lead'] == leadId
    rows = call(env, 'get', '/api/tasks', token=t1['token']).get_json()
    assert [r['lead'] for r in rows['tasks']] == [leadId]
    assert task(env, t2['token']).get_json()['task']['lead'] == ''
    assert call(env, 'post', '/api/tasks', {'prompt': 'y'}).get_json()['task']['lead'] == ''
    seedOldTask(env)      # 没有 lead 键的老任务：正常加载，摘要为 ''
    rows = call(env, 'get', '/api/tasks', token=t1['token']).get_json()
    assert [r['lead'] for r in rows['tasks'] if r['lead']] == [leadId]   # 仅此一条
    assert sum(1 for r in rows['tasks'] if not r['lead']) == 3   # T2/网页会话/老任务
    assert call(env, 'get', '/api/tasks/old11111111').get_json()['task']['lead'] == ''


def test_paused_lead_blocks_only_creation(env):
    """验收 #5/#6/#7：停用只拦建任务；已有任务读照常；改回 active 放行。"""
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'T1')
    assert bind(env, leadId, tok['id']).status_code == 200
    first = task(env, tok['token']).get_json()['task']
    before = len(call(env, 'get', '/api/tasks', token=tok['token']).get_json()['tasks'])
    setStatus(env, leadId, 'paused')
    resp = task(env, tok['token'])
    assert resp.status_code == 403
    body = resp.get_json()
    assert body['lead'] == leadId and body['status'] == 'paused'
    assert '停用' in body['error'] and '甲' in body['error']
    after = len(call(env, 'get', '/api/tasks', token=tok['token']).get_json()['tasks'])
    assert after == before                    # 没建成，任务数不变
    got = call(env, 'get', '/api/tasks/%s' % first['id'], token=tok['token'])
    assert got.status_code == 200 and got.get_json()['task']['status']
    setStatus(env, leadId, 'active')
    assert task(env, tok['token']).status_code == 202
    setStatus(env, leadId, 'retired')
    assert task(env, tok['token']).status_code == 403


def test_workline_dispatch_records_lead(env):
    """建任务入口核对：工作线派活（新开分支）同样记 lead。"""
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'T1')
    assert bind(env, leadId, tok['id']).status_code == 200
    wid = call(env, 'post', '/api/worklines', {'title': '线'}).get_json()['workline']['id']
    resp = call(env, 'post', '/api/worklines/%s/tasks' % wid, {'prompt': '干活'},
                token=tok['token'])
    assert resp.status_code == 202 and resp.get_json()['routed'] == 'new'
    assert env.repo.get(resp.get_json()['task']['id'])['lead'] == leadId


def test_share_create_records_lead(env):
    """建任务入口核对：手机转发（/api/agent/share 新建去处）同样记 lead。"""
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'T1')
    assert bind(env, leadId, tok['id']).status_code == 200
    resp = env.fresh.post('/api/agent/share', headers={'X-Agent-Token': tok['token']},
                          data={'file': (io.BytesIO(b'x'), 'a.txt'), 'prompt': '处理这个'},
                          content_type='multipart/form-data')
    assert resp.status_code == 200 and resp.get_json()['mode'] == 'created'
    assert env.repo.get(resp.get_json()['taskId'])['lead'] == leadId


# ---------------------------------------------------------------- Q6/Q7

def test_list_tasks_filtered_by_lead(env):
    """验收 #3/#4：?lead=<id> 只列该主管的；lead=me 按绑定筛；没绑 400。"""
    login(env.client)
    leadId = newLead(env, '甲')
    t1, t2 = newToken(env, 'T1'), newToken(env, 'T2')
    assert bind(env, leadId, t1['id']).status_code == 200
    for _ in range(2):
        assert task(env, t1['token']).status_code == 202
    assert task(env, t2['token']).status_code == 202
    plain = call(env, 'get', '/api/tasks', token=t1['token']).get_json()
    assert set(plain) == {'tasks', 'running', 'queued', 'limit', 'countScope'}
    assert plain['queued'] == 3 and plain['running'] == 0
    mine = call(env, 'get', '/api/tasks?lead=' + leadId, token=t1['token']).get_json()
    assert [t['id'] for t in mine['tasks']] == [t['id'] for t in plain['tasks'] if t['lead'] == leadId]
    assert mine['queued'] == 3 and mine['running'] == 0      # 仍是全局口径
    me = call(env, 'get', '/api/tasks?lead=me', token=t1['token']).get_json()
    assert [t['id'] for t in me['tasks']] == [t['id'] for t in mine['tasks']]
    resp = call(env, 'get', '/api/tasks?lead=me', token=t2['token'])
    assert resp.status_code == 400 and 'lead=me' in resp.get_json()['error']
    assert call(env, 'get', '/api/tasks?lead=me').status_code == 400   # 网页会话
    assert call(env, 'get', '/api/tasks?lead=', token=t1['token']).get_json()['queued'] == 3


def test_last_seen_throttled_one_write(env):
    """验收 #12：连发两个请求，lastSeenAt 非 0，写盘 ≤ 1 次。"""
    login(env.client)
    leadId, tok = newLead(env, '甲'), newToken(env, 'T1')
    assert bind(env, leadId, tok['id']).status_code == 200
    writes = []
    realSave = leads_store._save
    env.monkeypatch.setattr(leads_store, '_save',
                            lambda data: (writes.append(1), realSave(data)))
    leads_store._seenAt.clear()
    assert call(env, 'get', '/api/agent/lead', token=tok['token']).status_code == 200
    assert call(env, 'get', '/api/agent/lead', token=tok['token']).status_code == 200
    assert len(writes) == 1
    assert leads_store.get(leadId)['lastSeenAt'] > 0
