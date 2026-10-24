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
    """把 ACP 的 message chunk 合并成一段可读文本，避免每个 token 一条事件。"""
    if not text:
        return
    mergedSeq = None
    fresh = False
    with store.repo.lock:
        events = task.setdefault('events', [])
        if events and events[-1].get('type') == 'assistant':
            last = events[-1]
            merged = (last.get('text') or '') + text
            last['text'] = merged[:store.MAX_TEXT]
            if len(merged) > store.MAX_TEXT:
                last['truncated'] = True
            task['lastText'] = merged[:300]
            mergedSeq = last.get('seq')
            store.repo.save()
        else:
            fresh = True
    if fresh:
        store.repo.append_event(task, 'assistant', text=text[:store.MAX_TEXT], full=text,
                     truncated=len(text) > store.MAX_TEXT)
        return
    # 任务卡合并显示, 但完整流水逐块落盘, 便于事后追溯
    store.repo.log_write(task, {'seq': mergedSeq, 'at': store._now(), 'type': 'assistant_chunk', 'text': text})


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
                         'title': title, 'options': options}
        agent['permDecision'] = decision
        agent['permEvent'] = threading.Event()
        task['pendingPermission'] = str(requestId)
        store.repo.save()
    ev = store.repo.append_event(task, 'permission_request', requestId=str(requestId),
                      text='等待你授权：' + title, name=title, options=options)
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
            task['engine'] = 'acp'
            task['resume'] = True
            task['startedAt'] = task.get('startedAt') or store._now()
            store.repo.save()
        store.repo.append_event(task, 'status', text='会话已接续' if resume else 'ACP 会话已建立')
        if resume:
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
            _sessionThread(task, [], True, text)
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

task_engines.register('acp', task_engines.ThreadEngine(_sessionThread))
task_engines.register('p', task_engines.ThreadEngine(runTask))
