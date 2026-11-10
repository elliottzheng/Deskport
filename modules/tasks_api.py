# -*- coding: utf-8 -*-
"""任务路由层：/api/tasks 参数校验、调用 store/runner、返回 JSON 或文件。"""
import datetime
import json
import logging
import os
import shutil
import time
import uuid
from functools import wraps

from flask import (Blueprint, Response, g, jsonify, request, send_file,
                   stream_with_context)

from modules.kimi_acp import KimiAcp
from modules import tasks_runner as runner
from modules import task_state
from modules import tasks_store as store
from modules import fsutil
from modules import agent_nodes
from modules import events

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
    running = sum(1 for t in items if t.get('status') == 'running')
    queued = sum(1 for t in items if t.get('status') == 'queued')
    return jsonify({'tasks': [store.repo.summary(t) for t in items[:50]],
                    'running': running, 'queued': queued,
                    'countScope': 'all callers (全局)'})


@blueprint.route('/api/tasks', methods=['POST'])
def createTask():
    store.repo.load()
    data = request.get_json(force=True, silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    paths = data.get('paths') or []
    if not prompt:
        return jsonify({'error': '任务描述不能为空'}), 400
    if len(prompt) > 4000:
        return jsonify({'error': '任务描述过长（上限 4000 字符）——大文件请先'
                                 '上传到本机再用 paths 引用'}), 400
    if not isinstance(paths, list) or len(paths) > 20:
        return jsonify({'error': 'paths 必须是数组且不超过 20 项'}), 400
    engine = (data.get('engine') or 'acp').lower()
    if engine == 'p':
        engine = 'acp'   # 已废弃的旧引擎名，按 acp 处理
    if engine not in ('acp', 'remote'):
        return jsonify({'error': 'engine 只能是 acp / remote'}), 400
    # 不传 mode 时逐项审批（安全默认）——要自动批准必须显式传 auto
    mode = (data.get('mode') or 'default').lower()
    if mode not in ('auto', 'default', 'plan', 'yolo'):
        return jsonify({'error': 'mode 只能是 auto / default / plan / yolo'}), 400
    model = str(data.get('model') or '').strip()
    if model:
        if len(model) > 100 or any(c < ' ' for c in model):
            return jsonify({'error': 'model 不合法（≤100 字符，无控制字符）'}), 400
    node = ''
    remoteCwd = ''
    if engine == 'remote':
        node = str(data.get('node') or '').strip()
        if not node:
            return jsonify({'error': 'engine=remote 需要 node（执行点名）',
                            'nodes': agent_nodes.listNodeNames()}), 400
        if agent_nodes.findNode(node) is None \
                and agent_nodes.findFederationMember(node) is None:
            return jsonify({'error': '执行点不存在（agent node 或联邦成员）',
                            'nodes': agent_nodes.listNodeNames()}), 400
        remoteCwd = str(data.get('cwd') or '').strip()
        if not remoteCwd:
            return jsonify({'error': 'engine=remote 需要 cwd（远端工作目录）',
                            'hint': 'cwd 必须是执行点上已存在的目录'}), 400
        if len(remoteCwd) > 500:
            return jsonify({'error': 'cwd 过长'}), 400
        paths = []   # 远端任务不接受本机文件，跨机传输不在本引擎范围
    elif data.get('cwd'):
        return jsonify({'error': 'engine=acp 不接受 cwd（本机任务固定运行在'
                                 '独立工作区，输入文件用 paths 传入）'}), 400
    # 模型白名单：执行点声明了可选列表时必须命中（§7.1 按活的性质挑模型）
    decl = agent_nodes.nodeModelDecl(node or ('local' if engine == 'acp' else ''))
    if model and decl['models'] and model not in decl['models']:
        return jsonify({'error': '执行点 %s 不支持模型 %s' % (decl['name'], model),
                        'models': decl['models'],
                        'defaultModel': decl['default']}), 400
    with store.repo.lock:
        active = sum(1 for t in store.repo.list() if t.get('status') == 'running')
        if active >= runner.MAX_RUNNING:
            return jsonify({'error': '同时执行的任务过多，请稍后再试'}), 429
        taskId = uuid.uuid4().hex[:12]
        workspace = os.path.join(store.taskRoot(),
                                 datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                                 + '-' + store._slug(prompt) + '-' + taskId[:4])
    # 复制可能很慢，不能持有全局锁——否则列表/事件轮询全被阻塞
    attachments = data.get('attachments') or []
    if not isinstance(attachments, list) or len(attachments) > 20:
        return jsonify({'error': 'attachments 必须是数组且不超过 20 项'}), 400
    try:
        os.makedirs(workspace, exist_ok=False)
        manifest = store._collectSources(paths, workspace)
        manifest += store._saveAttachments(attachments, workspace)
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
        'mode': mode, 'model': model, 'resume': False, 'closedByUser': False,
        'pendingPermission': '', 'lastActiveAt': 0,
        'node': node, 'remoteCwd': remoteCwd,
        'sources': manifest,
    }
    with store.repo.lock:
        store.repo.put(task)
        store.repo.save()
    store.repo.append_event(task, 'status', text='任务已创建')
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        # 外部智能体经令牌通道建任务: 写系统审计（任务卡事件之外独立留痕）
        detail = prompt[:160] + (' · model=%s' % model if model else '')
        events.log('agent.task.create', tokenName, 'ok', detail,
                   request.remote_addr)
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
    # after 与 since 等价接受（外部反馈：手册写 since 实现是 after，别为难调用方）
    after = request.args.get('after') or request.args.get('since') or 0
    try:
        after = int(after)
    except ValueError:
        after = 0
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


@blueprint.route('/api/tasks/<taskId>/events/stream')
@taskRoute
def taskEventStream(task, taskId):
    """SSE：任务事件推送（cookie 与令牌均可）。

    轮询端点保留不变；本端点面向外部智能体的实时观察——
    事件到达即推，18s 心跳，closed/cancelled 后发 end 关流。
    """
    since = 0
    try:
        since = int(request.args.get('since') or 0)
    except ValueError:
        pass

    def gen():
        nonlocal since
        heartbeat = time.monotonic()
        try:
            while True:
                with store.repo.lock:
                    events = list(task.get('events') or [])
                    status = task.get('status') or ''
                for event in events:
                    if event.get('seq', 0) <= since:
                        continue
                    since = max(since, event.get('seq', 0))
                    yield 'data: %s\n\n' % json.dumps(event, ensure_ascii=False)
                if status in ('closed', 'cancelled'):
                    yield 'event: end\n\n'
                    return
                now = time.monotonic()
                if now - heartbeat >= 18:
                    yield ': keepalive\n\n'
                    heartbeat = now
                time.sleep(0.6)
        except GeneratorExit:
            return

    resp = Response(stream_with_context(gen()),
                    mimetype='text/event-stream; charset=utf-8')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


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
    return jsonify({'ok': True, 'status': task.get('status')})


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
        # 等托管线程落地终态（通常瞬时）；超时也如实返回当前状态
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            store.repo.load()
            with store.repo.lock:
                status = task.get('status') or ''
            if status in ('closed', 'cancelled', 'failed', 'paused',
                          'interrupted'):
                break
            time.sleep(0.2)
    else:
        task_state.transition(task, task_state.TaskState.CLOSED)
        task['endedAt'] = store._now()
        store.repo.append_event(task, 'status', text='会话已结束')
    # close 语义：返回的就是终态（已 cancel 的任务保持 cancelled，不会改判）
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


@blueprint.route('/api/tasks/files', methods=['POST'])
def uploadTaskFile():
    """智能体文件投递（§7b 过渡）：上传到本机暂存区，路径塞进 paths 用。

    等本机 exec/sftp 全等价上线后，这条只是便利通道，不是唯一路径。
    """
    data = request.get_json(force=True, silent=True) or {}
    name = store._safeAttachmentName(data.get('name'))
    if not name:
        return jsonify({'error': 'name 必须是纯文件名（无路径分隔符，≤120 字符）'}), 400
    try:
        raw = fsutil.decodeBase64(data.get('contentB64'))
    except ValueError:
        return jsonify({'error': 'contentB64 不是合法 base64（已拒绝写入）'}), 400
    if len(raw) > store.MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({'error': '文件超过 %dMB 上限' % store.MAX_UPLOAD_MB}), 413
    root = os.path.join(store.taskRoot(), '_uploads', uuid.uuid4().hex[:8])
    try:
        os.makedirs(root, exist_ok=True)
        path = fsutil.uniquePath(root, name)
        with open(path, 'wb') as f:
            f.write(raw)
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        events.log('agent.task.upload', tokenName, 'ok',
                   '%s (%d bytes)' % (name, len(raw)), request.remote_addr)
    return jsonify({'ok': True, 'path': os.path.abspath(path),
                    'size': len(raw),
                    'hint': '把该 path 放进 POST /api/tasks 的 paths 数组即可作为任务输入'})


def register(app):
    store._ensureRoot()
    store.repo.load()
    log.info('agent tasks root=%s', store.taskRoot())
