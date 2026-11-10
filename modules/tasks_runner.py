# -*- coding: utf-8 -*-
"""任务编排层：ACP 常驻会话、prompt 轮次、权限等待、取消/续跑。

不 import Flask；通过 tasks_store 读写状态。
"""
import atexit
import json
import logging
import queue
import threading

from modules import tasks_store as store
from modules import task_state
from modules import task_engines
from modules.kimi_acp import KimiAcp, extractText

log = logging.getLogger('tasks')

from modules.tasks_legacy import runTask

MAX_RUNNING = 2
PERMISSION_TIMEOUT = 300

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
        argsText = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
        with store.repo.lock:
            # 审批事件要能对回工具调用（反馈 §3.4）：ACP 的 permission 请求
            # 不带 toolCallId 时，退而取最近一次 tool_call
            task['_lastToolCall'] = {'callId': callId,
                                     'name': update.get('title') or '',
                                     'args': argsText[:2000]}
        store.repo.append_event(task, 'tool',
                     name=update.get('title') or update.get('kind') or 'tool',
                     args=argsText[:store.MAX_TEXT],
                     full=argsText,
                     truncated=len(argsText) > store.MAX_TEXT,
                     callId=callId, status=update.get('status') or '')
        return
    if kind == 'tool_call_update':
        if update.get('status') not in ('completed', 'failed'):
            return
        raw = update.get('rawOutput')
        if raw is None:
            raw = extractText(update.get('content'))
        rawText = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
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
    if kind == 'session_info_update':
        # Kimi 会用首轮 prompt 自动起标题（现在开头是平台约束），覆盖后任务名就没意义了。
        # 任务名始终用用户自己写的那句话。
        return
    if kind == 'usage_update':
        with store.repo.lock:
            task['usage'] = {'used': update.get('used'), 'size': update.get('size')}
        return
    # available_commands_update / current_mode_update 等：忽略


def _handleAcpPermission(task, agent, params, requestId):
    """把 ACP 的授权请求推到任务卡，等用户在前端点允许/拒绝。"""
    tool = params.get('toolCall') or {}
    title = tool.get('title') or '工具调用'
    callId = tool.get('toolCallId') or ''
    args = tool.get('rawInput')
    if args is None:
        with store.repo.lock:
            last = task.get('_lastToolCall') or {}
        callId = callId or last.get('callId') or ''
        args = last.get('args') or ''
    argsText = args if isinstance(args, str) \
        else json.dumps(args, ensure_ascii=False)
    options = []
    for opt in params.get('options') or []:
        options.append({'optionId': opt.get('optionId') or '',
                        'name': opt.get('name') or '',
                        'kind': opt.get('kind') or ''})
    if not options:
        store.repo.append_event(task, 'permission_result', requestId=str(requestId),
                     status='denied', text='没有可用选项，已拒绝：' + title)
        return None
    decision = {'value': None}
    with store.repo.lock:
        agent['perm'] = {'requestId': str(requestId), 'seq': 0,
                         'title': title, 'options': options,
                         'callId': callId, 'args': argsText[:2000]}
        agent['permDecision'] = decision
        agent['permEvent'] = threading.Event()
        task['pendingPermission'] = str(requestId)
        store.repo.save()
    ev = store.repo.append_event(task, 'permission_request', requestId=str(requestId),
                      text='等待你授权：' + title, name=title, options=options,
                      callId=callId, args=argsText[:store.MAX_TEXT])
    with store.repo.lock:
        if agent.get('perm'):
            agent['perm']['seq'] = ev.get('seq')
    deadline = store._now() + PERMISSION_TIMEOUT
    while store._now() < deadline:
        with store.repo.lock:
            if decision['value'] is not None:
                break
            if task.get('closedByUser') or task.get('status') == 'closed':
                break
        agent['permEvent'].wait(0.5)
        agent['permEvent'].clear()
    with store.repo.lock:
        value = decision['value']
        agent['perm'] = None
        task['pendingPermission'] = ''
        store.repo.save()
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


def _promptOnce(task, client, text):
    store.repo.append_event(task, 'status', text='开始执行')
    with store.repo.lock:
        task_state.transition(task, task_state.TaskState.RUNNING)
        task['lastActiveAt'] = store._now()
        store.repo.save()
    result = client.prompt(text)
    _flushAssistant(task)   # 回合结束即落终版，订阅者（SSE/轮询）拿到完整文本
    results = store._scanResults(task['workspace'])
    with store.repo.lock:
        task['stopReason'] = (result or {}).get('stopReason') or ''
        task['resultFiles'] = results
        task['lastActiveAt'] = store._now()
        if not task.get('closedByUser'):
            task_state.transition(task, task_state.TaskState.WAITING)
            task['awaitingAnswer'] = store._detectQuestion(store._lastAssistantText(task))
        store.repo.save()


def _sessionThread(task, manifest, resume=False, firstMessage=None, clientFactory=KimiAcp):
    """一个任务 = 一个常驻 ACP 会话；每轮 prompt 结束后继续等用户消息。"""
    taskId = task['id']
    agent = {'queue': queue.Queue(), 'client': None, 'perm': None,
             'permDecision': None, 'permEvent': threading.Event(),
             'ready': not resume}
    client = None
    try:
        client = clientFactory(
            cwd=task['workspace'],
            on_update=lambda update: (_handleAcpUpdate(task, update)
                                      if agent.get('ready') else None),
            on_permission=lambda params, rid: _handleAcpPermission(task, agent, params, rid),
            mode=task.get('mode') or 'auto',
            prompt_timeout=0)
        agent['client'] = client
        with store.repo.lock:
            store.repo.setRunning(taskId, client)
            store.repo.setAgent(taskId, agent)
        sid = client.start(session_id=task.get('sessionId') if resume else None)
        with store.repo.lock:
            task['sessionId'] = sid
            task['resume'] = True
            task['startedAt'] = task.get('startedAt') or store._now()
            store.repo.save()
        store.repo.append_event(task, 'status',
                     text='会话已接续' if resume else 'ACP 会话已建立',
                     **({'model': task['model']} if task.get('model') else {}))
        if resume:
            _flushAssistant(task)   # 上次中断未落卡的缓冲先补一条
            if firstMessage:
                agent['ready'] = True
                store.repo.append_event(task, 'user', text=firstMessage)
                _promptOnce(task, client, firstMessage)
                store.repo.append_event(task, 'status', text='这轮结束，等你继续')
        else:
            store.repo.append_event(task, 'user', text=task.get('prompt') or '')
            _promptOnce(task, client, store._promptFor(task, manifest))
            store.repo.append_event(task, 'status', text='这轮结束，等你继续')
        while True:
            item = agent['queue'].get()
            if item is None or item.get('type') == 'close':
                break
            if item.get('type') == 'message':
                text = (item.get('text') or '').strip()
                if not text:
                    continue
                store.repo.append_event(task, 'user', text=text)
                _promptOnce(task, client, text)
                store.repo.append_event(task, 'status', text='这轮结束，等你继续')
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
        with store.repo.lock:
            store.repo.dropRunning(taskId)
            store.repo.dropAgent(taskId)
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


def _startTask(task, manifest):
    task_engines.get(task.get('engine')).start(task, manifest)


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

# acp/remote 引擎由 modules/agent_nodes 注册（clientFactoryFor 统一选择
# 本机 node / 远端节点 / web 内 stdio）；这里只保留与智能体无关的 legacy。
task_engines.register('p', task_engines.ThreadEngine(runTask))
