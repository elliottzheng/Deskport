# -*- coding: utf-8 -*-
"""capability 判定入口（批次 H）：段匹配、node:local vs node:*、路径前缀与
大小写、空规则=拒绝、老 scope 映射逐字等价、on_behalf_of 记录。"""
import os

import pytest

from modules import agent_api, capability, events


def _legacyHasScope(agent, scope):
    """改接前 agent_api.agentHasScope 的逻辑（等价性对照的 oracle）。"""
    scopes = agent.get('scopes')
    if not isinstance(scopes, list) or not scopes:
        return True
    names = [str(s).strip() for s in scopes if str(s).strip()]
    return scope in names or 'full' in names


SCOPES = ('full', 'upload', 'share', 'upload_remote')

# 系统签发路径能造出来的全部令牌形态（设备令牌/设置页/联邦合成条目），
# 外加畸形但老逻辑有定义的输入。注意: 带 upload_remote 而不带 upload 的
# 形状不在表内——设备令牌必含 upload、设置页只认两档、联邦条目是 full，
# 那种形状只有手改 config 才造得出，见分歧专项测试。
REACHABLE_CONFIGS = [
    {},                                     # 没写 scopes = 全权
    {'scopes': []},                         # 空列表 = 全权
    {'scopes': ['full']},
    {'scopes': ['upload']},
    {'scopes': ['share']},
    {'scopes': ['upload', 'share']},        # 设备令牌默认档
    {'scopes': ['upload', 'share', 'upload_remote']},   # 勾了远端投递
    {'scopes': ['bogus']},                  # 不认识的 scope 名
    {'scopes': [' full ']},                 # 老逻辑会 strip
    {'scopes': 'upload'},                   # 非列表 = 全权（老逻辑）
    {'scopes': ['upload', 'upload']},       # 重复项
]


# ---------------------------------------------------------------- 匹配语义

def test_star_matches_exactly_one_segment():
    rules = [{'action': 'task:*', 'resource': '*'}]
    p = ('agent', 'x')
    assert capability.decide(rules, p, 'task:create', 'node:local')[0] == 'allow'
    assert capability.decide(rules, p, 'task:send', 'node:local')[0] == 'allow'
    # 只匹配一段：三段动作不中两段规则，单段动作也不中 task:*
    assert capability.decide(rules, p, 'task:create:deep', 'node:local')[0] == 'deny'
    assert capability.decide(rules, p, 'exec', 'node:local')[0] == 'deny'
    # 整串 '*' 匹配任意值：想全开就 action='*', resource='*'
    both = [{'action': '*', 'resource': '*'}]
    assert capability.decide(both, p, 'task:create:deep', 'path:C:\\x')[0] == 'allow'
    # node:* 只匹配一段，不跨到 node:local:x
    nodeStar = [{'action': '*', 'resource': 'node:*'}]
    assert capability.decide(nodeStar, p, 'upload', 'node:local')[0] == 'allow'
    assert capability.decide(nodeStar, p, 'upload', 'node:ai-xmyz')[0] == 'allow'
    assert capability.decide(nodeStar, p, 'upload', 'node:local:x')[0] == 'deny'


def test_node_local_is_not_node_star():
    local = capability.compile([{'action': 'upload', 'resource': 'node:local'}])
    star = capability.compile([{'action': 'upload', 'resource': 'node:*'}])
    p = ('agent', 'x')
    assert local.decide(p, 'upload', 'node:local')[0] == 'allow'
    assert local.decide(p, 'upload', 'node:ai-xmyz')[0] == 'deny'   # 不被 * 悄悄放宽
    assert local.decide(p, 'share', 'node:local')[0] == 'deny'      # 动作不符
    assert star.decide(p, 'upload', 'node:local')[0] == 'allow'
    assert star.decide(p, 'upload', 'node:ai-xmyz')[0] == 'allow'
    assert star.decide(p, 'share', 'node:ai-xmyz')[0] == 'deny'


def test_path_prefix_and_boundaries():
    rules = [{'action': 'file:write', 'resource': 'path:E:\\工作区'}]
    p = ('remote', 'B机')
    assert capability.decide(rules, p, 'file:write', 'path:E:\\工作区')[0] == 'allow'
    assert capability.decide(rules, p, 'file:write', 'path:E:\\工作区\\a.txt')[0] == 'allow'
    assert capability.decide(rules, p, 'file:write',
                             'path:E:\\工作区\\子\\b.txt')[0] == 'allow'
    # 前缀边界：同级目录不算根下
    assert capability.decide(rules, p, 'file:write',
                             'path:E:\\工作区2\\a.txt')[0] == 'deny'
    # .. 折叠后出界
    assert capability.decide(rules, p, 'file:write',
                             'path:E:\\工作区\\..\\evil.txt')[0] == 'deny'
    # glob 元字符按字面，不悄悄放宽
    meta = [{'action': 'file:write', 'resource': 'path:E:\\a?b'}]
    assert capability.decide(meta, p, 'file:write', 'path:E:\\a?b\\x.txt')[0] == 'allow'
    assert capability.decide(meta, p, 'file:write', 'path:E:\\aXb\\x.txt')[0] == 'deny'


@pytest.mark.skipif(os.name != 'nt', reason='Windows normcase 大小写不敏感')
def test_path_case_insensitive_on_windows():
    rules = [{'action': 'file:write', 'resource': 'path:E:\\Root'}]
    p = ('remote', 'B机')
    assert capability.decide(rules, p, 'file:write', 'path:e:\\root\\X.TXT')[0] == 'allow'


def test_empty_rules_deny_everything():
    verdict, why = capability.decide([], ('agent', 'x'), 'upload', 'node:local')
    assert verdict == 'deny' and why == '无规则'
    assert capability.decide(None, ('agent', 'x'), 'upload', 'node:local')[0] == 'deny'


def test_malformed_rules_are_dropped():
    rules = [{'action': '', 'resource': '*'}, {'resource': 'node:local'},
             {'action': 'upload', 'resource': 'node:local', 'scope': 'sometimes'},
             'nope', {'action': 'upload', 'resource': 'node:local'}]
    assert capability.decide(rules, ('agent', 'x'), 'upload', 'node:local')[0] == 'allow'
    assert capability.decide(rules, ('agent', 'x'), 'share', 'node:local')[0] == 'deny'
    # 全是畸形 = 空规则 = 拒绝
    assert capability.decide([{'action': '', 'resource': '*'}],
                             ('agent', 'x'), 'upload', 'node:local')[0] == 'deny'


def test_principal_must_be_a_known_kind():
    rules = [{'action': '*', 'resource': '*'}]
    for kind in capability.PRINCIPAL_KINDS:
        assert capability.decide(rules, (kind, ' whoever '), 'exec',
                                 'node:local')[0] == 'allow'
    assert capability.decide(rules, ('service', 'x'), 'exec', 'node:local')[0] == 'deny'
    assert capability.decide(rules, 'agent:x', 'exec', 'node:local')[0] == 'deny'
    assert capability.decide(rules, ('agent',), 'exec', 'node:local')[0] == 'deny'


def test_decide_accepts_raw_or_compiled_and_reports_reason():
    rules = [{'action': 'upload', 'resource': 'node:local'}]
    compiled = capability.compile(rules)
    assert capability.decide(compiled, ('agent', 'x'), 'upload', 'node:local') == \
        capability.decide(rules, ('agent', 'x'), 'upload', 'node:local')
    verdict, why = capability.decide(rules, ('agent', 'x'), 'upload', 'node:local')
    assert verdict == 'allow' and 'upload@node:local' in why
    verdict, why = capability.decide(rules, ('agent', 'x'), 'upload', 'node:h1')
    assert verdict == 'deny' and why == '无匹配规则'
    assert capability.decide(rules, ('agent', 'x'), '', 'node:local')[0] == 'deny'


# ---------------------------------------------------------------- 老 scope 桥

def test_scope_mapping_is_verbatim():
    assert capability.SCOPE_CAPABILITIES['full'] == \
        [{'action': '*', 'resource': '*'}]
    assert capability.SCOPE_CAPABILITIES['upload'] == \
        [{'action': 'upload', 'resource': 'node:local'}]
    assert capability.SCOPE_CAPABILITIES['share'] == \
        [{'action': 'share', 'resource': 'node:local'}]
    assert capability.SCOPE_CAPABILITIES['upload_remote'] == \
        [{'action': 'upload', 'resource': 'node:*'}]


@pytest.mark.parametrize('entry', REACHABLE_CONFIGS)
def test_token_allows_matches_legacy_semantics(entry):
    """系统签发路径能造出的每种令牌 × 每个 scope 门，与改接前的
    agentHasScope 逐字等价（oracle 是上头的 _legacyHasScope）。"""
    for scope in SCOPES:
        assert capability.tokenAllows(entry, scope) == \
            _legacyHasScope(entry, scope), (entry, scope)


@pytest.mark.parametrize('entry', REACHABLE_CONFIGS)
def test_agent_api_has_scope_matches_legacy_semantics(entry):
    """agent_api.agentHasScope 改接后走 capability，结论不变。"""
    for scope in SCOPES:
        assert agent_api.agentHasScope(entry, scope) == \
            _legacyHasScope(entry, scope), (entry, scope)


def test_upload_remote_without_upload_is_the_single_documented_divergence():
    """唯一的行为差，且只在手改 config 才造得出（设备令牌必含 upload、
    设置页只认两档、联邦合成条目是 full；批次 B2 起 upload_remote 就是叠在
    share 上的第三层，从不单独签发）：带 upload_remote 而不带 upload 的
    令牌，老语义本机上传 403，新语义放行——upload_remote = upload@node:*
    的规则字面含义（node:* 含 node:local）。其余每个门逐字不变。"""
    for entry in ({'scopes': ['upload_remote']},
                  {'scopes': ['share', 'upload_remote']}):
        assert _legacyHasScope(entry, 'upload') is False
        assert capability.tokenAllows(entry, 'upload') is True
        for scope in ('full', 'share', 'upload_remote'):
            assert capability.tokenAllows(entry, scope) == \
                _legacyHasScope(entry, scope), (entry, scope)


def test_capabilities_field_wins_over_scopes():
    entry = {'scopes': ['upload'],
             'capabilities': [{'action': 'share', 'resource': 'node:local'}]}
    assert capability.tokenAllows(entry, 'share') is True
    assert capability.tokenAllows(entry, 'upload') is False   # 不回落 scopes
    # 畸形项被丢弃；空列表 = 什么都不允许
    assert capability.tokenAllows(
        {'capabilities': [{'action': 'share'}, 'x']}, 'share') is False
    assert capability.tokenAllows({'capabilities': []}, 'full') is False


def test_gate_queries_are_shaped_by_the_rules():
    # full 门要 *@* 才满足：只有 node 维度全开的规则不够
    rules = [{'action': '*', 'resource': 'node:local'}]
    assert capability.decide(rules, ('agent', 'x'), '*', '*')[0] == 'deny'
    # upload 与 upload_remote 互不满足
    assert capability.tokenAllows({'scopes': ['upload']}, 'upload_remote') is False
    assert capability.tokenAllows({'scopes': ['upload_remote']},
                                  'upload_remote') is True


def test_principal_of_maps_entry_kinds():
    assert capability.principalOf({'kind': 'device', 'name': '小米'}) == \
        ('device', '小米')
    assert capability.principalOf({'kind': 'federation', 'name': '联邦:A'}) == \
        ('remote', '联邦:A')
    assert capability.principalOf({'name': 'kimi'}) == ('agent', 'kimi')
    assert capability.principalOf(None) == ('agent', '')
    assert capability.scopeQuery('full') == ('*', '*')
    assert capability.scopeQuery('bogus') is None


# ---------------------------------------------------------------- on_behalf_of

def test_events_on_behalf_of_defaults_to_empty(tmp_path, monkeypatch):
    target = str(tmp_path / 'audit.jsonl')
    monkeypatch.setattr(events, 'eventPath', lambda: target)
    events.log('unit.write', 't', 'ok', 'd', '127.0.0.1')
    events.log('unit.write2', 't', 'ok', 'd', '127.0.0.1', on_behalf_of='张三')
    rows = events.recent(10)
    assert rows[0]['on_behalf_of'] == ''
    assert rows[1]['on_behalf_of'] == '张三'


def test_audit_agent_action_records_on_behalf_of(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(events, 'log',
                        lambda action, target='', result='ok', detail='',
                        ip='', **kw: seen.append((action, detail, kw)))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    with app.test_request_context('/api/agent/hosts'):
        agent_api.auditAgentAction({'name': 'kimi', 'onBehalfOf': '张三'},
                                   'agent.exec', 'local', 'ok', 'echo hi')
        agent_api.auditAgentAction({'name': 'kimi'}, 'agent.exec', 'local',
                                   'ok', 'echo hi')
    assert seen[0][0] == 'agent.exec' and seen[0][1] == '[kimi] echo hi'
    assert seen[0][2] == {'on_behalf_of': '张三'}
    assert seen[1][2] == {}    # 没有 onBehalfOf 就不传，老形状逐字不变
