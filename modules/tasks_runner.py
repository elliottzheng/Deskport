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
from modules import sensitive_values
from modules import tasks_store as store
from modules import task_state, task_node
from modules import task_engines
from modules import worklines_store
from modules.kimi_acp import KimiAcp, extractText

log = logging.getLogger('tasks')

def maxRunning():
    """并发 running 上限（R4-9）：config.taskMaxRunning，缺省 8，0=不限。
    每次判定现读（config.load 按 mtime 热读），改 config.json 不重启即生效。"""
    return config.getTaskMaxRunning()

def _appendAssistantText(task, text):
    """回合内的 chunk 只累积缓冲（log 留完整流水），不写事件流。

    增量订阅者（SSE/轮询）按 seq 去重，若逐 chunk 合并进同一条事件，
    订阅者会停在首推时刻的部分文本。回合结束时 _flushAssistant 以单条
    完整事件落卡，订阅者天然拿到终版；log 流水仍逐 chunk 可追溯。
    """
    if not text:
        return
    with store.repo.lock:
        task['_assistantBuf'] = (task.get('_assistantBuf') or '') + text
        task['lastText'] = task['_assistantBuf'][:300]
        store.repo.save()
    store.repo.log_write(task, {'at': store._now(), 'type': 'assistant_chunk',
                                'text': text})


def _flushAssistant(task):
    """把回合缓冲落成一条完整 assistant 事件；无内容时不产生事件。"""
    with store.repo.lock:
        buf = task.pop('_assistantBuf', '') or ''
    if not buf:
        return
    store.repo.append_event(task, 'assistant', text=buf[:store.MAX_TEXT],
                            full=buf, truncated=len(buf) > store.MAX_TEXT)


def _argsText(args):
    """工具参数（对象/字符串）→ 事件里存的文本。"""
    return json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args


def _parseArgs(text):
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


def _rememberLateArgs(task, update):
    """参数后到（R5-9）：真实 Kimi 的流式形状是先来不带 rawInput 的
    tool_call，参数随后在 tool_call_update 的 content 文本里补，rawInput
    对象甚至晚于 request_permission 才到。

    按 callId 记下这两条线索，供审批回查 policy、给已落卡的 tool 事件补参
    数；返回 (callId, 可落盘的参数文本)，拿不到完整参数时文本为空。
    """
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
                parsed = _parseArgs(text)
                if parsed is not None:
                    argsText = json.dumps(parsed, ensure_ascii=False)
    return callId, argsText


def _backfillToolArgs(task, callId, argsText):
    """把后到的参数补进已落卡的 tool 事件（写入前同样脱敏，R5-2）。"""
    if not callId or not argsText:
        return
    text = sensitive_values.redactText(argsText)[:store.MAX_TEXT]
    with store.repo.lock:
        for event in reversed(task.get('events') or []):
            if event.get('type') != 'tool' or event.get('callId') != callId:
                continue
            if event.get('args'):
                return
            event['args'] = text
            event['truncated'] = len(argsText) > store.MAX_TEXT
            store.repo.save()
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
        store.repo.append_event(task, 'tool',
                     name=update.get('title') or update.get('kind') or 'tool',
                     args=argsText[:store.MAX_TEXT],
                     full=argsText,
                     truncated=len(argsText) > store.MAX_TEXT,
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
        store.repo.append_event(task, 'tool_result',
                     text=rawText[:store.MAX_TOOL_RESULT],
                     full=rawText,
                     truncated=len(rawText) > store.MAX_TOOL_RESULT,
                     callId=update.get('toolCallId') or '',
                     name=update.get('title') or '')
        return
    if kind == 'plan':
        entries = update.get('entries') or []
        lines = []
        for item in entries:
            if isinstance(item, dict):
                lines.append('%s %s' % (item.get('status') or '', item.get('content') or ''))
            else:
                lines.append(str(item))
        store.repo.append_event(task, 'plan', text='\n'.join(lines)[:store.MAX_TEXT])
        return
    if kind == 'available_commands_update':
        # R4-8：只存名字列表进任务详情（commands），其余忽略
        names = [c.get('name') for c in (update.get('commands') or [])
                 if isinstance(c, dict) and c.get('name')]
        if names:
            with store.repo.lock:
                task['commands'] = names[:200]
                store.repo.save()
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


BACKGROUND_DENY_TEXT = '后台任务被禁用，请在前台运行'
ORPHAN_DENY_TEXT = ('该审批的 callId 在本任务的工具调用记录里找不到'
                    '（后台幽灵审批），已自动拒绝')


def _knownToolCall(task, callId):
    """callId 是否在本任务的 tool 事件里出现过（R5-7 orphan 判定）。

    查两处：_toolArgs（tool_call update 记下的最近 50 个）与任务事件流里
    的 tool 事件（MAX_EVENTS 窗口内）。两处都没有 = 轮间幽灵审批。"""
    if not callId:
        return False
    with store.repo.lock:
        if callId in (task.get('_toolArgs') or {}):
            return True
        return any(e.get('type') == 'tool' and e.get('callId') == callId
                   for e in task.get('events') or [])

def _handleAcpPermission(task, agent, params, requestId):
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
            and not _knownToolCall(task, callId)
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
    # 空的不认，退回审批自带/流式文本里的真参数（R5-9：旧的"空也认"会盖掉它）
    if callId and byCall:
        rawInput = byCall               # 按 callId 精确回查：参数记下了就用
    if not rawInput and callId and streamed:
        parsed = _parseArgs(streamed)
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
    try:
        timeoutSec = int((policy or {}).get('timeoutSec')
                         if (policy or {}).get('timeoutSec') is not None
                         else config.getTaskPermissionTimeoutSec())
    except (TypeError, ValueError):
        timeoutSec = config.getTaskPermissionTimeoutSec()
    deadline = store._now() + timeoutSec if timeoutSec > 0 else None
    while True:
        with store.repo.lock:
            if decision['value'] is not None:
                break
            if task.get('closedByUser') or task.get('status') == 'closed':
                break
        if deadline is not None and store._now() >= deadline:
            break
        agent['permEvent'].wait(0.5)
        agent['permEvent'].clear()
    with store.repo.lock:
        value = decision['value']
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
    if value == '__deny__':
        store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                     status='denied', text='你拒绝了：' + title)
        return None
    store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                 status='approved', text='已允许：' + title)
    return value


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
            store.repo.save()
        elif high and not already:
            task['contextHigh'] = True
            store.repo.save()
    if high and not already:
        store.repo.append_event(
            task, 'status',
            text=('上下文已用 %d/%d（%d%%），超过工作线阈值；可轮换会话：'
                  'POST /api/worklines/%s/rotate'
                  % (info['used'], info['max'], round(info['ratio'] * 100),
                     wl['id'])))


def _promptOnce(task, client, text, first=False):
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


def _markTurnKind(task, seq0):
    """本轮形态（2026-11-13 早夭事件 + 2026-11-14 事件二/P1-4 反馈）：empty =
    0 工具 0 文本就结束（反复空转）；silent = 调了工具却一句终稿都没落（"干完
    活没交报告"）。text 看本轮窗口内的 assistant 事件——chunk 在流但没 flush
    出来对调用方就是没有终稿（lastText 跨轮残留，不能用）。"""
    with store.repo.lock:
        evs = [e for e in task.get('events') or [] if int(e.get('seq') or 0) > seq0]
        tools = sum(1 for e in evs if e.get('type') == 'tool')
        said = any(e.get('type') == 'assistant' and (e.get('text') or '').strip() for e in evs)
        task['lastTurn'] = {'tools': tools, 'empty': tools == 0 and not said
                            and not (task.get('lastText') or '').strip(),
                            'silent': tools > 0 and not said, 'at': store._now()}
        store.repo.save()


def _popQueuedMessage(task):
    """弹出下一条排队消息（队首=steer 插话优先）；无可投递的返回 None。

    两个投递闸门：有 live 挂起审批时不投（R4-3c——旧审批 5 分钟超时拒绝
    会把刚开的新一轮带死，等审批落定后的唤醒信号再投）；用户已关闭任务
    不投。"""
    with store.repo.lock:
        if task.get('pendingPermission') or task.get('closedByUser'):
            return None
        mq = task.get('messageQueue') or []
        if not mq:
            return None
        return mq.pop(0)


def _drainQueuedMessages(task, client):
    """把持久化队列 task['messageQueue'] 逐条投递，每条一轮 prompt
    （与手动续聊同链路）。轮中排队（R5-5）在每轮结束和每次唤醒后清队；
    steer 插话因在队首而优先投递。"""
    n = 0
    while True:
        msg = _popQueuedMessage(task)
        if msg is None:
            return n
        store.repo.append_event(task, 'user', text=msg.get('text') or '',
                                **({'steer': True} if msg.get('steer') else {}))
        _promptOnce(task, client, msg.get('text') or '')
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
        sid = client.start(session_id=task.get('sessionId') or None)
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
            store.repo.save()
        if resume:
            _flushAssistant(task)   # 上次中断未落卡的缓冲先补一条
            if firstMessage:
                agent['ready'] = True
                # 重启前排队未投的消息先投（入队时间比本次续聊更早）
                _drainQueuedMessages(task, client)
                store.repo.append_event(task, 'user', text=firstMessage)
                _promptOnce(task, client, firstMessage)
                store.repo.append_event(task, 'status', text='这轮结束，等你继续')
        else:
            firstText = store._promptFor(task, manifest,
                                         task.get('worklineBlock') or '')
            # 工作线任务把首条完整消息落卡：主控能核对接力棒真的进去了
            store.repo.append_event(
                task, 'user',
                text=firstText if task.get('workline')
                else (task.get('prompt') or ''))
            _promptOnce(task, client, firstText, first=True)
            store.repo.append_event(task, 'status', text='这轮结束，等你继续')
        _drainQueuedMessages(task, client)   # 轮中排队的消息，轮一结束就投
        while True:
            item = agent['queue'].get()
            if item is None or item.get('type') == 'close':
                break
            if item.get('type') == 'message':
                text = (item.get('text') or '').strip()
                if text:
                    store.repo.append_event(task, 'user', text=text)
                    _promptOnce(task, client, text)
                    store.repo.append_event(task, 'status', text='这轮结束，等你继续')
            # 任何唤醒（message/drain 信号）后先清持久化队列（R5-5）
            _drainQueuedMessages(task, client)
    except Exception as e:
        _flushAssistant(task)   # 异常中断也保留已收到的部分回答
        with store.repo.lock:
            task_state.transition(task, task_state.TaskState.FAILED)
            task['endedAt'] = store._now()
            task['error'] = str(e)[:2000]
            if client and client.stderr_lines:
                task['error'] += '\n' + '\n'.join(client.stderr_lines[-10:])
            store.repo.save()
        store.repo.append_event(task, 'error', text=task.get('error') or str(e))
        log.warning('ACP task failed %s: %s', taskId, e)
    finally:
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
            elif task.get('status') in ('running', 'waiting'):
                task_state.transition(task, task_state.TaskState.PAUSED)
                task['resume'] = bool(task.get('sessionId'))
                task['lastActiveAt'] = store._now()
            task['resultFiles'] = store._scanResults(task['workspace'])
            store.repo.save()


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
                store.repo.save()
    threading.Thread(target=run, daemon=True).start()

    # 唤醒看门狗（R4-3c 复测缺陷）：session/load 可能阻塞在被其他活进程
    # 持有的 kimi 会话锁上（如接手一个正被交互使用的会话）且无限等——
    # 超时清唤醒态、任务回 paused 并带上原因，调用方可改用 fork 接手
    def watchdog():
        deadline = time.monotonic() + RESUME_WATCHDOG_SEC
        while time.monotonic() < deadline:
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
