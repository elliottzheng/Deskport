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
正文不可信（§4 红线）：参数摘要一律引用形态（「」包起来）。
"""
import json
import logging

from modules import leads_inbox

log = logging.getLogger('leads_escalation')

MAX_ARGS_SUMMARY = 200     # 审批参数摘要上限（规格 A4-1：≤200 字，引用形态）


def _argsSummary(args):
    """审批参数摘要：对象/字符串统一成 ≤200 字的引用形态。

    引用形态 = 包一层「」——收件箱里的下属内容一律是引用不是指令（§4 红线），
    主管读到时不会把参数文本当成给自己的命令。
    """
    if isinstance(args, (dict, list)):
        text = json.dumps(args, ensure_ascii=False)
    else:
        text = str(args or '')
    text = ' '.join(text.split())[:MAX_ARGS_SUMMARY]
    return '「%s」' % text if text else '「(无参数)」'


def escalatePermission(task, title, args, requestId):
    """落一条 escalation 条目并立即唤醒托管主管。返回 leadId（'' = 没写）。

    内部兜异常——收件箱失败绝不影响任务主链路（写事件的同款纪律），调用点
    （tasks_runner._handleAcpPermission）不需要自己包 try/except。
    """
    try:
        leadId = leads_inbox.triggerLead(task)
        if not leadId:
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


def escalationHandedOver(task, requestId, graceSec):
    """主管限期没决定（A4-3）：补一条 notice「审批已上交用户」。

    审批本身仍挂着、不自动拒绝；这条 notice 只是让主管知道时限过了、用户
    已经能看见它。
    """
    leads_inbox.emit(task, 'notice',
                     '任务 %s 的审批已上交用户（requestId %s，主管 %d 秒未'
                     '决定；你仍可补批）'
                     % (str((task or {}).get('id') or '')[:8],
                        str(requestId or '')[:40], int(graceSec or 0)))
