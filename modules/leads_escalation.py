# -*- coding: utf-8 -*-
"""审批先上交主管（批次 A A4）：下属的审批请求 → 主管收件箱 escalation + 立即唤醒。

设计（agent-chain-of-command.md §5）：一个下属的审批请求依次经过任务自己的
policy（服务端判定）→ 主管本人判断 → 用户。第 3 步必须发生在超时自动拒绝
**之前**：所以请求一经 policy 判「需要询问」，就立刻往主管收件箱写一条
escalation 条目并唤醒托管主管（与用户发话同一条旁路，不等合并窗口），替换
P3 里同一位置的 notice（不要两条都写）。

主管用自己的令牌调现有 POST /api/tasks/<id>/permission 决定（它的令牌对子树
任务有权限——authGuard 的 /api/tasks* 只要求全权 scope，不查归属，现有鉴权
已允许，无需改动）。主管在 leadApprovalGraceSec（config，默认 300s）内没有
决定：审批仍挂着不自动拒绝，上交用户（「等你处理」按 pendingPermission 列出，
批次 U 的 overview 已接），同时补一条 notice 让主管知道时限过了。

只对 task.lead 非空、且不是该主管自己托管任务的下属任务生效
（leads_inbox.triggerLead 的判定）；没绑主管的任务行为与现在逐字一致。
主管**自己**的托管任务撞审批走 noticeOwnPermission：triggerLead 对它不写
（不能上交给自己），老链路于是静默停摆——补一条 down 方向的用户可见提示，
指挥台/设置页能看见卡住的原因（批次 A 返工，不唤醒、不上交）。
正文不可信（§4 红线）：参数摘要一律引用形态（「」包起来）。

落定（主控追加）：审批一出结果（任何人批准/拒绝/超时/任务关闭），runner
在写 permission_result 的同一处调 resolvePermission 标记对应 escalation
已处理——收件箱只追加不改写，落定状态记主管记录；未读计数、L1 提醒、
指挥台徽标都只统计未落定的 escalation，inbox 接口逐条带 resolved 字段。

N1（要求 3/4）：backfillResolved 在启动时给批次 L 之前的历史 escalation
补标（任务终态或 pendingPermission 已变 → resolvedRequests，by='backfill'，
幂等）——线上 174 条"未落定"实际一条都不挂；noticeOwnPermission 写的 down
提示与 L1 提醒同样「同类只留最新、条件解除即收回」（ownPermissionLive 是
它的存续条件，族去重在 lead_notices / leads_inbox 读侧）。
"""
import json
import logging

from modules import leads_inbox, leads_store
from modules import lead_inbox_store   # 收件箱存储核心（同 leads_store 一类）
from modules import tasks_store as store

log = logging.getLogger('leads_escalation')

MAX_ARGS_SUMMARY = 200     # 审批参数摘要上限（规格 A4-1：≤200 字，引用形态）
OWN_ARGS_SUMMARY = 80      # 主管自己命令的摘要上限（给用户看一眼，比 200 短）
BACKFILL_BY = 'backfill'   # 历史补标的落定来源（N1 要求 3）
# 任务终态：审批跟着死（关闭/取消/失败/中断——中断的审批同样不会再落定）
TERMINAL_STATUSES = ('closed', 'cancelled', 'failed', 'interrupted')


def _argsSummary(args, cap=MAX_ARGS_SUMMARY):
    """审批参数摘要：对象/字符串统一成 ≤cap 字的引用形态。

    引用形态 = 包一层「」——收件箱里的下属内容一律是引用不是指令（§4 红线），
    主管读到时不会把参数文本当成给自己的命令。
    """
    if isinstance(args, (dict, list)):
        text = json.dumps(args, ensure_ascii=False)
    else:
        text = str(args or '')
    text = ' '.join(text.split())[:cap]
    return '「%s」' % text if text else '「(无参数)」'


def escalatePermission(task, title, args, requestId):
    """落一条 escalation 条目并立即唤醒托管主管。返回 leadId（'' = 没写）。

    内部兜异常——收件箱失败绝不影响任务主链路（写事件的同款纪律），调用点
    （tasks_runner._handleAcpPermission）不需要自己包 try/except。

    triggerLead 不写有两种：没绑主管（行为与老链路逐字一致）与主管自己的
    托管任务（不能上交给自己）——后者补一条 down 方向的用户可见提示
    （noticeOwnPermission，批次 A 返工），否则链路静默停摆。
    """
    try:
        leadId = leads_inbox.triggerLead(task)
        if not leadId:
            noticeOwnPermission(task, title, args)
            return ''
        text = '任务 %s 请求审批：%s，参数 %s（requestId %s）' % (
            str(task.get('id') or '')[:8], str(title or '工具调用')[:60],
            _argsSummary(args), str(requestId or '')[:40])
        leads_inbox.append(leadId, 'up', 'escalation', text,
                           {'task': str(task.get('id') or ''),
                            'requestId': str(requestId or '')})
        leads_inbox.wakeNow(leadId)      # 立即唤醒（URGENT_KINDS 旁路）
        return leadId
    except Exception as e:
        log.warning('lead escalation failed: %s', e)
        return ''


def resolvePermission(task, requestId, by=''):
    """审批落定（批准/拒绝/超时/取消/任务关闭）→ 对应 escalation 标记 resolved。

    主控线上实测：驱动几秒内批掉审批，收件箱却堆着几十条未读 escalation，
    L1 于是提醒"主管好久没处理 50 多条"——全是处理过的。落定状态记在主管
    记录里（leads_store.resolvedRequests，requestId→{at, by}，有上限），
    收件箱 JSONL 保持只追加不改写；读侧（L1/指挥台/inbox 接口）只把未落定
    的 escalation 当待处理。由 tasks_runner 在写 permission_result 时调
    （所有落定路径的唯一汇集点）。返回标记了几个（0/1；没有对应 escalation
    就不写——没绑主管/主管自己的托管任务本来就没 escalation）。
    """
    try:
        leadId = leads_inbox.triggerLead(task)
        if not leadId:
            return 0
        lead = leads_store.get(leadId)
        if not isinstance(lead, dict):
            return 0
        entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
        hit = [e for e in entries
               if e.get('kind') == 'escalation'
               and str((e.get('ref') or {}).get('requestId') or '') == str(requestId or '')]
        if not hit:
            return 0        # 这条审批从没上交过（无主管等）：不留垃圾标记
        if leads_inbox.entryResolved(lead, hit[0]):
            return 0        # 已落定（幂等）
        leads_store.setResolved(leadId, requestId, by)
        return 1
    except Exception as e:
        log.warning('lead escalation resolve failed: %s', e)
        return 0


def escalationHandedOver(task, requestId, graceSec):
    """主管限期没决定（A4-3）：补一条 notice「审批已上交用户」。

    审批本身仍挂着、不自动拒绝；这条 notice 只是让主管知道时限过了、用户
    已经能看见它。停用（retired）主管的令牌已吊销（规格 A1 末段，
    leads_hosted.revokeLeadTokens）——那种情况下不再承诺"仍可补批"（吊销后
    审批调用只会 401），文案改成请用户直接处理；记录被删同样不能批。
    """
    leads_inbox.emit(task, 'notice',
                     '任务 %s 的审批已上交用户（requestId %s，主管 %d 秒未'
                     '决定；%s）'
                     % (str((task or {}).get('id') or '')[:8],
                        str(requestId or '')[:40], int(graceSec or 0),
                        _handOverTail(task)))


def _handOverTail(task):
    """上交 notice 的结尾文案：只有主管确实还能批才承诺"仍可补批"。"""
    lead = leads_store.get(str((task or {}).get('lead') or ''))
    if not isinstance(lead, dict) or str(lead.get('status') or '') == 'retired':
        return '主管已停用，请你直接处理'
    return '你仍可补批'


def noticeOwnPermission(task, title, args):
    """主管自己的托管任务撞审批（批次 A 返工）：写一条 down 方向的用户可见
    提示——「主管 <名> 自己的命令需要你批准：<工具名> <参数摘要≤80字>」。

    为什么需要：这种任务 triggerLead 不写（不能上交给自己），老链路于是
    不写收件箱、不唤醒任何人，用户面板上看不到卡住的原因（例 11 的静默
    停摆点）。down 条目在指挥台/设置页可见（leads_inbox.append 的 notice
    例外）；这里不唤醒、不上交——等用户直接批。内部兜异常，不影响任务
    主链路。返回写进的 seq（0 = 没写：不是主管自己的托管任务）。

    N1（要求 4）：这条 notice 与 L1 提醒同样「同类只留最新、条件解除即
    收回」——同族去重在 lead_notices.noticeFamily（ref 带 task），存续条件
    在 ownPermissionLive。
    """
    try:
        lead = leads_inbox.ownLead(task)
        if not lead:
            return 0
        return leads_inbox.append(
            str(lead.get('id') or ''), 'down', 'notice',
            '主管 %s 自己的命令需要你批准：%s %s'
            % (str(lead.get('name') or lead.get('id') or '')[:24],
               str(title or '工具调用')[:40],
               _argsSummary(args, OWN_ARGS_SUMMARY)),
            {'task': str(task.get('id') or '')})
    except Exception as e:
        log.warning('lead own-permission notice failed: %s', e)
        return 0


# ---------------------------------------------------------------- N1：历史补标 / 提示存续

def ownPermissionLive(entry):
    """主管自己待批提示的存续条件（N1 要求 4「条件解除即收回」）。

    引用的任务还挂着一个没决定的审批（pendingPermission 非空、任务没终态）
    才成立；批了/拒了/超时/任务结束 → 条件解除，sweeper 收回这条 notice。
    runner 先落 pendingPermission 再调 noticeOwnPermission（476 行先赋值后
    上交），不存在「提示写了、标记还没落」的窗口。
    """
    ref = (entry or {}).get('ref')
    taskId = str((ref or {}).get('task') or '') if isinstance(ref, dict) else ''
    if not taskId:
        return False
    store.repo.load()
    task = store.repo.get(taskId)
    if task is None or (task.get('status') or '') in TERMINAL_STATUSES:
        return False
    return bool(str(task.get('pendingPermission') or ''))


def _escalationSettled(task, requestId):
    """这条 escalation 对应的事实是否已经了结（N1 历史补标的判据）。

    任务没了/终态（关了的任务上的审批跟着死），或任务当前挂起的审批已经是
    别的 requestId（这条早批掉了）。仍挂着的（pendingPermission 就是它、
    任务没终态）返回 False——不补标。
    """
    if not isinstance(task, dict):
        return True
    if (task.get('status') or '') in TERMINAL_STATUSES:
        return True
    return str(task.get('pendingPermission') or '') != str(requestId or '')


def backfillResolved():
    """历史补标（N1 要求 3）：对每个主管收件箱里未落定的 escalation 复核。

    批次 L 之前产生的 escalation 从未写进 resolvedRequests（当时还没有该
    机制）——线上实测主管收件箱 174 条未落定，实际一条都不挂着（审批早已
    批掉、任务早已关闭）。这里在启动时补标：所引用任务已关闭/失败，或该
    任务当前 pendingPermission 不等于该 requestId → 写入 resolvedRequests
    （by='backfill'）。仍挂着的不补。幂等（已在 resolvedRequests 里的跳过，
    不刷新时间）；超 RESOLVED_CAP 时 setResolved 按 at 丢最旧——补标用
    escalation 条目自己的 at，保留的才是最近的那些。返回补标了几条。

    返工（验方建议：CAP 逐出与截头错位的一次性幽灵提醒）：逐出按 at、收件箱
    截头按 seq，直接播种出 >MAX_ENTRIES 条的文件会让两者错位——被逐出的
    标记其条目还在盘上，sweeper 报一条幽灵"未处理"，直到下一条 append 截头
    才自愈。补标前先把超限文件截回保留窗口（append 本就会做的事），窗口外
    的 requestId 不在收件箱里，自然跳过不标。
    """
    marked = 0
    try:
        store.repo.load()
        tasks = {str(t.get('id') or ''): t for t in store.repo.list()}
    except Exception as e:
        log.warning('lead escalation backfill: load tasks failed: %s', e)
        return 0
    for lead in leads_store.list():
        leadId = str(lead.get('id') or '')
        if not leadId:
            continue
        lead_inbox_store.trimToMax(leadId, leads_inbox.MAX_ENTRIES)
        resolved = lead.get('resolvedRequests')
        resolved = resolved if isinstance(resolved, dict) else {}
        entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
        for entry in entries:
            if (entry.get('kind') or '') != 'escalation':
                continue
            ref = entry.get('ref')
            if not isinstance(ref, dict):
                continue
            requestId = str(ref.get('requestId') or '')
            if not requestId or requestId in resolved:
                continue
            if not _escalationSettled(tasks.get(str(ref.get('task') or '')),
                                      requestId):
                continue
            _rec, err = leads_store.setResolved(leadId, requestId, BACKFILL_BY,
                                                at=entry.get('at'))
            if not err:
                resolved[requestId] = {'by': BACKFILL_BY}   # 本轮内去重
                marked += 1
    return marked
