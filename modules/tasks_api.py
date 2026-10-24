# -*- coding: utf-8 -*-
"""任务路由层：/api/tasks 参数校验、调用 store/runner、返回 JSON 或文件。"""
import datetime
import json
import logging
import os
import shutil
import uuid
from functools import wraps

from flask import Blueprint, jsonify, request, send_file

from modules.kimi_acp import KimiAcp
from modules import tasks_runner as runner
from modules import task_state
from modules import tasks_store as store
from modules import fsutil

blueprint = Blueprint('tasks', __name__)
log = logging.getLogger('tasks')


def taskRoute(fn):
    """统一「加载任务 → 404 → 注入 task」，路由体只留业务。"""
    @wraps(fn)
    def wrapper(taskId, *args, **kwargs):
        store.repo.load()
        task = store.repo.get(taskId)
        if not task:
            return jsonify({'error': '任务不存在'}), 404
        return fn(task, taskId, *args, **kwargs)
    return wrapper

@blueprint.route('/api/tasks', methods=['GET'])
def listTasks():
    store.repo.load()
    with store.repo.lock:
        items = sorted(store.repo.list(), key=lambda x: -x.get('createdAt', 0))
    return jsonify({'tasks': [store.repo.summary(t) for t in items[:50]],
                    'running': store.repo.runningCount()})


@blueprint.route('/api/tasks', methods=['POST'])
def createTask():
    store.repo.load()
    data = request.get_json(force=True, silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    paths = data.get('paths') or []
    if not prompt:
        return jsonify({'error': '任务描述不能为空'}), 400
    if len(prompt) > 4000:
        return jsonify({'error': '任务描述过长'}), 400
    if not isinstance(paths, list) or len(paths) > 20:
        return jsonify({'error': 'paths 必须是数组且不超过 20 项'}), 400
    engine = (data.get('engine') or 'acp').lower()
    if engine not in ('acp', 'p'):
        return jsonify({'error': 'engine 只能是 acp / p'}), 400
    mode = (data.get('mode') or 'auto').lower()
    if mode not in ('auto', 'default', 'plan', 'yolo'):
        return jsonify({'error': 'mode 只能是 auto / default / plan / yolo'}), 400
    with store.repo.lock:
        active = sum(1 for t in store.repo.list() if t.get('status') == 'running')
        if active >= runner.MAX_RUNNING:
            return jsonify({'error': '同时执行的任务过多，请稍后再试'}), 429
        taskId = uuid.uuid4().hex[:12]
        workspace = os.path.join(store.taskRoot(),
                                 datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                                 + '-' + store._slug(prompt) + '-' + taskId[:4])
    # 复制可能很慢，不能持有全局锁——否则列表/事件轮询全被阻塞
    try:
        os.makedirs(workspace, exist_ok=False)
        manifest = store._collectSources(paths, workspace)
    except (OSError, ValueError, shutil.Error) as e:
        try:
            shutil.rmtree(workspace, ignore_errors=True)
        except OSError:
            pass
        return jsonify({'error': str(e)}), 400
    task = {
        'id': taskId, 'title': prompt.splitlines()[0][:40],
        'prompt': prompt, 'workspace': workspace, 'status': 'queued',
        'createdAt': store._now(), 'startedAt': 0, 'endedAt': 0,
        'seq': 0, 'events': [], 'lastText': '',
        'resultFiles': [], 'error': '', 'sessionId': '', 'engine': engine,
        'mode': mode, 'resume': False, 'closedByUser': False,
        'pendingPermission': '', 'lastActiveAt': 0,
        'sources': manifest,
    }
    with store.repo.lock:
        store.repo.put(task)
        store.repo.save()
    store.repo.append_event(task, 'status', text='任务已创建')
    runner._startTask(task, manifest)
    return jsonify({'task': store.repo.summary(task)}), 202


@blueprint.route('/api/tasks/<taskId>', methods=['GET'])
@taskRoute
def getTask(task, taskId):
    out = store.repo.summary(task)
    out['events'] = task.get('events', [])[-500:]
    out['sources'] = task.get('sources', [])
    return jsonify({'task': out})


@blueprint.route('/api/tasks/<taskId>/events')
@taskRoute
def taskEvents(task, taskId):
    after = 0
    try:
        after = int(request.args.get('after') or 0)
    except ValueError:
        pass
    events = [e for e in task.get('events', []) if e.get('seq', 0) > after]
    return jsonify({'status': task.get('status'), 'events': events,
                    'lastText': task.get('lastText', ''),
                    'resultFiles': task.get('resultFiles', []),
                    'error': task.get('error', ''),
                    'pendingPermission': task.get('pendingPermission') or '',
                    'canContinue': (task.get('status') in ('waiting', 'paused')
                                    and bool(task.get('sessionId'))),
                    'canResume': (task.get('status') in ('failed', 'interrupted')
                                  and bool(task.get('sessionId'))),
                    'awaitingAnswer': bool(task.get('awaitingAnswer')),
                    'mode': task.get('mode') or 'auto'})


@blueprint.route('/api/tasks/<taskId>/cancel', methods=['POST'])
@taskRoute
def cancelTask(task, taskId):
    if task.get('status') not in ('queued', 'running'):
        return jsonify({'ok': True, 'status': task.get('status')})
    with store.repo.lock:
        client = store.repo.runningClient(taskId)
        agent = store.repo.agentOf(taskId)
    if isinstance(client, KimiAcp):
        runner._denyPendingPermission(agent)
        try:
            client.cancel()
        except Exception:
            pass
        store.repo.append_event(task, 'status', text='已请求停止当前这轮')
        return jsonify({'ok': True, 'status': task.get('status')})
    task_state.transition(task, task_state.TaskState.CANCELLED)
    task['endedAt'] = store._now()
    store.repo.append_event(task, 'status', text='任务已取消')
    return jsonify({'ok': True})


@blueprint.route('/api/tasks/<taskId>/message', methods=['POST'])
@taskRoute
def taskMessage(task, taskId):
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    if len(text) > 8000:
        return jsonify({'error': '消息过长'}), 400
    status = task.get('status')
    with store.repo.lock:
        agent = store.repo.agentOf(taskId)
    if agent:
        if status == 'running':
            return jsonify({'error': '这一轮还在执行，等结束或先点停止'}), 409
        if status not in ('waiting',):
            return jsonify({'error': '会话当前状态不能继续: %s' % status}), 409
        agent['queue'].put({'type': 'message', 'text': text})
        with store.repo.lock:
            task_state.transition(task, task_state.TaskState.RUNNING)
            task['awaitingAnswer'] = False
            store.repo.save()
        return jsonify({'ok': True, 'status': 'running'}), 202
    if status in ('closed', 'cancelled'):
        return jsonify({'error': '会话已结束，请新建任务'}), 409
    if not task.get('sessionId'):
        return jsonify({'error': '会话不可恢复，请新建任务'}), 409
    with store.repo.lock:
        task['awaitingAnswer'] = False
        if status in ('failed', 'interrupted'):
            task['error'] = ''
            task['resume'] = True
        store.repo.save()
    try:
        runner._resumeWithMessage(task, text)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 409
    return jsonify({'ok': True, 'status': 'queued'}), 202


@blueprint.route('/api/tasks/<taskId>/event/<int:seq>')
@taskRoute
def taskEvent(task, taskId, seq):
    path = store.repo.log_path(task)
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get('seq') != seq or obj.get('type') == 'assistant_chunk':
                        continue
                    return jsonify({'event': obj})
        except OSError:
            pass
    for e in task.get('events', []):
        if e.get('seq') == seq:
            return jsonify({'event': e})
    return jsonify({'error': '事件不存在'}), 404


@blueprint.route('/api/tasks/<taskId>/mode', methods=['POST'])
@taskRoute
def taskMode(task, taskId):
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get('mode') or '').lower()
    if mode not in ('auto', 'default', 'plan', 'yolo'):
        return jsonify({'error': 'mode 只能是 auto / default / plan / yolo'}), 400
    with store.repo.lock:
        if task.get('status') == 'running':
            return jsonify({'error': '这一轮还在执行，等结束再切换模式'}), 409
        task['mode'] = mode
        agent = store.repo.agentOf(taskId)
        store.repo.save()
    client = agent.get('client') if agent else None
    if isinstance(client, KimiAcp):
        try:
            client.set_mode(mode)
        except Exception as e:
            return jsonify({'error': str(e)[:300]}), 502
        store.repo.append_event(task, 'status', text='模式已切换: ' + mode)
    else:
        store.repo.append_event(task, 'status', text='模式将在下次唤醒时生效: ' + mode)
    return jsonify({'ok': True, 'mode': mode})


@blueprint.route('/api/tasks/<taskId>/close', methods=['POST'])
@taskRoute
def closeTask(task, taskId):
    with store.repo.lock:
        agent = store.repo.agentOf(taskId)
        task['closedByUser'] = True
    if agent:
        runner._denyPendingPermission(agent)
        agent['queue'].put({'type': 'close'})
        client = agent.get('client')
        if isinstance(client, KimiAcp):
            try:
                client.cancel()
            except Exception:
                pass
    else:
        task_state.transition(task, task_state.TaskState.CLOSED)
        task['endedAt'] = store._now()
        store.repo.append_event(task, 'status', text='会话已结束')
    return jsonify({'ok': True, 'status': task.get('status')})


@blueprint.route('/api/tasks/<taskId>/permission', methods=['POST'])
@taskRoute
def taskPermission(task, taskId):
    data = request.get_json(force=True, silent=True) or {}
    requestId = str(data.get('requestId') or '')
    optionId = str(data.get('optionId') or '')
    deny = bool(data.get('deny'))
    with store.repo.lock:
        agent = store.repo.agentOf(taskId)
        perm = agent.get('perm') if agent else None
        if not perm or perm.get('requestId') != requestId:
            return jsonify({'error': '授权请求已失效'}), 409
        if deny:
            optionId = '__deny__'
        else:
            valid = [o.get('optionId') for o in perm.get('options') or []]
            if optionId not in valid:
                return jsonify({'error': '无效的选项'}), 400
        agent['permDecision']['value'] = optionId
        agent['permEvent'].set()
    return jsonify({'ok': True})


@blueprint.route('/api/tasks/<taskId>/log')
@taskRoute
def taskLog(task, taskId):
    path = store.repo.log_path(task)
    if not os.path.isfile(path):
        return jsonify({'error': '暂无日志'}), 404
    download = request.args.get('download') in ('1', 'true')
    return send_file(path, mimetype='application/x-ndjson', as_attachment=download,
                     download_name=taskId + '.jsonl')


@blueprint.route('/api/tasks/<taskId>/download')
@taskRoute
def downloadResult(task, taskId):
    rel = request.args.get('path') or ''
    workspace = os.path.abspath(task['workspace'])
    full = os.path.abspath(os.path.join(workspace, rel))
    if not fsutil.isUnder(full, workspace):
        return jsonify({'error': '非法路径'}), 403
    if not os.path.isfile(full):
        return jsonify({'error': '文件不存在'}), 404
    return send_file(full, as_attachment=True, download_name=os.path.basename(full))


def register(app):
    store._ensureRoot()
    store.repo.load()
    log.info('agent tasks root=%s', store.taskRoot())
