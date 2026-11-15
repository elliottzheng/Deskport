# -*- coding: utf-8 -*-
"""指挥台聚合视图（批次 U）：一个 overview 接口 + 汇报已读；批次 U2：指挥树，
外加指挥者的直接活动（读审计日志，汇总在 modules/command_activity.py）。

设计见 docs/plan/agent-chain-of-command.md §8 与规格 .drive/批次U-指挥台.md、
.drive/批次U2-指挥台树.md：用户只和少数主管说话，待办只来自主管上交的事。本模块
把散在各处的数据聚成指挥台的一屏——等你处理 + 一棵指挥树；数据全部来自现有存储
（tasks_store / leads_store / leads_inbox / 审计日志），只读、不改它们。鉴权：
/api/command/* 只认网页会话。

U2 起前端改用 tree 字段（旧字段 pending/leads/unassigned/retiredLeads 原样保留，
供回滚与对账）；节点形状、key、计数口径、activity 与审批行人话字段都见 docs/api.md
的 overview 一节。lead 节点本身即它的托管任务（ownTask 从子项剔除但计入计数）；任务
节点只带 TASK_NODE_KEYS；其它节点带逐级汇总的 counts（等待给 waiting/waiting_you
两个同值键）。响应体积：300 个任务 < 200KB（树与旧字段各写一遍任务摘要）。
"""
import json
import time

from flask import Blueprint, jsonify, request

from modules import command_activity, command_tree, leads_inbox, leads_store, task_title
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
    if isinstance(value, (dict, list)):
        seq = value.values() if isinstance(value, dict) else value
        parts = [str(v) for v in seq if v not in (None, '')]
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
    openTasks.sort(key=lambda t: -(_int(t.get('lastActiveAt')) or _int(t.get('createdAt'))))
    closedTasks.sort(key=lambda t: -(_int(t.get('endedAt')) or _int(t.get('createdAt'))))
    hidden = max(0, len(closedTasks) - CLOSED_KEEP)
    briefs = [_brief(t) for t in openTasks]
    briefs += [_brief(t) for t in closedTasks[:CLOSED_KEEP]]
    return briefs, hidden


def _taskCounts(tasks):
    """卡片第二行的下属计数：在跑（running+queued）/ 等待 / 出错。

    等待同时给 waiting 与 waiting_you 两个同值键（旧字段读前者、树读后者）。
    """
    waiting = sum(1 for t in tasks if (t.get('status') or '') == 'waiting')
    return {'running': sum(1 for t in tasks
                           if (t.get('status') or '') in ACTIVE_STATUSES),
            'waiting': waiting, 'waiting_you': waiting,
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
    return _int(task.get('endedAt')) or _int(task.get('lastActiveAt')) or _int(task.get('createdAt'))


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
    # U3：审批行要说人话——命令/路径单独一条，下属自述的用途（description）
    # 单独一条，派活令牌名也带上（未登记成主管时前端据此标「未登记的指挥者」）
    cmd = args.get('command') if isinstance(args, dict) else None
    purpose = _firstLine(args.get('description'), REASON_MAX) if isinstance(args, dict) else ''
    command = _firstLine(cmd, ARG_SUMMARY_MAX) if cmd else _argsSummary(args)
    return {'kind': 'permission', 'at': at,
            'taskId': task.get('id') or '', 'leadId': task.get('lead') or '',
            'title': _firstLine(title, TITLE_MAX) or '工具调用',
            'detail': command, 'purpose': purpose, 'requestId': requestId,
            'owner': str(task.get('owner') or ''),
            'options': [{'optionId': str(o.get('optionId') or ''),
                         'name': str(o.get('name') or ''),
                         'kind': str(o.get('kind') or '')} for o in options]}


def _reportPending(lead, entries):
    """主管未读汇报（dir='down' 且 seq > reportSeq）→ 每条的 pending 行。

    kind 按条目原样带出：down 侧绝大多数是 report（汇报），但 L1 的主管
    反应超时提醒与批次 A 的主管自己待批提示都是 notice——指挥台不该把它们
    显示成「汇报」（返工：以前一律硬编码 report）。
    """
    rows = []
    for entry in entries:
        rows.append({'kind': entry.get('kind') or 'report', 'at': _int(entry.get('at')),
                     'leadId': lead.get('id') or '',
                     'title': _firstLine(lead.get('name'), TITLE_MAX) or '主管',
                     'detail': _firstLine(entry.get('text')),
                     'seq': _int(entry.get('seq'))})
    return rows


def _stallPending(task):
    """卡住的工具调用（L4）：无主管的任务进指挥台「等你处理」。

    标记由 task_health 巡检写入（task['_toolStall']，只对无主任务），工具
    收尾即清；行只在该任务还在 running 时出现。有主管的走主管收件箱
    failure，不进这里（规格 L4 的两路分流）。
    """
    stall = task.get('_toolStall')
    if not isinstance(stall, dict) or (task.get('status') or '') != 'running':
        return None
    sec = _int(stall.get('sec')) or _int(stall.get('minutes')) * 60
    if sec >= 60:
        waited = '%d 分钟' % (sec // 60)
    else:
        waited = '%d 秒' % sec       # toolStallSec < 60：显示秒（0 也曾显示成「?」）
    return {'kind': 'stall', 'at': _int(stall.get('at')),
            'taskId': task.get('id') or '', 'leadId': task.get('lead') or '',
            'title': _firstLine(task.get('title') or task.get('prompt'),
                                TITLE_MAX) or '任务',
            'detail': '工具 %s 已 %s无输出'
                      % (_firstLine(stall.get('name'), TITLE_MAX) or '工具',
                         waited)}


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
    row = {'id': lead.get('id') or '', 'name': lead.get('name') or '', 'kind': lead.get('kind') or '',
           'status': lead.get('status') or '', 'state': _leadState(lead, tasksById, unread, hasApproval, now),
           'taskId': lead.get('taskId') or '', 'lastSeenAt': _int(lead.get('lastSeenAt')),
           'counts': _taskCounts(tasks), 'lastReport': _lastReport(str(lead.get('id') or '')),
           'unreadReports': unread, 'closedHidden': closedHidden, 'tasks': briefs}
    return row      # 树的 lead 节点由 command_tree.leadNode 从这行长出来


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
    liveLeads = sorted(leads_store.list(), key=lambda l: _int(l.get('createdAt')))
    for lead in liveLeads:
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
        row = _stallPending(task)         # L4：卡住的工具调用（无主任务）
        if row:
            pending.append(row)
    pending += _problemPending(tasks, now)
    pending.sort(key=lambda p: _int(p.get('at')), reverse=True)

    unassigned = byLead.get('', [])
    briefs, closedHidden = _splitTasks(unassigned)
    tree = command_tree.build(liveLeads, leadRows, byLead, tasks, retiredLeads,
                              _taskCounts, _splitTasks)
    return {'pending': pending, 'leads': leadRows,
            'retiredLeads': retiredLeads,
            'unassigned': {'count': len(unassigned),
                           'running': sum(1 for t in unassigned
                                          if (t.get('status') or '')
                                          in ACTIVE_STATUSES),
                           'closedHidden': closedHidden, 'tasks': briefs},
            # 指挥者的直接活动（读审计日志，modules/command_activity.py）
            'tree': command_activity.decorate(
                tree, command_activity.nodeIndex(liveLeads, now))}


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
