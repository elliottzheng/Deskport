# -*- coding: utf-8 -*-
"""capability 自述 — GET /api/capability/me（批次 L：我的权限第一次可见）。

只读端点：把当前主体与它生效的规则表、以及固定 6 条示例判定摊开给请求方。
网页设置页「我的权限」卡片与持令牌的程序共用这一个答案——规则是数据结构
（批次 H 的 capability.decide），不该只有代码看得见。

主体（capability.principalOf 的现有映射，不另写一套）:
    网页 Cookie 会话 = human；X-Agent-Token 内置令牌 = agent；
    设备令牌 = device；联邦令牌 = remote。

硬边界（规格给定，改之前先想后果）:
    - 只读、无入参: 没有"任意 (action, resource) 查询"入口——那等于给
      策略语言开后门；示例判定是服务端固定的 6 条；
    - human 的 *@* 是硬编码语义，不为此在 config 里存一份（收紧它是
      阶段 4 的事，见设计文档）；
    - 可见不可撤：撤销走设备/令牌页的吊销，本端点不改任何规则。
"""
import os

from flask import Blueprint, jsonify

import auth
import config
from modules import agent_api, capability

blueprint = Blueprint('capability', __name__)

# 人的默认规则（human 没有令牌条目）：*@*，来源标 human-default。
# 模块级常量，别就地改这些字典（要改先复制）。
HUMAN_RULES = [{'action': '*', 'resource': '*', 'scope': 'any',
                'source': 'human-default'}]

# 固定 6 条示例判定: 本机建任务 / 本机读文件 / 本机执行命令 / 远端写 /
# 远端读 / 配置写。远端 node 名取现有远端的第一台；没有远端就只问本机
# 那几条。写死在这里——让人一眼看懂规则的效果，不给任意查询留口子。
LOCAL_QUESTIONS = (
    ('task:create', 'node:local'),    # 本机建任务（/api/tasks* 组要 full）
    ('share', 'node:local'),          # 本机读文件/收文件（投暂存区、转发给执行方）
    ('exec', 'node:local'),           # 本机执行命令
)
REMOTE_QUESTIONS = (
    ('file:write', None),             # 远端写
    ('file:read', None),              # 远端读
)
TAIL_QUESTIONS = (
    ('config:write', 'node:local'),   # 配置写
)


def exampleQuestions():
    """固定示例判定问句 → [(action, resource)]；无远端时只问本机那几条。"""
    questions = list(LOCAL_QUESTIONS)
    remotes = config.getRemotes()
    if remotes:
        node = 'node:' + str(remotes[0].get('name') or remotes[0].get('id') or '')
        questions.extend((action, node) for action, _ in REMOTE_QUESTIONS)
    questions.extend(TAIL_QUESTIONS)
    return questions


def humanName():
    """人的名字: 单用户系统没有账户名，用操作系统登录名（审计里人记
    「面板」，这里给人看就用本人名字）；取不到退回「面板」。"""
    return os.environ.get('USERNAME', '').strip() or '面板'


def currentPrincipalAndRules():
    """当前主体 → (Principal, rules)；未认证返回 (None, None)（理论到不了:
    authGuard 已拦，这里只是 fail closed）。

    与 authGuard 同序: 带 Cookie 会话 = human（网页面板就是人自己）；
    否则走令牌通道（内置 → 设备 → 联邦，agent_api.agentFromRequest 的
    既定顺序，principalOf 给出 kind）。带会话又带令牌头的请求按会话认，
    与守卫对"已登录就放行"的处理一致。
    """
    if auth.isAuthenticated():
        return ('human', humanName()), HUMAN_RULES
    agent = agent_api.agentFromRequest()
    if agent is None:
        return None, None
    rules = [{'action': str(rule.get('action') or '').strip(),
              'resource': str(rule.get('resource') or '').strip(),
              'scope': str(rule.get('scope') or 'any').strip() or 'any',
              'source': source}
             for rule, source in capability.tokenRuleTable(agent)]
    return capability.principalOf(agent), rules


@blueprint.route('/api/capability/me', methods=['GET'])
def capabilityMe():
    """当前主体 + 生效规则表 + 固定 6 条示例判定（只读，忽略任何入参）。"""
    principal, rules = currentPrincipalAndRules()
    if principal is None:
        return jsonify({'error': 'unauthorized'}), 401
    compiled = capability.compile(rules)
    decisions = [{'action': action, 'resource': resource,
                  'verdict': compiled.decide(principal, action, resource)[0]}
                 for action, resource in exampleQuestions()]
    return jsonify({'principal': {'kind': principal[0], 'name': principal[1]},
                    'rules': rules, 'decisions': decisions})
