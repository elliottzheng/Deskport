# -*- coding: utf-8 -*-
"""指挥台聚合视图（批次 U）：一个 overview 接口 + 汇报已读。

设计（docs/plan/agent-chain-of-command.md §8，规格 .drive/批次U-指挥台.md）：
用户只和少数主管说话，待办只来自主管上交的事。本模块把散在三处的数据聚成
指挥台的一屏——等你处理（审批/汇报/出问题的下属）、主管卡片、其它任务。
数据全部来自现有存储（tasks_store / leads_store / leads_inbox），不新建存储；
只读调用现有函数，不改 tasks_store / tasks_runner / leads_api / leads_inbox。

鉴权：/api/command/* 不在 authGuard 的令牌通道前缀表里（/api/agent、
/api/tasks、/api/worklines、/api/capability），设备/智能体令牌与未登录一律
落到 /api/ 统一 401——只认网页会话，与 /api/settings/leads* 同档。

与规格草图的差异（都在草图形状上加键，不改语义）：
    lead.closedHidden / unassigned.closedHidden——已关闭任务裁剪后还剩几条，
        前端据此决定显不显示「更多」（草图的 tasks 只有裁剪后的 5 条，看不出
        还有没有）；brief 由 tasks_store.summaryBrief 加一个 lastActiveAt
        （行内「最近活跃」要它，summaryBrief 本身不带这个键）。
    retiredLeads——停用主管的 id→名字（U 返工：停用主管的任务仍会进「等你
        处理」的问题行，主管卡片又没有它，名字从这里补；详情面板据此显示
        「已停用的主管」而不是原始 id）。

响应体积：253 个合成任务 < 200KB（tests/test_command_view.py 钉住）。
"""
import json
import time

from flask import Blueprint, jsonify, request

from modules import leads_inbox, leads_store, task_title
from modules import tasks_store as store

blueprint = Blueprint('command_view', __name__)

ACTIVE_STATUSES = ('running', 'queued')       # 在跑 = 运行中 + 排队中
CLOSED_STATUSES = ('closed', 'cancelled')     # 已关闭 = 用户结束/取消
PROBLEM_STATUSES = ('failed', 'interrupted')  # 出问题的下属（终态两类）
PROBLEM_WINDOW_SEC = 24 * 3600                # 最近 24 小时内结束才算
LEAD_BUSY_SEEN_SEC = 300                      # 外部主管 5 分钟内见过 = 在忙
CLOSED_KEEP = 5                               # 已关闭任务每组只留最近 5 条
ARG_SUMMARY_MAX = 80                          # 审批参数摘要上限（规格：≤80 字）
REASON_MAX = 80                               # 出问题原因首句上限
TITLE_MAX = 40                                # 行内标题上限（长标题不撑破行）


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _firstLine(text, limit=REASON_MAX):
    """首行 + 截断（汇报/原因/标题都只要第一句）。"""
    raw = str(text or '').strip()
    if not raw:
        return ''
    line = raw.splitlines()[0].strip()
    return line[:limit]


def _argsSummary(args):
    """审批参数摘要：对象取值/数组取项/字符串原样，' · ' 连接后截 80 字。"""
    value = args
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ('{', '['):
            try:
                value = json.loads(stripped)
            except ValueError:
                value = stripped
        else:
            value = stripped
    if isinstance(value, dict):
        parts = [str(v) for v in value.values() if v not in (None, '')]
    elif isinstance(value, list):
        parts = [str(v) for v in value if v not in (None, '')]
    else:
        parts = [str(value)] if value not in (None, '') else []
    return _firstLine(' · '.join(parts), ARG_SUMMARY_MAX)


def _brief(task):
    """列表行数据：tasks_store.summaryBrief + lastActiveAt（行内「最近活跃」）。

    批次 G1：带上最新结构化结果的精简版——指挥台任务行的 verdict/测试数
    小标签用它；没有结果的任务没有这个键，前端按空处理。
    """
    brief = store.repo.summaryBrief(task)
    brief['lastActiveAt'] = _int(task.get('lastActiveAt'))
    brief['result'] = task.get('result') or None
    return brief


def _splitTasks(tasks):
    """按「未关闭全部 + 已关闭最近 5 条」裁剪。

    返回 (briefs, closedHidden)：未关闭的按最近活跃倒序（活跃的浮在前面），
    已关闭的按结束时间倒序取前 CLOSED_KEEP 条，closedHidden = 还剩几条没显示。
    """
    openTasks, closedTasks = [], []
    for task in tasks:
        if (task.get('status') or '') in CLOSED_STATUSES:
            closedTasks.append(task)
        else:
            openTasks.append(task)
    openTasks.sort(key=lambda t: -(_int(t.get('lastActiveAt'))
                                   or _int(t.get('createdAt'))))
    closedTasks.sort(key=lambda t: -(_int(t.get('endedAt'))
                                     or _int(t.get('createdAt'))))
    hidden = max(0, len(closedTasks) - CLOSED_KEEP)
    briefs = [_brief(t) for t in openTasks]
    briefs += [_brief(t) for t in closedTasks[:CLOSED_KEEP]]
    return briefs, hidden


def _taskCounts(tasks):
    """卡片第二行的下属计数：在跑（running+queued）/ 等待 / 出错。"""
    return {'running': sum(1 for t in tasks
                           if (t.get('status') or '') in ACTIVE_STATUSES),
            'waiting': sum(1 for t in tasks if (t.get('status') or '') == 'waiting'),
            'failed': sum(1 for t in tasks
                          if (t.get('status') or '') in PROBLEM_STATUSES)}


def _problemReason(task):
    """出问题的下属的原因首句；不是问题返回 ''（空 = 不进 pending）。"""
    status = task.get('status') or ''
    if status in PROBLEM_STATUSES:
        return _firstLine(task.get('error')) or '任务中断了（没有错误信息）'
    lastTurn = task.get('lastTurn') or {}
    if lastTurn.get('empty'):
        return '上一轮 0 个工具、0 句文本就结束了（疑似早夭）'
    if lastTurn.get('silent'):
        return '调了工具却一句终稿都没落（疑似早夭）'
    return ''


def _problemEndedAt(task):
    """结束时间：endedAt 优先，老数据没有就退 lastActiveAt/createdAt。"""
    return (_int(task.get('endedAt')) or _int(task.get('lastActiveAt'))
            or _int(task.get('createdAt')))


def _pendingPermission(task):
    """挂起审批 → pending 行；options 原样带出，前端直接拿来出按钮。

    参数摘要从事件流里对应的 permission_request 取（持久、跨进程可用）；
    事件被 2000 条上限挤掉时退化成只有工具名的行（按钮只剩拒绝/查看）。
    """
    requestId = str(task.get('pendingPermission') or '')
    if not requestId:
        return None
    title, args, options = '工具调用', '', []
    at = _int(task.get('lastActiveAt')) or _int(task.get('createdAt'))
    for event in reversed(task.get('events') or []):
        if event.get('type') == 'permission_request' \
                and str(event.get('requestId') or '') == requestId:
            title = event.get('name') or title
            args = event.get('args') if event.get('args') is not None else ''
            at = _int(event.get('at')) or at
            options = [o for o in (event.get('options') or [])
                       if isinstance(o, dict)]
            break
    return {'kind': 'permission', 'at': at,
            'taskId': task.get('id') or '', 'leadId': task.get('lead') or '',
            'title': _firstLine(title, TITLE_MAX) or '工具调用',
            'detail': _argsSummary(args), 'requestId': requestId,
            'options': [{'optionId': str(o.get('optionId') or ''),
                         'name': str(o.get('name') or ''),
                         'kind': str(o.get('kind') or '')} for o in options]}


def _reportPending(lead, entries):
    """主管未读汇报（dir='down' 且 seq > reportSeq）→ 每条的 pending 行。"""
    rows = []
    for entry in entries:
        rows.append({'kind': 'report', 'at': _int(entry.get('at')),
                     'leadId': lead.get('id') or '',
                     'title': _firstLine(lead.get('name'), TITLE_MAX) or '主管',
                     'detail': _firstLine(entry.get('text')),
                     'seq': _int(entry.get('seq'))})
    return rows


def _problemPending(tasks, now):
    """出问题的下属：failed/interrupted 或空转/静默轮，且最近 24h 内结束。"""
    rows = []
    for task in tasks:
        reason = _problemReason(task)
        if not reason:
            continue
        if now - _problemEndedAt(task) > PROBLEM_WINDOW_SEC:
            continue
        rows.append({'kind': 'problem', 'at': _problemEndedAt(task),
                     'taskId': task.get('id') or '',
                     'leadId': task.get('lead') or '',
                     'title': _firstLine(task_title.titleFor(task),
                                         TITLE_MAX) or '任务',
                     'detail': reason})
    return rows


def _leadState(lead, tasksById, unread, hasApproval, now):
    """卡片状态徽标：已暂停 > 等你 > 在忙 > 睡着（用户的注意力优先）。

    「等你」= 名下有挂起审批或有未读汇报（规格 §2）；托管看自己的会话任务
    在不在跑，外部看 lastSeenAt 新不新（5 分钟内）。
    """
    if (lead.get('status') or '') == 'paused':
        return 'paused'
    if unread > 0 or hasApproval:
        return 'waiting_you'
    own = tasksById.get(str(lead.get('taskId') or ''))
    if (lead.get('kind') or '') == 'hosted':
        if own is not None and (own.get('status') or '') in ACTIVE_STATUSES:
            return 'busy'
    else:
        seen = _int(lead.get('lastSeenAt'))
        if seen and now - seen <= LEAD_BUSY_SEEN_SEC:
            return 'busy'
    return 'asleep'


def _lastReport(leadId):
    """最近一条汇报（不管读没读）；没有返回 None。"""
    entries, _lastSeq = leads_inbox.entriesAfter(leadId, 0, 'down')
    if not entries:
        return None
    entry = entries[-1]
    return {'seq': _int(entry.get('seq')), 'text': str(entry.get('text') or ''),
            'at': _int(entry.get('at'))}


def _leadRow(lead, tasks, unread, hasApproval, tasksById, now):
    briefs, closedHidden = _splitTasks(tasks)
    return {'id': lead.get('id') or '', 'name': lead.get('name') or '',
            'kind': lead.get('kind') or '', 'status': lead.get('status') or '',
            'state': _leadState(lead, tasksById, unread, hasApproval, now),
            'taskId': lead.get('taskId') or '',
            'lastSeenAt': _int(lead.get('lastSeenAt')),
            'counts': _taskCounts(tasks),
            'lastReport': _lastReport(str(lead.get('id') or '')),
            'unreadReports': unread, 'closedHidden': closedHidden,
            'tasks': briefs}


def overview():
    """指挥台一屏数据（路由只做 jsonify；测试可直接调）。"""
    now = time.time()
    store.repo.load()
    tasks = store.repo.list()
    tasksById = {str(t.get('id') or ''): t for t in tasks}
    byLead = {}
    for task in tasks:
        byLead.setdefault(str(task.get('lead') or ''), []).append(task)

    leadRows, pending = [], []
    retiredLeads = {}                     # 停用主管 id→名字（见模块 docstring）
    for lead in sorted(leads_store.list(),
                       key=lambda l: _int(l.get('createdAt'))):
        leadId = str(lead.get('id') or '')
        if (lead.get('status') or '') == 'retired':
            retiredLeads[leadId] = str(lead.get('name') or '')
            continue                      # 停用的主管不上指挥台
        own = byLead.get(leadId, [])
        entries, _last = leads_inbox.entriesAfter(
            leadId, _int(lead.get('reportSeq')), 'down')
        unread = len(entries)
        approvals = sum(1 for t in own if str(t.get('pendingPermission') or ''))
        leadRows.append(_leadRow(lead, own, unread, approvals, tasksById, now))
        pending += _reportPending(lead, entries)
        # 审批在任务侧统一收集（含无主任务），主管只提供名字

    for task in tasks:                    # 任意任务的挂起审批都进待办
        row = _pendingPermission(task)
        if row:
            pending.append(row)
    pending += _problemPending(tasks, now)
    pending.sort(key=lambda p: _int(p.get('at')), reverse=True)

    unassigned = byLead.get('', [])
    briefs, closedHidden = _splitTasks(unassigned)
    return {'pending': pending, 'leads': leadRows,
            'retiredLeads': retiredLeads,
            'unassigned': {'count': len(unassigned),
                           'running': sum(1 for t in unassigned
                                          if (t.get('status') or '')
                                          in ACTIVE_STATUSES),
                           'closedHidden': closedHidden, 'tasks': briefs}}


@blueprint.route('/api/command/overview')
def commandOverview():
    return jsonify(overview())


@blueprint.route('/api/command/leads/<leadId>/reports/read', methods=['POST'])
def reportsRead(leadId):
    """把该主管的 reportSeq 推到 seq（用户已读汇报；只增不减）。

    走 leads_store.setCursor（P3 的游标写入口，max 语义在那边），不自己落盘。
    """
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    if 'seq' not in data:
        return jsonify({'error': '缺少 seq（已读到哪条汇报）'}), 400
    try:
        int(data.get('seq'))
    except (TypeError, ValueError):
        return jsonify({'error': 'seq 需为整数（汇报序号）'}), 400
    record, err = leads_store.setCursor(leadId, reportSeq=data.get('seq'))
    if err == leads_store.NOT_FOUND:
        return jsonify({'error': '主管不存在: %s' % leadId}), 404
    if err:
        return jsonify({'error': err}), 400
    return jsonify({'ok': True, 'reportSeq': _int(record.get('reportSeq'))})
