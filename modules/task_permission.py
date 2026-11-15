# -*- coding: utf-8 -*-
"""任务审批漏斗（批次 G1 从 tasks_runner 拆出）：ACP 授权请求 → 服务端强制门
→ policy 短路 → 挂起等决定 → 结果事件。

拆出原因：tasks_runner 行数预算紧张（规格：超了就拆模块，不要调预算），
审批是自成一段的子系统（幽灵审批/policy 短路/主管上交/限期都在这里）。
tasks_runner 保留 _handleAcpPermission 兼容别名，行为不变。

批 G1 新增：平台注入的 submit_result 工具（mcp__deskport__submit_result）
自动放行——MCP 工具调用与本机工具走同一条 ACP permission 通道，不自动放行
就会给执行方的交付动作弹审批（规格 §3：不产生审批请求）。
"""
import json
import threading

import config
from modules import leads_escalation, task_node, task_result_mcp
from modules import tasks_store as store

BACKGROUND_DENY_TEXT = '后台任务被禁用，请在前台运行'
ORPHAN_DENY_TEXT = ('该审批的 callId 在本任务的工具调用记录里找不到'
                    '（后台幽灵审批），已自动拒绝')
SUBMIT_TOOL_TITLE = 'mcp__%s__%s' % (task_result_mcp.MCP_SERVER_NAME,
                                     task_result_mcp.TOOL_NAME)


def parseArgs(text):
    """参数文本 → 对象；解析不成 dict/list 返回 None。

    流式参数在 tool_call_update.content 里是逐段 JSON 快照（先 '{'，再半
    截，最后完整），只有完整那次解析得出来。
    """
    text = (text or '').strip()
    if text[:1] not in ('{', '['):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def knownToolCall(task, callId):
    """callId 是否在本任务的 tool 事件里出现过（R5-7 orphan 判定）。

    查两处：_toolArgs（tool_call update 记下的最近 50 个）与任务事件流里
    的 tool 事件（MAX_EVENTS 窗口内）。两处都没有 = 轮间幽灵审批。
    """
    if not callId:
        return False
    with store.repo.lock:
        if callId in (task.get('_toolArgs') or {}):
            return True
        return any(e.get('type') == 'tool' and e.get('callId') == callId
                   for e in task.get('events') or [])


def handleAcpPermission(task, agent, params, requestId):
    """把 ACP 的授权请求推到任务卡，等用户在前端点允许/拒绝。

    挂起前先过两道服务端强制门（R5-7）：轮间 callId 查不到的幽灵审批直接
    auto_denied(rule=orphan)；policy 命中 allow/deny 的调用也在这里直接
    短路（auto_approved/auto_denied 落卡，不产生 permission_request，
    run_in_background 见 task_policy）；联邦代理任务跳过本端 policy 判定
    ——对端中枢已用同一份 policy 判过。
    """
    from modules import task_policy
    tool = params.get('toolCall') or {}
    title = tool.get('title') or '工具调用'
    callId = tool.get('toolCallId') or ''
    kind = tool.get('kind') or ''
    rawInput = tool.get('rawInput')
    # 平台注入的 submit_result（批次 G1）：执行方的交付动作，自动放行——
    # 不过问 policy、不挂起、不进主管上交链路
    if title == SUBMIT_TOOL_TITLE:
        optionId = next((o['optionId'] for o in params.get('options') or []
                         if o.get('kind') in ('allow_once', 'allow_always')), '')
        if optionId:
            store.repo.append_event(task, 'auto_approved', callId=callId,
                                    rule='submit-result', name=title)
            return optionId
        store.repo.append_event(task, 'permission_result',
                                requestId=str(requestId), status='denied',
                                text='没有可用选项，已拒绝：' + title)
        return None
    with store.repo.lock:
        last = task.get('_lastToolCall') or {}
        byKind = (task.get('_toolKinds') or {}).get(callId) if callId else None
        byCall = (task.get('_toolArgs') or {}).get(callId) if callId else None
        streamed = (task.get('_toolArgText') or {}).get(callId) if callId else None
        # 轮间幽灵审批（R5-7 #2）：status=waiting（本轮已结束、等续聊）时
        # 收到的审批，callId 在本任务 tool 事件里查不到——后台任务结束后的
        # 残响会反复重放审批。直接拒绝，不挂起、不挡 message；执行轮次中
        # （running）与其他状态不适用，避免误杀正常链路
        orphan = bool(callId) and task.get('status') == 'waiting' \
            and not knownToolCall(task, callId)
    if orphan:
        store.repo.append_event(task, 'auto_denied', callId=callId,
                                rule='orphan', name=title, text=ORPHAN_DENY_TEXT)
        return None
    # kind 链（R4-2b）：真实 Kimi 的 request_permission.toolCall 不带 kind，
    # 按 callId 回查 tool_call update 记下的 kind，再兜底最近一次调用
    if not kind:
        kind = byKind or last.get('kind') or ''
    # 真实链路（R4 验收 / R5-9）：Kimi 的 request_permission 常不带 rawInput，
    # 且 tool_call 参数是 JSON 字符串——为空就按 callId/最近调用回查，字符串
    # 统一解析成对象，否则引号剥离会把整段 JSON 当引号内容吃掉。
    # 流式形状（模型在 tool_call 之后才吐参数）下按 callId 记下的可能是空串：
    # 空的不认，退回审批自带/流式文本里的真参数（R5-9：旧的"空也认"会盖掉它）。
    # 例 11 实测（glm-5.3-flash）：tool_call 首帧 content 就是 "{"（流式残片，
    # 完整快照在同 callId 的 tool_call_update 里，先于 request_permission 到）——
    # 残片是解析不成 JSON 的字符串，直接用它 policy 只能拿到 "{" 判不了，边界内
    # 的命令也会挂起等人工（托管主管无人干预链就是这么断的）。所以残片让位给
    # streamed 的完整快照；streamed 也没有才认残片
    staleShard = isinstance(byCall, str) and parseArgs(byCall) is None
    if callId and byCall and not (staleShard and streamed):
        rawInput = byCall               # 按 callId 精确回查：参数记下了就用
    if not rawInput and callId and streamed:
        parsed = parseArgs(streamed)
        rawInput = parsed if parsed is not None else streamed
    if not rawInput and not callId:
        # 无 callId 才借用最近一次调用（R4-2b，连 callId 一起认领）；callId
        # 带着但查不到说明是另一次调用（后台/轮间请求，事件流里无对应 tool
        # 事件）——轮次之间已在上面按 orphan 自动拒绝（R5-7 #2）；执行轮次
        # 中保持空参数走 policy default（ask），不借用（R4-3c #1）
        callId = last.get('callId') or callId
        rawInput = last.get('rawArgs')
    if not rawInput and not callId:
        rawInput = last.get('args') or ''
    if isinstance(rawInput, str) and rawInput.strip()[:1] in ('{', '['):
        try:
            parsed = json.loads(rawInput.strip())
            if isinstance(parsed, dict):
                rawInput = parsed
        except ValueError:
            pass
    if callId and rawInput:
        # 回查到的参数按调用留痕：后续审批/孤儿判定都能查到（R5-9）
        with store.repo.lock:
            task.setdefault('_toolArgs', {})[callId] = rawInput
    # 审批事件自带被审批调用的参数（对象优先），name=工具名（反馈 §1）
    argsForEvent = rawInput if isinstance(rawInput, (dict, list)) \
        else str(rawInput or '')
    paths = task_policy.permissionPaths(params)
    options = []
    for opt in params.get('options') or []:
        options.append({'optionId': opt.get('optionId') or '',
                        'name': opt.get('name') or '',
                        'kind': opt.get('kind') or ''})

    policy = task.get('policy') or {}
    peerJudged = False
    if policy and not task_node.isLocal(task_node.of(task)):
        from modules import agent_nodes
        peerJudged = bool(agent_nodes.findFederationMember(task.get('node') or ''))
    if policy and options and not peerJudged:
        kind = tool.get('kind') or ''
        try:
            decision, rule = task_policy.evaluate(policy, kind, title, rawInput,
                                                  cwd=task.get('workspace') or '', paths=paths)
        except Exception:
            decision, rule = 'ask', 'policy-error'
        if decision == 'allow':
            optionId = next((o['optionId'] for o in options
                             if o.get('kind') in ('allow_once', 'allow_always')), '')
            if optionId:
                store.repo.append_event(task, 'auto_approved', callId=callId,
                                        rule=rule, name=title)
                return optionId
            decision, rule = 'ask', 'no-allow-option'
        elif decision == 'deny':
            # 拒绝原因（R5-7）：ACP permission 响应只有 selected/cancelled
            # 两种 outcome、带不了原因文字，原因落 auto_denied 事件给主控看
            extra = {'text': BACKGROUND_DENY_TEXT} if rule == 'background' else {}
            store.repo.append_event(task, 'auto_denied', callId=callId,
                                    rule=rule, name=title, **extra)
            return None

    if not options:
        store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                     status='denied', text='没有可用选项，已拒绝：' + title)
        return None
    decision = {'value': None}
    with store.repo.lock:
        agent['perm'] = {'requestId': str(requestId), 'seq': 0,
                         'title': title, 'options': options,
                         'callId': callId, 'args': argsForEvent, 'paths': paths}
        agent['permDecision'] = decision
        agent['permEvent'] = threading.Event()
        task['pendingPermission'] = str(requestId)
        store.repo.save()
        # 挂起标记赋值路径显式唤醒（R4-3b #3）：不依赖 append_event 的
        # notify——标记先于事件落卡，等待者醒来即可读到完整状态
        store.repo.evCond.notify_all()
    ev = store.repo.append_event(task, 'permission_request', requestId=str(requestId),
                      text='等待你授权：' + title, name=title, options=options,
                      callId=callId, args=argsForEvent, paths=paths)
    with store.repo.lock:
        if agent.get('perm'):
            agent['perm']['seq'] = ev.get('seq')
    # 批次 A（A4-1）：有主管的下属任务——审批请求立刻上交主管（escalation
    # 条目 + 立即唤醒，与用户发话同一条旁路），替换 P3 里同一位置的 notice
    # （不要两条都写）。主管自己的托管任务与没绑主管的任务不写（前者另由
    # leads_escalation 补 down 用户可见提示；后者行为与现在逐字一致）
    escLead = leads_escalation.escalatePermission(task, title, argsForEvent,
                                                  str(requestId))
    try:
        timeoutSec = int((policy or {}).get('timeoutSec')
                         if (policy or {}).get('timeoutSec') is not None
                         else config.getTaskPermissionTimeoutSec())
    except (TypeError, ValueError):
        timeoutSec = config.getTaskPermissionTimeoutSec()
    deadline = store._now() + timeoutSec if timeoutSec > 0 else None
    # 批次 A（A4-3）：主管限期。期限内没决定 → 补一条 notice「审批已上交
    # 用户」，审批继续挂着、不自动拒绝（用户的「等你处理」按
    # pendingPermission 列出）；主管仍可在期限内补批
    graceSec = config.getLeadApprovalGraceSec()
    graceAt = (store._now() + graceSec) if (escLead and graceSec > 0) else None
    handedOver = False
    while True:
        with store.repo.lock:
            if decision['value'] is not None:
                break
            if task.get('closedByUser') or task.get('status') == 'closed':
                break
        if graceAt is not None and not handedOver and store._now() >= graceAt:
            handedOver = True
            leads_escalation.escalationHandedOver(task, str(requestId), graceSec)
        if deadline is not None and store._now() >= deadline:
            break
        agent['permEvent'].wait(0.5)
        agent['permEvent'].clear()
    with store.repo.lock:
        value = decision['value']
        # 批次 A（A4-4）：谁批的留痕——taskPermission 记下决定者（主管令牌名
        # / 面板），结果事件带 by；超时/取消没有决定者，不带这个键
        decidedBy = str(decision.get('by') or '')
        agent['perm'] = None
        task['pendingPermission'] = ''
        backlog = bool(task.get('messageQueue'))
        store.repo.save()
    if backlog:
        # 审批落定即唤醒队列消费（R5-5）：轮中排队的消息会在本轮结束后投递；
        # 轮间挂起（后台调用审批）时 runner 阻塞在 queue.get()，靠这条信号醒来。
        # 多余的信号无害——空队清一遍即回到等待
        try:
            agent['queue'].put({'type': 'drain'})
        except Exception:
            pass
    if value is None:
        store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                     status='timeout', text='授权超时，已拒绝：' + title)
        return None
    # 批次 A（A4-4）：结果事件带 by（决定者：主管令牌名 / 面板）
    byKw = {'by': decidedBy} if decidedBy else {}
    if value == '__deny__':
        store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                     status='denied', text='你拒绝了：' + title, **byKw)
        return None
    store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                 status='approved', text='已允许：' + title, **byKw)
    return value
