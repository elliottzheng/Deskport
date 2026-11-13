# -*- coding: utf-8 -*-
"""权限判定唯一入口 — Principal × action × resource → allow / deny（批次 H）。

同一句「谁能在这个地方做这件事」曾被五处配置各答一遍：agentTokens scopes、
设备令牌 scopes、remotes[].filesWrite、federationRoots、task.policy。本模块把
这类判定收成一个纯函数入口：调用方把自己的配置降级成规则表喂进来，decide 只做
匹配——不 import flask、不 import config（好测、无加载环）。

主体:
    Principal = (kind, name)，kind ∈ {'human','agent','device','remote'}。
    本批 principal 只作记录与校验，不参与匹配（规则由调用方按主体取好后
    传入）；「按主体查规则」留到可见/可撤阶段。

规则:
    Rule = {'action': str, 'resource': str, 'scope': 'self'|'any'（可选）}
    action   : 'upload' / 'share' / 'exec' / 'file:write' / 'task:create' / '*'
    resource : 'node:local' / 'node:*' / 'path:<目录>' / '*'

匹配语义（写死在这里，改之前先想后果）:
    1. '*' 只匹配**一段**：'node:*' 匹配 node:local 与 node:ai-xmyz，不匹配
       node:local:x（段数先对齐再逐段比）；整串就是 '*' 时匹配任意值——想全开
       就写 action='*', resource='*'，没有 '**' 这种任意深度通配。
    2. 'node:local' 与 'node:*' 是两回事：upload 只对本机有效，不会被 '*'
       悄悄放宽成所有执行点。
    3. 'path:' 走目录前缀（本实现在前缀与 fnmatch 之间选前缀）：value 等于根，
       或以「根 + 分隔符」开头，两侧都过 normcase + normpath（Windows 大小写
       不敏感，POSIX 大小写敏感）。选前缀是因为第一个消费者 federationRoots
       就是目录白名单，前缀正是既有 isUnder 的契约；目录名里的 glob 元字符
       （? * [）也就不会悄悄放宽匹配。
    4. 空规则（或全是畸形规则）= 一律拒绝。deny 是默认，allow 必须命中。
    5. 不做 ask：单任务审批（task.policy.allowPaths/allowCommands）语义不同，
       在 policy 层，不在这里。

老 scope 桥（阶段 1，全库只此一处，别散落）:
    scopes → capabilities 映射（rulesForToken 用）:
        full          → [{'action': '*', 'resource': '*'}]
        upload        → [{'action': 'upload', 'resource': 'node:local'}]
        share         → [{'action': 'share', 'resource': 'node:local'}]
        upload_remote → [{'action': 'upload', 'resource': 'node:*'}]
    老字段照读：有 capabilities 用它（scopes 的超集）；没有就按上表映射；
    两者都没有/为空 = 全权（向后兼容）。scope 门 → 能力查询（scopeQuery 用）:
        full → ('*', '*')、upload → ('upload', 'node:local')、
        share → ('share', 'node:local')、upload_remote → ('upload', 'node:*')。

    已知的一处行为差（规格给定映射的规则字面后果，只影响手改 config 才造
    得出的令牌——设备令牌必含 upload、设置页只认两档、联邦条目是 full）:
    带 upload_remote 而不带 upload 的令牌，老语义在本机上传门 403，新语义
    放行（node:* 含 node:local）。其余每个门与老白名单逐字等价，回归见
    tests/test_capability.py 的 oracle 对照。
"""
import os


PRINCIPAL_KINDS = ('human', 'agent', 'device', 'remote')
RULE_SCOPES = ('any', 'self')

# scopes → capabilities：老值映射（阶段 1 的桥）。值是不可变的语义契约，
# 调用方要改就复制一份，别就地改这些字典。
SCOPE_CAPABILITIES = {
    'full': [{'action': '*', 'resource': '*'}],
    'upload': [{'action': 'upload', 'resource': 'node:local'}],
    'share': [{'action': 'share', 'resource': 'node:local'}],
    'upload_remote': [{'action': 'upload', 'resource': 'node:*'}],
}

# scope 门 → 能力查询：每个老 scope 门问一句具体的能力（full 问"是否全开"，
# upload 问"能否往本机写"，upload_remote 问"能否往任意节点写"）。
SCOPE_QUERIES = {
    'full': ('*', '*'),
    'upload': ('upload', 'node:local'),
    'share': ('share', 'node:local'),
    'upload_remote': ('upload', 'node:*'),
}


def _matchSegments(pattern, value):
    """冒号分段匹配：段数先对齐，'*' 只顶一段；整串 '*' 匹配任意值。"""
    if pattern == '*':
        return True
    parts = pattern.split(':')
    target = value.split(':')
    if len(parts) != len(target):
        return False
    return all(p == '*' or p == t for p, t in zip(parts, target))


def pathUnder(root, target):
    """目录前缀匹配：等于根，或以「根 + 分隔符」开头；两侧 normcase +
    normpath（Windows 大小写不敏感，POSIX 大小写敏感）。federation_write
    .isUnder 与 'path:' 能力资源共用这一处实现（批次 H：匹配规则只许一个
    出处）；目录名里的 glob 元字符按字面，不悄悄放宽。"""
    r = os.path.normcase(os.path.normpath(root))
    t = os.path.normcase(os.path.normpath(target))
    if not r or not t or r == '.' or t == '.':
        return False
    if t == r:
        return True
    return t.startswith(r.rstrip('\\/') + os.sep)


def _matchResource(pattern, value):
    if pattern == '*':
        return True
    if pattern.startswith('path:'):
        if not value.startswith('path:'):
            return False
        return pathUnder(pattern[5:], value[5:])
    return _matchSegments(pattern, value)


class Compiled:
    """compile() 的产物：线性规则表（规则数 <50，别过度设计）。"""

    __slots__ = ('rules',)

    def __init__(self, rules):
        self.rules = tuple(rules)

    def decide(self, principal, action, resource):
        kind, _name = _principalParts(principal)
        if kind is None:
            return ('deny', '主体不合法: %r' % (principal,))
        act = str(action or '').strip()
        res = str(resource or '').strip()
        if not act or not res:
            return ('deny', '动作/资源为空')
        if not self.rules:
            return ('deny', '无规则')
        for idx, rule in enumerate(self.rules):
            if _matchSegments(rule[0], act) and _matchResource(rule[1], res):
                return ('allow', '规则%d %s@%s' % (idx, rule[0], rule[1]))
        return ('deny', '无匹配规则')


def _principalParts(principal):
    if isinstance(principal, (tuple, list)) and len(principal) == 2:
        kind = str(principal[0] or '').strip()
        if kind in PRINCIPAL_KINDS:
            return kind, str(principal[1] or '')
    return None, ''


def _validRule(rule):
    if not isinstance(rule, dict):
        return False
    if not str(rule.get('action') or '').strip():
        return False
    if not str(rule.get('resource') or '').strip():
        return False
    scope = rule.get('scope')
    if scope is not None and str(scope).strip() not in RULE_SCOPES:
        return False
    return True


def compile(rules):
    """rules → 内部结构。畸形规则（缺 action/resource、scope 不在白名单）
    当场丢弃——喂错配置的结果是「什么都不允许」，不是「什么都允许」。"""
    out = []
    for rule in rules or ():
        if not _validRule(rule):
            continue
        out.append((str(rule['action']).strip(), str(rule['resource']).strip(),
                    str(rule.get('scope') or 'any').strip()))
    return Compiled(out)


def decide(rules, principal, action, resource):
    """(rules, principal, action, resource) → ('allow'|'deny', reason)。

    rules 可以是 compile() 的产物，也可以是原始规则列表（内部现编）。
    不做 ask：ask 留给 policy 层。
    """
    if not isinstance(rules, Compiled):
        rules = compile(rules)
    return rules.decide(principal, action, resource)


# ---------------------------------------------------------------- 老 scope 桥

def rulesForToken(agent):
    """令牌条目 → 规则表（阶段 1 的桥，全库只此一处）。

    有 capabilities 字段就用它（新字段，scopes 的超集；畸形项丢弃）；
    没有就按 scopes 走 SCOPE_CAPABILITIES 映射（不认识的 scope 名不贡献
    规则）；两者都没有/为空 = 全权（向后兼容老行为）。

    规则从哪来（展示用）见 tokenRuleTable——判定逻辑只有这一份，
    调用方别照抄"capabilities 优先、再 scopes、都没有=全权"的判序。
    """
    return [rule for rule, _source in tokenRuleTable(agent)]


def tokenRuleTable(agent):
    """令牌条目 → [(rule, source)]：rulesForToken 的带来源版（同一处逻辑）。

    source 只说这条规则从哪来，不参与判定：
        'capabilities'  新字段（scopes 的超集）逐条；
        'scope:<名>'    老 scopes 经 SCOPE_CAPABILITIES 逐条映射；
        'default-full'  两者都没有/为空 = 全权（向后兼容）。
    消费者：GET /api/capability/me 的规则表展示（批次 L）。
    """
    entry = agent if isinstance(agent, dict) else {}
    caps = entry.get('capabilities')
    if isinstance(caps, list):
        return [(rule, 'capabilities') for rule in caps if _validRule(rule)]
    scopes = entry.get('scopes')
    if isinstance(scopes, list) and scopes:
        out = []
        for item in scopes:
            name = str(item).strip()
            for rule in SCOPE_CAPABILITIES.get(name, ()):
                out.append((rule, 'scope:' + name))
        return out
    return [(rule, 'default-full') for rule in SCOPE_CAPABILITIES['full']]


def scopeQuery(scope):
    """scope 门名 → 能力查询 (action, resource)；不认识的门返回 None
    （调用方按拒绝处理——fail closed）。"""
    return SCOPE_QUERIES.get(str(scope or '').strip())


_PRINCIPAL_KIND_BY_ENTRY = {'device': 'device', 'federation': 'remote'}


def principalOf(agent):
    """令牌条目 → Principal。kind: 设备令牌记 device，联邦令牌（别的
    Deskport）记 remote，其余（内置智能体令牌/缺省）记 agent。"""
    entry = agent if isinstance(agent, dict) else {}
    kind = _PRINCIPAL_KIND_BY_ENTRY.get(str(entry.get('kind') or '').strip(),
                                       'agent')
    return (kind, str(entry.get('name') or '').strip())


def tokenAllows(agent, scope):
    """令牌条目 + scope 门名 → 是否放行。agent_api.agentHasScope 的实现：
    老语义逐字保留（见模块 docstring 的查询表）。"""
    query = scopeQuery(scope)
    if query is None:
        return False
    verdict, _why = decide(rulesForToken(agent), principalOf(agent),
                           query[0], query[1])
    return verdict == 'allow'
