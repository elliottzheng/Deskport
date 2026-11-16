# -*- coding: utf-8 -*-
"""任务编排层：ACP 常驻会话、prompt 轮次、权限等待、取消/续跑。

不 import Flask；通过 tasks_store 读写状态。
"""
import atexit
import json
import logging
import queue
import threading
import time

import config
from modules import leads_inbox, sensitive_values
from modules import session_adopt, task_drain, task_permission, task_result, task_result_mcp
from modules import tasks_store as store
from modules import task_events, task_state, task_node
from modules import task_engines
from modules import worklines_store
from modules.kimi_acp import KimiAcp, extractText

log = logging.getLogger('tasks')

# 审批漏斗已拆至 modules/task_permission（批次 G1 腾预算）；兼容存量测试引用
_handleAcpPermission = task_permission.handleAcpPermission

def maxRunning():
    """缺省并发 running 上限（R4-9）：config.taskMaxRunning，缺省 8，0=不限。
    每次判定现读（config.load 按 mtime 热读），改 config.json 不重启即生效。
    批次 P 起它只是「未声明执行点的缺省值」，按节点的上限见 limitFor。"""
    return config.getTaskMaxRunning()

def limitFor(node):
    """该执行点的并发 running 上限（批次 P）：节点声明 maxRunning → 缺省
    config.taskMaxRunning（8）→ 0=不限。名字经 task_node.normalize() 归一后
    再比，host/旧别名换写法绕不过上限；声明来源 agentNodes[] / remotes[] /
    localAgentNodeMaxRunning（config 侧已归一成整数 ≥0 或 None）。"""
    from modules import agent_nodes
    name = task_node.normalize(node)
    entry = agent_nodes.findNode(name) or agent_nodes.findFederationMember(name)
    declared = (entry or {}).get('maxRunning')
    return maxRunning() if declared is None else declared

def _appendAssistantText(task, text):
    """回合内的 chunk 只累积缓冲（log 留完整流水），不写事件流。

    增量订阅者（SSE/轮询）按 seq 去重，若逐 chunk 合并进同一条事件，
    订阅者会停在首推时刻的部分文本。回合结束时 _flushAssistant 以单条
    完整事件落卡，订阅者天然拿到终版；log 流水仍逐 chunk 可追溯。

    批 PS2：chunk 路径不 save 也不 touch——lastText 随轮末 _flushAssistant
    的 assistant 事件与 _promptOnce 的状态变迁落盘（规格：轮中崩溃时
    lastText 停在轮初，jsonl 有 chunk 全文可追溯，方案 §6 风险 7 已列明）。
    """
    if not text:
        return
    with store.repo.lock:
        task['_assistantBuf'] = (task.get('_assistantBuf') or '') + text
        task['lastText'] = task['_assistantBuf'][:300]
    store.repo.log_write(task, {'at': store._now(), 'type': 'assistant_chunk',
                                'text': text})


def _flushAssistant(task):
    """把回合缓冲落成一条完整 assistant 事件；无内容时不产生事件。"""
    with store.repo.lock:
        buf = task.pop('_assistantBuf', '') or ''
    if not buf:
        return
    text, truncated = task_events.truncateEvent('assistant', buf)
    store.repo.append_event(task, 'assistant', text=text, full=buf, truncated=truncated)


def _argsText(args):
    """工具参数（对象/字符串）→ 事件里存的文本。"""
    return json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args


def _rememberLateArgs(task, update):
    """参数后到（R5-9）：真实 Kimi 的流式形状是先来不带 rawInput 的
    tool_call，参数随后在 tool_call_update 的 content 文本里补，rawInput
    对象甚至晚于 request_permission 才到。按 callId 记下这两条线索，供审批
    回查 policy、给已落卡的 tool 事件补参数；返回 (callId, 可落盘的参数
    文本)，拿不到完整参数时文本为空。"""
    callId = update.get('toolCallId') or ''
    if not callId:
        return '', ''
    status = update.get('status') or ''
    rawInput = update.get('rawInput')
    # completed/failed 的 content 是工具输出，不是参数
    text = '' if status in ('completed', 'failed') \
        else extractText(update.get('content'))
    argsText = ''
    with store.repo.lock:
        if rawInput is not None:
            task.setdefault('_toolArgs', {})[callId] = rawInput
            argsText = _argsText(rawInput)
            last = task.get('_lastToolCall') or {}
            if last.get('callId') == callId:
                last['rawArgs'] = rawInput
                last['args'] = argsText[:2000]
        if text:
            texts = task.setdefault('_toolArgText', {})
            texts[callId] = text[-store.MAX_TEXT:]
            if len(texts) > 50:
                for k in list(texts.keys())[:-50]:
                    texts.pop(k)
            if not argsText:
                parsed = task_permission.parseArgs(text)
                if parsed is not None:
                    argsText = json.dumps(parsed, ensure_ascii=False)
    return callId, argsText


def _needsBackfill(args, argsText):
    """是否把后到的参数补进已落卡的 tool 事件（主控 2026-11-15 定规则）。

    已落卡的 args 为空、或不是合法完整 JSON（流式首帧残片，如 "{"、
    "{\"command\": "——U 批验收实测的形状），且后到的 argsText 是合法完整
    JSON → 回填。后到的参数本来就是权威值；已落卡是合法完整 JSON 时不覆盖
    （先落的完整参数同样是权威值）。后到的不是合法 JSON 时也不回填——流式
    中途残片盖过去只会把事件卡在半截参数上。
    """
    if task_permission.parseArgs(args) is not None:
        return False        # 已落卡完整：不覆盖
    return task_permission.parseArgs(argsText) is not None


def _backfillToolArgs(task, callId, argsText):
    """把后到的参数补进已落卡的 tool 事件（写入前同样脱敏，R5-2）。

    只改内存不够：批 PS1 起窗口从 jsonl 回填，而 jsonl 该行是参数未到时写
    的（args 为空），重启后回填看到的还是空——真实 Kimi 正是这种流式形状
    （_rememberLateArgs），主控 2026-11-15 决定不接受"重启后看不到命令"。
    故回填时往 jsonl 追加一行**不带 seq** 的 tool_args_patch 补丁记录
    （targetSeq=被补事件的 seq，args 与内存落卡逐字一致），重启回填与
    /event/<seq> 按它还原；补丁不占事件序号，seq 校正不受影响。

    回填判定（独立验收 2026-11-15 追加）：残片首帧（含 U 批实测的
    "{\"command\": " 这种 11 字半截 JSON）也要补，规则见 _needsBackfill。

    批 PS2 确认：回填只改内存 + 写 jsonl 补丁行，不触发 save/touch——落盘
    快照已剥离 events（批 PS1），args 以 jsonl 补丁行为准，重启后按它还原。
    """
    if not callId or not argsText:
        return
    text = sensitive_values.redactText(argsText)
    text, flag = task_events.truncateEvent('tool', text)
    with store.repo.lock:
        for event in reversed(task.get('events') or []):
            if event.get('type') != 'tool' or event.get('callId') != callId:
                continue
            if not _needsBackfill(event.get('args'), argsText):
                return
            event['args'] = text
            event['truncated'] = flag
            store.repo.log_write(task, {'type': 'tool_args_patch',
                                        'targetSeq': event.get('seq'),
                                        'args': text, 'truncated': flag,
                                        'at': store._now()})
            return


def _handleAcpUpdate(task, update):
    kind = update.get('sessionUpdate')
    if kind == 'agent_message_chunk':
        _appendAssistantText(task, extractText(update.get('content')))
        return
    if kind == 'agent_thought_chunk':
        return   # 思考片段不落任务卡，避免每 token 一行
    if kind == 'tool_call':
        callId = update.get('toolCallId') or ''
        args = update.get('rawInput')
        if args is None:
            args = extractText(update.get('content'))
        argsText = _argsText(args)
        with store.repo.lock:
            # 审批事件要能对回工具调用（反馈 §3.4）：ACP 的 permission 请求
            # 常不带 rawInput——按 callId 回查原始参数（对象/字符串原样保留），
            # 兜底再取最近一次 tool_call；_lastToolCall.args 是序列化文本。
            # kind 同链路回查（R4-2b：真实 Kimi 的 request_permission.toolCall
            # 不带 kind，tool_call update 里有——policy 写工具路径判定要用）
            byCall = task.setdefault('_toolArgs', {})
            byKind = task.setdefault('_toolKinds', {})
            if callId:
                byCall[callId] = args
                byKind[callId] = update.get('kind') or ''
                if len(byCall) > 50:
                    for k in list(byCall.keys())[:-50]:
                        byCall.pop(k)
                if len(byKind) > 50:
                    for k in list(byKind.keys())[:-50]:
                        byKind.pop(k)
            task['_lastToolCall'] = {'callId': callId,
                                     'name': update.get('title') or '',
                                     'kind': update.get('kind') or '',
                                     'args': argsText[:2000],
                                     'rawArgs': args}
            # 主控可观测（R4-8）：执行方正在跑的工具，tool_result 到达即清
            task['_currentTool'] = {'name': update.get('title') or '',
                                    'since': store._now()}
        args, truncated = task_events.truncateEvent('tool', argsText)
        store.repo.append_event(task, 'tool',
                     name=update.get('title') or update.get('kind') or 'tool',
                     args=args, full=argsText, truncated=truncated,
                     callId=callId, status=update.get('status') or '')
        return
    if kind == 'tool_call_update':
        lateCallId, lateArgs = _rememberLateArgs(task, update)
        _backfillToolArgs(task, lateCallId, lateArgs)
        if update.get('status') not in ('completed', 'failed'):
            return
        raw = update.get('rawOutput')
        if raw is None:
            raw = extractText(update.get('content'))
        rawText = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        with store.repo.lock:
            task['_currentTool'] = None   # 工具收尾（R4-8 currentTool 清空）
            task['_toolStall'] = None     # L4：卡住标记随之清除（指挥台不再待办）
        text, truncated = task_events.truncateEvent('tool_result', rawText)
        store.repo.append_event(task, 'tool_result',
                     text=text, full=rawText, truncated=truncated,
                     callId=update.get('toolCallId') or '',
                     name=update.get('title') or '')
        return
    if kind == 'plan':
        lines = []
        for item in update.get('entries') or []:
            if isinstance(item, dict):
                lines.append('%s %s' % (item.get('status') or '', item.get('content') or ''))
            else:
                lines.append(str(item))
        planText, _ = task_events.truncateEvent('plan', '\n'.join(lines))
        store.repo.append_event(task, 'plan', text=planText)
        return
    if kind == 'available_commands_update':
        # R4-8：只存名字列表进任务详情（commands），其余忽略
        names = [c.get('name') for c in (update.get('commands') or [])
                 if isinstance(c, dict) and c.get('name')]
        if names:
            with store.repo.lock:
                task['commands'] = names[:200]
                store.repo.touch()   # 批 PS2：非关键字段等 1 秒 flush
        return
    if kind == 'session_info_update':
        # Kimi 会用首轮 prompt 自动起标题（现在开头是平台约束），覆盖后任务名就没意义了。
        # 任务名始终用用户自己写的那句话。
        return
    if kind == 'usage_update':
        with store.repo.lock:
            task['usage'] = {'used': update.get('used'), 'size': update.get('size')}
        return
    # available_commands_update / current_mode_update 等：忽略

def _approvePermission(agent, requestId=''):
    """把挂起审批按"允许"放行（L2）：requestId 给空 = 当前挂起的那条。

    返回 (ok, 错误消息)。与 /permission 端点同一机制（置 permDecision +
    唤醒等待循环）——事件由等待循环落卡，不在这里重复写。
    """
    if not agent:
        return False, '任务没有活动会话，无法处理这条审批'
    with store.repo.lock:
        perm = agent.get('perm') or {}
        if not perm or (requestId and perm.get('requestId') != requestId):
            return False, '授权请求已失效（可能已超时或已处理）'
        optionId = next((str(o.get('optionId') or '') for o in perm.get('options') or []
                         if o.get('kind') in ('allow_once', 'allow_always')), '')
        if not optionId:
            return False, '这条审批没有可放行的选项'
        decision = agent.get('permDecision')
        if decision is None or decision.get('value') is not None:
            return False, '授权请求已失效（可能已超时或已处理）'
        decision['value'] = optionId
        ev = agent.get('permEvent')
    if ev is not None:
        ev.set()
    return True, ''

def _awaitPermissionCleared(task, timeout=2.0):
    """等等待循环跳出并清掉 pendingPermission（有界）；清了返回 True。

    不清（等待循环没在跑/卡住）也照常投递——审批侧已落定，消息作为普通
    下一条到达，最坏是晚一轮，不会丢。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store.repo.lock:
            if not task.get('pendingPermission'):
                return True
        time.sleep(0.05)
    return False

def answerPermission(task, requestId, text, by=''):
    """L2 答复入口（/answer 端点用）：审批按允许放行 + 答复 steer 立即投递。

    先放行再投递——steer 的队首消息会被 pendingPermission 闸门挡住
    （_popQueuedMessage），顺序反了答复就躺在队列里干等审批超时。
    返回 (payload, status)。
    """
    with store.repo.lock:
        agent = store.repo.agentOf(task.get('id') or '')
    ok, err = _approvePermission(agent, requestId)
    if not ok:
        return {'error': err}, 409
    _awaitPermissionCleared(task)
    from modules import tasks_api      # 延迟导入：tasks_api 模块级 import 本模块
    payload, code = tasks_api.deliverMessage(task, text, 'steer', by=by)
    payload = dict(payload or {})
    payload['answered'] = True
    return payload, code
def _afterWorklineTurn(task):
    """轮末收尾（R5-1a）：执行方改过 WORKLINE.md 才写回，超阈值提示轮换。"""
    if not task.get('workline'):
        return
    result = worklines_store.syncFromWorkspace(task)
    if result and result.get('rev'):
        store.repo.append_event(task, 'status',
                                text='接力棒已更新 rev %s' % result['rev'][:8])
    elif result and result.get('skipped'):
        store.repo.append_event(task, 'status',
                                text='接力棒在轮中被主控改过，本轮写回已跳过（不覆盖）')
    wl = worklines_store.repo.get((task.get('workline') or {}).get('id') or '')
    info = worklines_store.contextInfo(task)
    if not wl or not info:
        return
    high = info['ratio'] >= worklines_store.warnRatio(wl)
    with store.repo.lock:
        already = bool(task.get('contextHigh'))
        if not high and already and info['ratio'] < 0.5:
            task['contextHigh'] = False
            store.repo.touch()   # 批 PS2：非关键字段等 1 秒 flush
        elif high and not already:
            task['contextHigh'] = True
            store.repo.touch()
    if high and not already:
        store.repo.append_event(
            task, 'status',
            text=('上下文已用 %d/%d（%d%%），超过工作线阈值；可轮换会话：'
                  'POST /api/worklines/%s/rotate'
                  % (info['used'], info['max'], round(info['ratio'] * 100),
                     wl['id'])))


def _promptOnce(task, client, text, first=False):
    if task_drain.waitWhileDraining(task):
        return False        # 排空期间被关闭：这一轮不开，交回会话循环收尾
    if not first and task.get('workline'):
        tail = worklines_store.turnTail(task)
        if tail:
            text = text + '\n\n' + tail
    seq0 = int(task.get('seq') or 0)      # 本轮水位线：_markTurnKind 据此判空转
    store.repo.append_event(task, 'status', text='开始执行')
    with store.repo.lock:
        task_state.transition(task, task_state.TaskState.RUNNING)
        task['lastActiveAt'] = store._now()
        store.repo.save()
    result = client.prompt(text)
    _flushAssistant(task)   # 回合结束即落终版，订阅者（SSE/轮询）拿到完整文本
    _markTurnKind(task, seq0)
    _afterWorklineTurn(task)
    results = store._scanResults(task['workspace'])
    with store.repo.lock:
        task['stopReason'] = (result or {}).get('stopReason') or ''
        task['resultFiles'] = results
        task['lastActiveAt'] = store._now()
        if not task.get('closedByUser'):
            task_state.transition(task, task_state.TaskState.WAITING)
            task['awaitingAnswer'] = store._detectQuestion(store._lastAssistantText(task))
        store.repo.save()
    if not task.get('closedByUser'):
        # 批次 P3 触发点（2026-11-14）：轮次正常结束 → 主管收件箱 report
        turn = task.get('lastTurn') or {}
        report = '任务 %s（%s）本轮结束：工具 %d 个' % (
            task['id'][:8], (task.get('title') or '')[:40],
            int(turn.get('tools') or 0))
        if int(turn.get('finalLen') or 0):
            report += '，终稿 %d 字' % int(turn.get('finalLen'))
        leads_inbox.emit(task, 'report', report)
        # 批次 G1：有 expects 的任务，本轮（非取消/非错误）结束仍没交结构化
        # 结果 → 自动追发一次固定文案；追过仍没有 → 主管收件箱 result_missing
        if task.get('stopReason') not in ('cancelled', 'error'):
            nudge = task_result.nudgeText(task)
            if nudge:
                store.repo.append_event(task, 'user', text=nudge,
                                        by=task_result.NUDGE_BY)
                if _promptOnce(task, client, nudge):
                    store.repo.append_event(task, 'status',
                                            text='这轮结束，等你继续')
            else:
                task_result.noteMissing(task)
    return True


def _markTurnKind(task, seq0):
    """本轮形态（2026-11-13 早夭事件 + 2026-11-14 事件二/P1-4 反馈）：empty =
    0 工具 0 文本就结束（反复空转）；silent = 调了工具却一句终稿都没落（"干完
    活没交报告"）。text 看本轮窗口内的 assistant 事件——chunk 在流但没 flush
    出来对调用方就是没有终稿（lastText 跨轮残留，不能用）。finalLen 给主管
    收件箱的轮末汇报用（批次 P3：有终稿才报字数）。"""
    with store.repo.lock:
        evs = [e for e in task.get('events') or [] if int(e.get('seq') or 0) > seq0]
        tools = sum(1 for e in evs if e.get('type') == 'tool')
        said = any(e.get('type') == 'assistant' and (e.get('text') or '').strip() for e in evs)
        final = next((e.get('text') or '' for e in reversed(evs)
                      if e.get('type') == 'assistant'), '')
        task['lastTurn'] = {'tools': tools, 'finalLen': len(final),
                            'empty': tools == 0 and not said
                            and not (task.get('lastText') or '').strip(),
                            'silent': tools > 0 and not said, 'at': store._now()}
        store.repo.touch()   # 批 PS2：非关键字段等 1 秒 flush（轮末的
        # WAITING 变迁 save 会把它一起带走，见 _promptOnce）
    # 批次 P3 触发点（2026-11-14）：空转/静默轮 → 主管收件箱 failure
    if not task.get('closedByUser') and (task['lastTurn']['empty']
                                          or task['lastTurn']['silent']):
        leads_inbox.emit(task, 'failure', '任务 %s 上一轮%s'
                         % (task['id'][:8],
                            '空转（0 工具 0 文本）' if task['lastTurn']['empty']
                            else '静默（有工具无终稿）'))


def _popQueuedMessage(task):
    """弹出下一条排队消息（队首=steer 插话优先）；无可投递的返回 None。

    两个投递闸门：有 live 挂起审批时不投（R4-3c——旧审批 5 分钟超时拒绝
    会把刚开的新一轮带死，等审批落定后的唤醒信号再投）；用户已关闭任务
    不投。例外（L2）：挂起的审批是提问（AskUserQuestion）时，队首消息当作
    答复——按"允许"放行；投递由清完标记后的 backlog 信号触发再次 drain。
    """
    with store.repo.lock:
        if task.get('pendingPermission') or task.get('closedByUser'):
            if task.get('pendingPermission') and task.get('messageQueue'):
                agent = store.repo.agentOf(task.get('id') or '')
                perm = (agent.get('perm') if agent else None) or {}
                if perm.get('question'):
                    _approvePermission(agent)
            return None
        mq = task.get('messageQueue') or []
        if not mq:
            return None
        return mq.pop(0)


def _drainQueuedMessages(task, client):
    """把持久化队列 task['messageQueue'] 逐条投递，每条一轮 prompt
    （与手动续聊同链路）。轮中排队（R5-5）在每轮结束和每次唤醒后清队；
    steer 插话因在队首而优先投递。条目带 by 时转发进 user 事件（补 4，
    与直唤醒路径同字段）；老形状条目没这个键，行为不变。

    排空期间（批次 DR）不开新一轮：消息留在持久队列，排空结束后由
    _wakeQueuedAgents 的唤醒信号回来续投。"""
    n = 0
    while True:
        if task_drain.draining():
            return n
        msg = _popQueuedMessage(task)
        if msg is None:
            return n
        store.repo.append_event(task, 'user', text=msg.get('text') or '',
                                **({'steer': True} if msg.get('steer') else {}),
                                **({'by': msg['by']} if msg.get('by') else {}))
        if not _promptOnce(task, client, msg.get('text') or ''):
            return n
        store.repo.append_event(task, 'status', text='这轮结束，等你继续')
        n += 1


def _sessionThread(task, manifest, resume=False, firstMessage=None, clientFactory=KimiAcp):
    """一个任务 = 一个常驻 ACP 会话；每轮 prompt 结束后继续等用户消息。"""
    taskId = task['id']
    agent = {'queue': queue.Queue(), 'client': None, 'perm': None,
             'permDecision': None, 'permEvent': threading.Event(),
             'ready': not resume}
    client = None
    try:
        client = clientFactory(
            cwd=task_node.cwdOf(task) or task['workspace'],
            on_update=lambda update: (_handleAcpUpdate(task, update)
                                      if agent.get('ready') else None),
            on_permission=lambda params, rid: _handleAcpPermission(task, agent, params, rid),
            mode=task.get('mode') or 'auto',
            prompt_timeout=0)
        agent['client'] = client
        with store.repo.lock:
            store.repo.setRunning(taskId, client)
            store.repo.setAgent(taskId, agent)
        # 批次 G1：按 expects 注入 submit_result 的 MCP 服务（http 形式，
        # 每任务一个会话令牌）。推不出中枢地址（远端执行点且没配
        # agentMcpUrl/lanAddress）时不注入，落一条状态事件说明退路
        client.mcp_servers = task_result_mcp.mcpServersFor(task)
        if task.get('expects') and not client.mcp_servers:
            store.repo.append_event(task, 'status',
                         text='未注入 submit_result（未配置中枢地址）：执行方请用'
                              '技能包 deskport.py result --task %s --json <文件> 提交'
                              % taskId)
        sid = session_adopt.startSession(client, task)
        with store.repo.lock:
            task['sessionId'] = sid
            task['resume'] = True
            task['startedAt'] = task.get('startedAt') or store._now()
            # 旧进程的审批挂起随重启/唤醒失效（回放窗口里的旧审批已被
            # R4-5a 门禁取消）：不清会卡死队列投递闸门（R5-5）
            task['pendingPermission'] = ''
            store.repo.save()
        replayed = int(getattr(client, 'replayCount', 0) or 0)
        if replayed:
            # session/load 回放已被客户端门禁拦截（R4-5a），落一条汇总
            store.repo.append_event(task, 'status',
                                    text='已加载历史 %d 条' % replayed)
        store.repo.append_event(task, 'status',
                     text=('已接手既有 Kimi 会话' if task.get('kimiSessionId')
                           else '会话已接续' if resume else 'ACP 会话已建立'),
                     **({'model': task['model']} if task.get('model') else {}))
        with store.repo.lock:
            # liveFrom 之后的事件才是本轮 live 的（R4-5a #3）：wait 重建
            # pending 只认这条水位线之后产生的 permission_request
            task['liveFrom'] = task.get('seq', 0)
            store.repo.touch()   # 批 PS2：非关键字段等 1 秒 flush
        if resume:
            _flushAssistant(task)   # 上次中断未落卡的缓冲先补一条
            if firstMessage:
                agent['ready'] = True
                # 重启前排队未投的消息先投（入队时间比本次续聊更早）
                _drainQueuedMessages(task, client)
                store.repo.append_event(task, 'user', text=firstMessage)
                if _promptOnce(task, client, firstMessage):
                    store.repo.append_event(task, 'status',
                                            text='这轮结束，等你继续')
        else:
            firstText = store._promptFor(task, manifest,
                                         task.get('worklineBlock') or '')
            # 工作线任务把首条完整消息落卡：主控能核对接力棒真的进去了
            store.repo.append_event(
                task, 'user',
                text=firstText if task.get('workline')
                else (task.get('prompt') or ''))
            if _promptOnce(task, client, firstText, first=True):
                store.repo.append_event(task, 'status', text='这轮结束，等你继续')
        _drainQueuedMessages(task, client)   # 轮中排队的消息，轮一结束就投
        while True:
            item = agent['queue'].get()
            if item is None or item.get('type') == 'close':
                break
            if item.get('type') == 'message':
                text = (item.get('text') or '').strip()
                if text:
                    # 补 4：收件箱唤醒的消息显式记 by（不能落成"面板"——那会让
                    # 人以为是用户说的）；缺省不带这个键，行为不变
                    store.repo.append_event(task, 'user', text=text,
                                            **({'by': item['by']} if item.get('by') else {}))
                    if _promptOnce(task, client, text):
                        store.repo.append_event(task, 'status',
                                                text='这轮结束，等你继续')
            # 任何唤醒（message/drain 信号）后先清持久化队列（R5-5）
            _drainQueuedMessages(task, client)
    except Exception as e:
        _flushAssistant(task)   # 异常中断也保留已收到的部分回答
        with store.repo.lock:
            if task.get('closedByUser'):
                # 关闭竞态（G1 返工）：用户已关闭 → 直接落 CLOSED，不经过
                # FAILED——中间态会让结果接口在窗口里放行提交、事件流里
                # 留一条假的失败
                task_state.transition(task, task_state.TaskState.CLOSED)
                task['endedAt'] = store._now()
            else:
                task_state.transition(task, task_state.TaskState.FAILED)
                task['endedAt'] = store._now()
                task['error'] = str(e)[:2000]
                if client and client.stderr_lines:
                    task['error'] += '\n' + '\n'.join(client.stderr_lines[-10:])
            store.repo.save()
        if not task.get('closedByUser'):
            # 窗口竞态：上面写完 FAILED 后用户才关闭——错误事件不再落（用户
            # 关闭 supersede 这次失败），终态由 finally 修回 CLOSED
            store.repo.append_event(task, 'error', text=task.get('error') or str(e))
            log.warning('ACP task failed %s: %s', taskId, e)
    finally:
        # 批次 G1：会话线程退出 = MCP 注入的 submit_result 令牌随之作废
        # （任务关闭/失败/重启休眠都不再收提交；续跑时发新令牌）
        task_result_mcp.revokeTask(taskId)
        if client:
            try:
                client.close()
            except Exception:
                pass
            if task.get('closedByUser'):
                # 用户明确结束：node/联邦侧的 ACP 进程立刻回收，
                # 不再等 45 分钟空闲回收（第二轮反馈 §3）
                try:
                    client.shutdown()
                except Exception:
                    pass
        with store.repo.lock:
            store.repo.dropRunning(taskId)
            store.repo.dropAgent(taskId)
            # P0-3：会话线程结束 = 没有活的审批发起方，盘上残留的
            # pendingPermission 是死标记（/wait 会把它当活的回报，driver
            # 一 POST 就 409 再误判 STALE）。幂等清理，堵住线程异常退出
            # 漏清标记的路径（进程重启由 tasks_store.load 兜底）
            task['pendingPermission'] = ''
            if task.get('closedByUser'):
                task_state.transition(task, task_state.TaskState.CLOSED)
                task['endedAt'] = task.get('endedAt') or store._now()
                # 关闭竞态残值：窗口里写过的 error 被用户关闭 supersede
                task['error'] = ''
            elif task.get('status') in ('running', 'waiting'):
                task_state.transition(task, task_state.TaskState.PAUSED)
                task['resume'] = bool(task.get('sessionId'))
                task['lastActiveAt'] = store._now()
            task['resultFiles'] = store._scanResults(task['workspace'])
            store.repo.save()
        # 批次 P3 触发点（2026-11-14）：会话线程退出且 failed/interrupted/closed
        # → 主管收件箱 failure。closedByUser 的关闭由 closeTaskCore 报（不重复
        # 两条）；web 重启把在飞任务打 paused 不在这——每次部署给所有在飞下属
        # 刷一条没有意义（设计 §4 的 failure 清单也没有它）
        status = task.get('status') or ''
        if status in ('failed', 'interrupted') \
                or (status == 'closed' and not task.get('closedByUser')):
            err = (task.get('error') or '').strip()
            leads_inbox.emit(task, 'failure', '任务 %s %s%s' % (
                task['id'][:8], status,
                '：' + err.splitlines()[0][:120] if err else ''))


def _denyPendingPermission(agent):
    if not agent:
        return
    with store.repo.lock:
        decision = agent.get('permDecision')
        ev = agent.get('permEvent')
    if decision is not None and decision.get('value') is None:
        decision['value'] = '__deny__'
        try:
            if ev is not None:
                ev.set()
        except Exception:
            pass


RESUME_WATCHDOG_SEC = 90   # 唤醒看门狗：超时清唤醒态回 paused（R4-5 余项补遗）


def _resumeWithMessage(task, text):
    with store.repo.lock:
        if task.get('resuming'):
            raise RuntimeError('会话正在唤醒，请稍候')
        task['resuming'] = True
        task_state.transition(task, task_state.TaskState.QUEUED)
        store.repo.save()
    def run():
        try:
            from modules.agent_nodes import clientFactoryFor
            _sessionThread(task, [], True, text, clientFactory=clientFactoryFor(task))
        finally:
            with store.repo.lock:
                task['resuming'] = False
                store.repo.touch()   # 批 PS2：非关键字段等 1 秒 flush
    threading.Thread(target=run, daemon=True).start()

    # 唤醒看门狗（R4-3c 复测缺陷）：session/load 可能阻塞在被其他活进程
    # 持有的 kimi 会话锁上（如接手一个正被交互使用的会话）且无限等——
    # 超时清唤醒态、任务回 paused 并带上原因，调用方可改用 fork 接手
    def watchdog():
        deadline = time.monotonic() + RESUME_WATCHDOG_SEC
        while time.monotonic() < deadline:
            if task_drain.draining():
                # 排空期间唤醒等待（闸门）不计超时：否则部署一长，resume 会被
                # 看门狗打回 paused，与排空语义打架（批次 DR）
                deadline = time.monotonic() + RESUME_WATCHDOG_SEC
            with store.repo.lock:
                if not task.get('resuming') or task.get('status') != 'queued':
                    return   # 唤醒线程自己完成了/已进入其它状态
            time.sleep(1)
        with store.repo.lock:
            if not task.get('resuming'):
                return
            task['resuming'] = False
            if task.get('status') == 'queued':
                task_state.transition(task, task_state.TaskState.PAUSED)
            task['error'] = ('唤醒超时（%ds）：会话加载未完成，可能正被其他 '
                             'Kimi 进程占用——改用 fork 接手或稍后重试'
                             % RESUME_WATCHDOG_SEC)
            store.repo.append_event(task, 'error', text=task['error'])
            store.repo.save()
    threading.Thread(target=watchdog, daemon=True).start()


def _startTask(task, manifest):
    from modules.agent_nodes import executionKind
    task_engines.get(executionKind(task.get('node'))).start(task, manifest)


def _shutdownClients():
    with store.repo.lock:
        clients = store.repo.runningClients()
    for client in clients:
        try:
            if isinstance(client, KimiAcp):
                client.close()
        except Exception:
            pass


atexit.register(_shutdownClients)

# 执行点类型（local / agent / federation）由 modules/agent_nodes 注册，
# clientFactoryFor 统一选择本机 node / 远端节点 / 联邦成员 / web 内 stdio。
# 旧的分派方式已随跨版本兼容一起移除（部署面保持同版）。
