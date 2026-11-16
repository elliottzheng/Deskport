# -*- coding: utf-8 -*-
"""反馈状态机规则（规格 §5）：一张白名单表 + 谁能流转 + 什么时候必填原因。

纯函数、不碰存储——规则只有这一处定义，客户端镜像与共享测试数据都指向它
（规格 §5 末条）。落库与 history 写在 feedback_store / feedback_ops。

状态图（规格 §5）：

    submitted ──► acknowledged ──► processing ──► resolved ──► user_confirmed（终态）
                                       │   ▲          │
                                       ▼   │          └──（提交者不认账）──► processing
                                    rejected ─────────（重新打开）──────► processing

actor 两种：handler（处理者，本系统 = 网页会话主人）与 submitter（提交者本人）。
系统自动确认（resolved 14 天无人操作）不走本表，由 feedback_ops.sweepAutoConfirm
直接落 history，actor 记 system。
"""
import time

STATUSES = ('submitted', 'acknowledged', 'processing', 'resolved', 'rejected',
            'user_confirmed')
TERMINAL = 'user_confirmed'

# 合法单步流转（白名单之外一律 409）
TRANSITIONS = {
    'submitted': {'acknowledged'},
    'acknowledged': {'processing', 'rejected'},
    'processing': {'resolved', 'rejected'},
    'resolved': {'user_confirmed', 'processing'},
    'rejected': {'processing'},
    'user_confirmed': set(),
}

# 每条流转只能由谁发起（表里没有的 (from, to) 组合本来就不合法）
ACTORS = {
    ('submitted', 'acknowledged'): 'handler',
    ('acknowledged', 'processing'): 'handler',
    ('processing', 'resolved'): 'handler',
    ('acknowledged', 'rejected'): 'handler',
    ('processing', 'rejected'): 'handler',
    ('rejected', 'processing'): 'handler',
    ('resolved', 'user_confirmed'): 'submitter',
    ('resolved', 'processing'): 'submitter',
}

# 必填 reason 的流转（缺原因 400；非法流转 409——规格 §11.5）
REASON_REQUIRED = {
    ('processing', 'resolved'),
    ('acknowledged', 'rejected'),
    ('processing', 'rejected'),
    ('resolved', 'processing'),
}
ACTOR_TEXT = {'handler': '处理者', 'submitter': '提交者'}

# resolved 起 14 天提交者未操作，系统自动推进为 user_confirmed（规格 §5）
AUTO_CONFIRM_DAYS = 14
# 待受理超时线：submitted 超过 24h 没人 acknowledged，处理端标红（规格 §5/§6.1）
TIMEOUT_HOURS = 24
_AT_FORMAT = '%Y-%m-%d %H:%M:%S'


def checkTransition(current, target, actor, reason=''):
    """校验一次流转。返回 (ok, httpCode, error)。

    - target 不是合法状态 → 400
    - (current → target) 不在白名单 → 409（规格 §5：只允许白名单单步流转）
    - 白名单内但 actor 不符 → 403（走错端点：提交者的动作别用处理端 PATCH 打）
    - 必填 reason 的流转缺原因 → 400
    """
    if target not in STATUSES:
        return False, 400, '未知状态：%s' % target
    if target not in TRANSITIONS.get(current, set()):
        return False, 409, '状态不能从 %s 直接变成 %s' % (current, target)
    who = ACTORS.get((current, target))
    if who != actor:
        return False, 403, '%s → %s 只能由%s发起' % (current, target, ACTOR_TEXT.get(who, who))
    if (current, target) in REASON_REQUIRED and not (reason or '').strip():
        return False, 400, '%s → %s 必须附一句原因' % (current, target)
    return True, 0, ''


def parseAt(text):
    """'%Y-%m-%d %H:%M:%S' 本地时间串 → epoch 秒；解析不了返回 None。"""
    try:
        return time.mktime(time.strptime(str(text or '')[:19], _AT_FORMAT))
    except (ValueError, OverflowError):
        return None


def isOverdue(item, now=None):
    """待受理超时：status == submitted 且 received_at 早于 now - 24h。

    now 为 epoch 秒（可注入时钟）；老数据解析失败按不超时处理（不乱标红）。
    """
    now = time.time() if now is None else now
    if (item.get('status') or 'submitted') != 'submitted':
        return False
    at = parseAt(item.get('received_at') or item.get('submitted_at') or '')
    return at is not None and now - at > TIMEOUT_HOURS * 3600


def resolvedAt(item):
    """最近一次进入 resolved 的时刻（epoch 秒）；没有或解析不了返回 None。

    兼容两种 history 条目形状：v2 {from, to, at} 与 v1 {status, at}。
    """
    latest = None
    for entry in item.get('history') or []:
        if (entry.get('to') or entry.get('status') or '') != 'resolved':
            continue
        at = parseAt(entry.get('at') or '')
        if at is not None and (latest is None or at > latest):
            latest = at
    return latest


def dueAutoConfirm(item, now=None):
    """resolved 满 14 天且提交者没动过 → 该自动确认。时钟可注入（测试用）。"""
    now = time.time() if now is None else now
    if (item.get('status') or '') != 'resolved':
        return False
    at = resolvedAt(item)
    return at is not None and now - at >= AUTO_CONFIRM_DAYS * 86400
