# -*- coding: utf-8 -*-
"""批次 SR：外部智能体自助成为主管（POST /api/agent/lead + CLI lead --register）。

规格「例子」表 1–6 全收：全权未绑定令牌注册 201 / 同名再调 200 / 换名 409 /
受限令牌 403 / 名字被占 409 / 注册后建任务记 lead（例 7 的 CLI 真跑在
tests/test_deskport_lead_cli.py）；
另收规格正文：联邦与设备令牌 403、审计 lead.self_registered、入参校验、
悬空绑定 409、retired 名字可回收。追加一条：注册成功时认领该令牌之前建的、
lead 为空的任务（未结束的全认，已结束的只认最近 7 天内），claimed: N。
全部桩化，不碰真实主机与真实配置。
"""
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules import worklines_store
from modules.tasks_store import TaskRepository
from modules.worklines_store import WorklineRepository

PASSWORD = 'pw-123456'
LEAD_VIEW = {'id', 'name', 'project', 'kind', 'parent', 'status'}


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


def call(env, method, path, body=None, token=None, headers=None):
    client = env.fresh if (token or headers) else env.client
    kw = {'headers': dict(headers or {})}
    if token:
        kw['headers']['X-Agent-Token'] = token
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(client, method)(path, **kw)


def newToken(env, name='T1', scopes=None):
    body = {'name': name}
    if scopes is not None:
        body['scopes'] = scopes
    resp = call(env, 'post', '/api/settings/agent-tokens', body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def register(env, token, name, project=None, headers=None):
    body = {'name': name}
    if project is not None:
        body['project'] = project
    return call(env, 'post', '/api/agent/lead', body, token=token, headers=headers)


def leadsOf(env):
    return call(env, 'get', '/api/settings/leads').get_json()['leads']


def auditOf(env, action):
    return [a for a in env.audit if a['action'] == action]


def task(env, token):
    return call(env, 'post', '/api/tasks', {'prompt': 'x'}, token=token)


# ---------------------------------------------------------------- 规格「例子」表

def test_register_creates_lead_and_binds_calling_token(env):
    """例 1：全权未绑定令牌 register "A" → 201；自查返回 A；设置页 tokens 含它。"""
    login(env.client)
    tok = newToken(env, 'T1')
    resp = register(env, tok['token'], 'A', project='课题')
    assert resp.status_code == 201
    lead = resp.get_json()['lead']
    assert set(lead) == LEAD_VIEW
    assert lead['name'] == 'A' and lead['project'] == '课题'
    assert lead['kind'] == 'external' and lead['parent'] == 'human'
    assert lead['status'] == 'active' and lead['id'].startswith('ld_')
    got = call(env, 'get', '/api/agent/lead', token=tok['token']).get_json()
    assert got['lead'] == lead
    row = [l for l in leadsOf(env) if l['id'] == lead['id']][0]
    assert row['tokens'] == ['T1'] and row['kind'] == 'external'
    stored = json.loads((env.tmp / 'config.json').read_text('utf-8'))
    assert stored['agentTokens'][0]['lead'] == lead['id']


def test_register_same_name_is_idempotent(env):
    """例 2：同一令牌再 register "A" → 200，返回同一记录。"""
    login(env.client)
    tok = newToken(env, 'T1')
    first = register(env, tok['token'], 'A').get_json()['lead']
    resp = register(env, tok['token'], 'A')
    assert resp.status_code == 200
    assert resp.get_json()['lead'] == first
    assert len(leadsOf(env)) == 1


def test_register_other_name_conflicts(env):
    """例 3：同一令牌 register "B"（已绑 A）→ 409 并带回已绑主管。"""
    login(env.client)
    tok = newToken(env, 'T1')
    first = register(env, tok['token'], 'A').get_json()['lead']
    resp = register(env, tok['token'], 'B')
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['lead']['id'] == first['id'] and 'A' in body['error']
    assert call(env, 'get', '/api/agent/lead', token=tok['token']).get_json()['lead'] == first


def test_restricted_tokens_are_denied(env):
    """例 4：upload/share 档令牌 register → 403 {"error","need":"full"}。"""
    login(env.client)
    for scopes in (['upload', 'share'], ['share']):   # 设置页只认这两档受限
        tok = newToken(env, 'R-%s' % '-'.join(scopes), scopes=scopes)
        resp = register(env, tok['token'], 'A')
        assert resp.status_code == 403
        assert resp.get_json() == {'error': '令牌无此权限', 'need': 'full'}
    assert leadsOf(env) == []
    assert auditOf(env, 'agent.scope')


def test_name_taken_by_active_lead_conflicts(env):
    """例 5：另一个全权令牌 register "A"（A 已存在且未停用）→ 409。"""
    login(env.client)
    tokA = newToken(env, 'T1')
    lead = register(env, tokA['token'], 'A').get_json()['lead']
    tokB = newToken(env, 'T2')
    resp = register(env, tokB['token'], 'A')
    assert resp.status_code == 409 and '同名' in resp.get_json()['error']
    assert [l['id'] for l in leadsOf(env)] == [lead['id']]
    assert call(env, 'get', '/api/agent/lead', token=tokB['token']).get_json() == {'lead': None}


def test_task_created_after_register_records_lead(env):
    """例 6：register 后用该令牌建任务 → task.lead == A.id。"""
    login(env.client)
    tok = newToken(env, 'T1')
    lead = register(env, tok['token'], 'A').get_json()['lead']
    created = task(env, tok['token']).get_json()['task']
    assert created['lead'] == lead['id']
    detail = call(env, 'get', '/api/tasks/%s' % created['id'],
                  token=tok['token']).get_json()
    assert detail['task']['lead'] == lead['id']


# ---------------------------------------------------------------- 追加：认领历史任务

def test_register_claims_previous_own_tasks(env):
    """追加：注册前用该令牌建的 2 个任务被认领；别的令牌建的不受影响。"""
    login(env.client)
    tok, other = newToken(env, 'T1'), newToken(env, 'T2')
    mine = [task(env, tok['token']).get_json()['task']['id'] for _ in range(2)]
    theirs = task(env, other['token']).get_json()['task']['id']
    for tid in mine + [theirs]:
        assert env.repo.get(tid)['lead'] == ''
    resp = register(env, tok['token'], 'A')
    assert resp.status_code == 201
    lead = resp.get_json()['lead']
    assert resp.get_json()['claimed'] == 2
    for tid in mine:
        assert env.repo.get(tid)['lead'] == lead['id']
    assert env.repo.get(theirs)['lead'] == ''
    rows = auditOf(env, 'lead.self_registered')
    assert 'claimed=2' in rows[0]['detail']


def test_claim_window_only_recent_ended(env):
    """追加：未结束的必认领；closed/cancelled 只认最近 7 天内结束的。"""
    login(env.client)
    tok = newToken(env, 'T1')
    open1 = task(env, tok['token']).get_json()['task']['id']
    now = time.time()
    seeded = [
        ('closed-new', 'closed', now - 3 * 86400),      # 7 天内关闭：认
        ('closed-old', 'closed', now - 10 * 86400),     # 关了很久：不认
        ('cancel-old', 'cancelled', now - 30 * 86400),  # 取消已久：不认
    ]
    for tid, status, ended in seeded:
        env.repo.put({'id': tid, 'title': tid, 'prompt': 'x', 'owner': 'T1',
                      'status': status, 'lastActiveAt': ended,
                      'endedAt': ended, 'createdAt': ended - 86400,
                      'lead': '', 'events': [], 'seq': 1})
    env.repo.save()
    resp = register(env, tok['token'], 'A')
    lead = resp.get_json()['lead']
    assert resp.get_json()['claimed'] == 2      # 未结束 1 + 7 天内关闭 1
    assert env.repo.get(open1)['lead'] == lead['id']
    assert env.repo.get('closed-new')['lead'] == lead['id']
    assert env.repo.get('closed-old')['lead'] == ''
    assert env.repo.get('cancel-old')['lead'] == ''


def test_register_without_prior_tasks_claims_zero(env):
    """追加：没有历史任务时 claimed=0，键仍在；审计记 claimed=0。"""
    login(env.client)
    tok = newToken(env, 'T1')
    body = register(env, tok['token'], 'A').get_json()
    assert body['claimed'] == 0 and body['lead']['name'] == 'A'
    assert 'claimed=0' in auditOf(env, 'lead.self_registered')[0]['detail']


# ---------------------------------------------------------------- 规格正文补充

def test_federation_and_device_tokens_are_denied(env):
    """联邦令牌、设备令牌 register → 403（不是 agentTokens 里的条目）。"""
    login(env.client)
    env.monkeypatch.setattr(configMod, 'getFederationToken', lambda: 'fed-' + 'f' * 24)
    fed = register(env, None, 'A', headers={'X-Deskport-Token': 'fed-' + 'f' * 24})
    assert fed.status_code == 403 and '联邦' in fed.get_json()['error']
    dev = env.fresh.post('/api/agent/device/login',
                         data=json.dumps({'device': '手机', 'password': PASSWORD}),
                         content_type='application/json')
    assert dev.status_code == 200, dev.get_json()
    resp = register(env, dev.get_json()['token'], 'A')
    assert resp.status_code == 403
    assert leadsOf(env) == []


def test_audit_self_registered(env):
    """审计 lead.self_registered：target 是主管名，detail 带令牌名/主管 id/名字。"""
    login(env.client)
    tok = newToken(env, 'T1')
    lead = register(env, tok['token'], 'A').get_json()['lead']
    rows = auditOf(env, 'lead.self_registered')
    assert len(rows) == 1
    assert rows[0]['target'] == 'A'
    assert 'T1' in rows[0]['detail'] and lead['id'] in rows[0]['detail']
    assert register(env, tok['token'], 'A').status_code == 200
    assert len(auditOf(env, 'lead.self_registered')) == 1   # 幂等调用不重复记


def test_register_validation(env):
    """入参校验：坏名字/坏 project → 400；无令牌 401；坏 JSON 400。"""
    login(env.client)
    tok = newToken(env, 'T1')
    for bad in ('', '   ', 'x' * 25, 'a<b', 'a\nb', 123):
        resp = register(env, tok['token'], bad)
        assert resp.status_code == 400 and 'name' in resp.get_json()['error']
    assert register(env, tok['token'], 'A', project='x' * 41).status_code == 400
    assert 'project' in register(env, tok['token'], 'A',
                                 project='x' * 41).get_json()['error']
    assert leadsOf(env) == []
    resp = env.fresh.post('/api/agent/lead', data=json.dumps({'name': 'A'}),
                          content_type='application/json')
    assert resp.status_code == 401
    resp = env.fresh.post('/api/agent/lead', headers={'X-Agent-Token': tok['token']},
                          data='[1,2]', content_type='application/json')
    assert resp.status_code == 400
    resp = env.fresh.post('/api/agent/lead', headers={'X-Agent-Token': tok['token']})
    assert resp.status_code == 400       # 空体：name 缺失


def test_dangling_binding_conflicts(env):
    """令牌绑到不存在的主管（悬空）：注册 409 说清，不静默改写绑定。"""
    login(env.client)
    tok = newToken(env, 'T1')
    cfg = json.loads((env.tmp / 'config.json').read_text('utf-8'))
    cfg['agentTokens'][0]['lead'] = 'ld_deadbeef'
    (env.tmp / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
    configMod._cache.update(mtime=None, data=None)
    resp = register(env, tok['token'], 'A')
    assert resp.status_code == 409 and '悬空' in resp.get_json()['error']
    assert leadsOf(env) == []


def test_retired_lead_name_is_recycled(env):
    """retired 的名字可回收：停用后另一个令牌可用同名注册。"""
    login(env.client)
    tokA = newToken(env, 'T1')
    lead = register(env, tokA['token'], 'A').get_json()['lead']
    assert call(env, 'patch', '/api/settings/leads/' + lead['id'],
                {'status': 'retired'}).status_code == 200
    tokB = newToken(env, 'T2')
    resp = register(env, tokB['token'], 'A')
    assert resp.status_code == 201
    assert resp.get_json()['lead']['id'] != lead['id']
    assert call(env, 'get', '/api/agent/lead', token=tokB['token']
                ).get_json()['lead']['id'] == resp.get_json()['lead']['id']
