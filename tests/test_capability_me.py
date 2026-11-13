# -*- coding: utf-8 -*-
"""GET /api/capability/me — 「我有什么权限」在网页里第一次可见（批次 L）。

5 条端点测试全部真发 HTTP 请求（test_client）:
    未登录 → 401 JSON（与现有 /api/* 同口径，不新造一种）;
    Cookie 会话 → human、规则含 *@*（来源 human-default）、示例判定全 allow;
    X-Agent-Token(scopes=["share"]) → agent、task:create deny、share@node:local allow;
    设备令牌（真走 /api/agent/device/login 换发）→ device;
    联邦令牌 → remote。
另覆盖: 规则带来源（capabilities / scope:<名> / default-full）、固定 6 条
问句（远端 node 取第一台远端；无远端只问本机 4 条）、decisions 与
capability.decide 同源、任意 query 入参被忽略（不变成通用判定接口）。
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import capability, device_auth

PASSWORD = 'pw-123456'
TOKEN_SHARE = 'tok-share-' + 's' * 24
SHARE_HASH = hashlib.sha256(TOKEN_SHARE.encode()).hexdigest()
TOKEN_FULL = 'tok-full-' + 'f' * 24
FULL_HASH = hashlib.sha256(TOKEN_FULL.encode()).hexdigest()
FED_TOKEN = 'fed-' + 'f' * 24
REMOTE = {'id': 'r1', 'name': 'win4710', 'base': 'http://10.126.126.3:6789',
          'token': '', 'terminal': True, 'apps': [], 'files': False,
          'filesWrite': False, 'models': [], 'model': ''}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'getFederationToken', lambda: FED_TOKEN)
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'share', 'name': '手机分享', 'tokenHash': SHARE_HASH,
         'scopes': ['share']},
        {'id': 'full', 'name': '全权智能体', 'tokenHash': FULL_HASH}])
    monkeypatch.setattr(configMod, 'getRemotes', lambda: [REMOTE])
    device_auth._resetForTests()
    configMod._cache.update(mtime=None, data=None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    yield SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                          app=app, monkeypatch=monkeypatch)
    configMod._cache.update(mtime=None, data=None)


def me(client, headers=None):
    return client.get('/api/capability/me', headers=headers or {})


def decisionOf(body, action, resource):
    for d in body['decisions']:
        if d['action'] == action and d['resource'] == resource:
            return d
    return None


# ---------------------------------------------------------------- 5 条端点测试

def test_unauthenticated_401(env):
    """未登录: 与现有 /api/* 同口径的 401 JSON, 不重定向、不新造一种。"""
    resp = me(env.fresh)
    assert resp.status_code == 401
    assert resp.get_json()['error'] == 'unauthorized'
    # 坏令牌同样 401（令牌通道未命中 → 落到统一 401）
    assert me(env.fresh, {'X-Agent-Token': 'bad'}).status_code == 401


def test_cookie_session_is_human(env):
    env.client.post('/login', data={'password': PASSWORD})
    resp = me(env.client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['principal']['kind'] == 'human'
    assert body['principal']['name']          # 操作系统登录名, 不空
    star = [r for r in body['rules']
            if r['action'] == '*' and r['resource'] == '*']
    assert star and star[0]['source'] == 'human-default'
    assert len(body['decisions']) == 6
    assert all(d['verdict'] == 'allow' for d in body['decisions'])


def test_share_token_is_agent(env):
    resp = me(env.fresh, {'X-Agent-Token': TOKEN_SHARE})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['principal'] == {'kind': 'agent', 'name': '手机分享'}
    assert body['rules'] == [
        {'action': 'share', 'resource': 'node:local', 'scope': 'any',
         'source': 'scope:share'}]
    assert decisionOf(body, 'task:create', 'node:local')['verdict'] == 'deny'
    assert decisionOf(body, 'share', 'node:local')['verdict'] == 'allow'
    assert decisionOf(body, 'exec', 'node:local')['verdict'] == 'deny'


def test_device_token_is_device(env):
    """设备令牌真走一遍换发: App 密码登录 → 拿令牌 → 打 /me。"""
    login = env.fresh.post('/api/agent/device/login',
                           data=json.dumps({'password': PASSWORD,
                                            'device': '小米手机'}),
                           content_type='application/json')
    assert login.status_code == 200
    token = login.get_json()['token']
    resp = me(env.fresh, {'X-Agent-Token': token})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['principal'] == {'kind': 'device', 'name': '小米手机'}
    assert body['rules'] == [
        {'action': 'upload', 'resource': 'node:local', 'scope': 'any',
         'source': 'scope:upload'},
        {'action': 'share', 'resource': 'node:local', 'scope': 'any',
         'source': 'scope:share'}]
    assert decisionOf(body, 'share', 'node:local')['verdict'] == 'allow'
    assert decisionOf(body, 'task:create', 'node:local')['verdict'] == 'deny'


def test_federation_token_is_remote(env):
    resp = me(env.fresh, {'X-Deskport-Token': FED_TOKEN})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['principal']['kind'] == 'remote'
    assert body['rules'] == [
        {'action': '*', 'resource': '*', 'scope': 'any', 'source': 'scope:full'}]
    assert all(d['verdict'] == 'allow' for d in body['decisions'])


# ---------------------------------------------------------------- 形状与边界

def test_questions_fixed_six_with_first_remote(env):
    body = me(env.fresh, {'X-Agent-Token': TOKEN_FULL}).get_json()
    pairs = [(d['action'], d['resource']) for d in body['decisions']]
    assert pairs == [
        ('task:create', 'node:local'),
        ('share', 'node:local'),
        ('exec', 'node:local'),
        ('file:write', 'node:win4710'),
        ('file:read', 'node:win4710'),
        ('config:write', 'node:local'),
    ]
    assert all(d['verdict'] == 'allow' for d in body['decisions'])
    # 没写 scopes = 全权: 规则来源记 default-full（向后兼容）
    assert body['rules'] == [
        {'action': '*', 'resource': '*', 'scope': 'any', 'source': 'default-full'}]


def test_questions_local_only_without_remotes(env):
    env.monkeypatch.setattr(configMod, 'getRemotes', lambda: [])
    configMod._cache.update(mtime=None, data=None)
    body = me(env.fresh, {'X-Agent-Token': TOKEN_FULL}).get_json()
    pairs = [(d['action'], d['resource']) for d in body['decisions']]
    assert pairs == [
        ('task:create', 'node:local'),
        ('share', 'node:local'),
        ('exec', 'node:local'),
        ('config:write', 'node:local'),
    ]


def test_decisions_same_source_as_decide(env):
    """decisions 必须是 capability.decide 对同一份规则的判定, 不是另一套。"""
    from modules import capability_api
    for headers in ({'X-Agent-Token': TOKEN_SHARE},
                    {'X-Agent-Token': TOKEN_FULL},
                    {'X-Deskport-Token': FED_TOKEN}):
        body = me(env.fresh, headers).get_json()
        principal = (body['principal']['kind'], body['principal']['name'])
        for d in body['decisions']:
            verdict, _why = capability.decide(
                body['rules'], principal, d['action'], d['resource'])
            assert verdict == d['verdict'], (headers, d)
        assert capability_api.exampleQuestions() == [
            (d['action'], d['resource']) for d in body['decisions']]


def test_query_params_do_not_become_generic_query(env):
    """只读自述: 任意 query 入参被忽略, 不变成通用判定接口。"""
    plain = me(env.fresh, {'X-Agent-Token': TOKEN_SHARE}).get_json()
    injected = env.fresh.get(
        '/api/capability/me?action=exec&resource=node:local&verdict=allow',
        headers={'X-Agent-Token': TOKEN_SHARE}).get_json()
    assert injected == plain


def test_capabilities_field_source(env, monkeypatch):
    """capabilities（新字段）优先于 scopes, 来源逐条记 capabilities。"""
    caps = [{'action': 'upload', 'resource': 'node:*'},
            {'action': 'file:write', 'resource': 'path:E:/共享'}]
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'c', 'name': '新字段令牌', 'tokenHash': FULL_HASH,
         'scopes': ['share'], 'capabilities': caps}])
    body = me(env.fresh, {'X-Agent-Token': TOKEN_FULL}).get_json()
    assert [(r['action'], r['resource'], r['source']) for r in body['rules']] == [
        ('upload', 'node:*', 'capabilities'),
        ('file:write', 'path:E:/共享', 'capabilities')]
    # 固定 6 条里没有 upload, 规则效果直接对同一份规则表问 decide
    from modules import capability
    principal = (body['principal']['kind'], body['principal']['name'])
    assert capability.decide(
        body['rules'], principal, 'upload', 'node:win4710')[0] == 'allow'
    assert capability.decide(
        body['rules'], principal, 'share', 'node:local')[0] == 'deny'


def test_empty_rules_all_deny(env, monkeypatch):
    """畸形/空规则 = 一律拒绝（capability 的既有语义, 不在这里放宽）。"""
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'x', 'name': '空规则令牌', 'tokenHash': FULL_HASH,
         'capabilities': [{'action': '', 'resource': '*'}, 'x']}])
    body = me(env.fresh, {'X-Agent-Token': TOKEN_FULL}).get_json()
    assert body['rules'] == []
    assert all(d['verdict'] == 'deny' for d in body['decisions'])
