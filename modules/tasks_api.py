# -*- coding: utf-8 -*-
"""任务路由层：/api/tasks 参数校验、调用 store/runner、返回 JSON 或文件。"""
import datetime
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
from functools import wraps

from flask import (Blueprint, Response, g, jsonify, request, send_file,
                   stream_with_context)

from modules.kimi_acp import KimiAcp
from modules import kimi_acp
from modules import task_policy
from modules import tasks_runner as runner
from modules import task_state
from modules import tasks_store as store
from modules import worklines_store
from modules import fsutil, node_path, staging, task_cwd, task_node
from modules import agent_nodes
from modules import model_availability
from modules import events
from modules import task_inputs
from modules.task_wait import _blockWait, _parseWaitSeconds, _waitSnapshot

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


def _agentOnBehalfOf():
    """on_behalf_of 关键字（批次 H）：authGuard 放行令牌时记下它代表谁；
    没有就不传——events.log 缺省空串，老日志解析不受影响。"""
    who = str(getattr(g, 'agentOnBehalfOf', '') or '').strip()
    return {'on_behalf_of': who} if who else {}

@blueprint.route('/api/tasks', methods=['GET'])
def listTasks():
    store.repo.load()
    full = request.args.get('full') == '1'
    with store.repo.lock:
        items = sorted(store.repo.list(), key=lambda x: -x.get('createdAt', 0))
    running = sum(1 for t in items if t.get('status') == 'running')
    queued = sum(1 for t in items if t.get('status') == 'queued')
    tasks = [store.repo.summary(t) if full else store.repo.summaryBrief(t)
             for t in items[:50]]
    return jsonify({'tasks': tasks, 'running': running, 'queued': queued,
                    'limit': runner.maxRunning(),
                    'countScope': 'all callers (全局)'})


def _mergeWorklineDefaults(data, wl):
    """工作线给的默认执行点/目录/模型/模式/边界；请求里显式给的优先。

    旧工作线的字段映射在 task_node 里做一次；旧远端线缺 node 直接报错让人补，
    不能悄悄落到本机。返回 (data, errorPayload)，无错时 errorPayload 为 None。
    """
    data = dict(data)
    wlNode, missing = task_node.worklineNode(wl)
    if not str(data.get('node') or '').strip():
        data['node'] = wlNode
        if missing:
            return data, {'error': '工作线 %s 记录的是旧版远端执行点但缺少 node，'
                                   '请先 PUT /api/worklines/%s 补 node'
                                   % ((wl.get('id') or ''), (wl.get('id') or ''))}
    if not data.get('cwd') and wl.get('cwd'):
        data['cwd'] = wl['cwd']
    defaults = wl.get('defaults') or {}
    for key in ('model', 'mode'):
        if not data.get(key) and defaults.get(key):
            data[key] = defaults[key]
    if not data.get('policy') and wl.get('policy'):
        data['policy'] = wl['policy']
    return data, None


@blueprint.route('/api/tasks', methods=['POST'])
def createTask():
    data = request.get_json(force=True, silent=True) or {}
    payload, code = createTaskResponse(data)
    return jsonify(payload), code

def createTaskResponse(data):
    """建任务核心（/api/tasks 与 /api/worklines/<id> 的派活共用）。

    返回 (payload, status)。工作线（R5-1a）在这里合并默认值、注入接力棒。
    """
    store.repo.load()
    data = dict(data or {})
    if (legacy := task_node.rejectLegacyEngine(data)):
        return {'error': legacy}, 400
    worklineId = str(data.get('workline') or data.get('worklineId') or '').strip()
    workline = None
    if worklineId:
        workline = worklines_store.repo.get(worklineId)
        if workline is None:
            return {'error': '工作线不存在: %s' % worklineId}, 404
        data, wlError = _mergeWorklineDefaults(data, workline)
        if wlError:
            return wlError, 400
    prompt = (data.get('prompt') or '').strip()
    paths = data.get('paths') or []
    if not prompt:
        return ({'error': '任务描述不能为空'}), 400
    if len(prompt) > 4000:
        return ({'error': '任务描述过长（上限 4000 字符）——大文件请先'
                                 '上传到本机再用 paths 引用'}), 400
    if not isinstance(paths, list) or len(paths) > 20:
        return ({'error': 'paths 必须是数组且不超过 20 项'}), 400
    node = task_node.normalize(data.get('node'))
    local = task_node.isLocal(node)
    # 不传 mode 时逐项审批（安全默认）——要自动批准必须显式传 auto
    mode = (data.get('mode') or 'default').lower()
    if mode not in ('auto', 'default', 'plan', 'yolo'):
        return ({'error': 'mode 只能是 auto / default / plan / yolo'}), 400
    model = str(data.get('model') or '').strip()
    if model:
        if len(model) > 100 or any(c < ' ' for c in model):
            return ({'error': 'model 不合法（≤100 字符，无控制字符）'}), 400
    taskCwd = remoteCwd = ''
    try:
        policy = task_policy.validatePolicy(data.get('policy'))
        if local:
            taskCwd = task_cwd.resolve(data.get('cwd'))
    except ValueError as e:
        return ({'error': str(e)}), 400
    # P0-1（外部智能体反馈 2026-11-14，已实测复现）：mode=auto/yolo 时 hub 让
    # 节点侧 kimi 自行批准一切（session/set_mode），policy 漏斗收不到调用——
    # denyCommands 被静默作废，"外推前停下来审批"的承诺落空。显式拒绝这对组合。
    if mode in ('auto', 'yolo') and (policy.get('denyCommands') or []):
        return ({'error': 'mode=%s 与 denyCommands 矛盾：该模式下执行方自行批准'
                          '所有调用，denyCommands 不会生效（已复现：echo 直接被'
                          '放行）。要强制人工审批请用 mode=default；要全自动就'
                          '不要配 denyCommands' % mode}), 400
    if not local:
        if agent_nodes.findNode(node) is None \
                and agent_nodes.findFederationMember(node) is None:
            return ({'error': '执行点不存在（agent node 或联邦成员）',
                            'nodes': agent_nodes.listNodeNames()}), 400
        remoteCwd = str(data.get('cwd') or '').strip()
        if not remoteCwd:
            return ({'error': '执行点 %s 需要 cwd（该机器上的工作目录）' % node,
                            'hint': 'cwd 必须是执行点上已存在的目录（由执行点校验）'}), 400
        if len(remoteCwd) > 500:
            return ({'error': 'cwd 过长'}), 400
        # 批次 NODE-OS：按执行点自报平台前置校验 cwd 语法——Windows 执行点
        # 收到 /tmp 时 Popen(cwd=...) 抛的 FileNotFoundError 会被对端归成
        # 「找不到 kimi 命令」，根因就在这；os 未知（老 node/老成员）不校验
        cwdErr = node_path.cwdError(node, remoteCwd)
        if cwdErr:
            return ({'error': cwdErr}), 400
        # 批次 B3：cwd 还必须落在执行点自报的允许根之下（cwdRoots 非空才
        # 校验；空 = 未知，沿用只查语法）。原来远端 cwd 能指到对端任何可写
        # 目录，附件也会落进 <cwd>/.deskport-inputs/<taskId>/——先拦在建卡前
        rootsErr = node_path.cwdRootsError(node, remoteCwd)
        if rootsErr:
            return ({'error': rootsErr}), 400
        # R5-1a：联邦执行点的首条消息受对端 4000 上限约束，接力棒无法保证
        # 到达；先明确拒绝，别让工作线悄悄丢棒（R5-1b 再补）
        if workline and agent_nodes.findFederationMember(node) is not None:
            return ({'error': '工作线暂不支持联邦执行点（R5-1b 再做），'
                              '请改用 agent node'}), 400
        # 非本机的 paths/attachments 不丢弃：下面经 task_inputs 投到执行点机器
    # 接手已有 Kimi CLI 会话（第二轮反馈 §2）：默认先 fork 复制，
    # 避免与用户开着的终端并发写同一会话；fork:false 且会话未被占用才直接接管
    kimiSessionId = str(data.get('kimiSessionId') or '').strip()   # 用户传的原会话
    kimiSessionFork = False
    loadedSessionId = ''                                           # 实际加载的会话
    if kimiSessionId:
        if not re.match(r'^session_[0-9a-f-]{36}$', kimiSessionId):
            return ({'error': 'kimiSessionId 格式不合法（应为 session_<uuid>，'
                            'GET /api/tasks/kimi-sessions 可列出候选）'}), 400
        fork = data.get('fork', True)
        if isinstance(fork, str):
            fork = fork.strip().lower() != 'false'
        kimiSessionFork = bool(fork)
        if local:
            loadedSessionId = kimiSessionId
            if fork:
                try:
                    loadedSessionId = kimi_acp.forkSession(kimiSessionId)
                except RuntimeError as e:
                    return ({'error': str(e),
                                    'hint': '会话可能正被占用（先关掉那个终端）或不存在'}), 400
            else:
                store.repo.load()
                with store.repo.lock:
                    live = [t for t in store.repo.list()
                            if t.get('sessionId') == loadedSessionId
                            and t.get('status') in ('queued', 'running', 'waiting')]
                if live:
                    return ({'error': '该会话已被任务 %s 接管，不能再次直接接管'
                                    % live[0]['id'],
                                    'hint': '去掉 fork:false 改用 fork 副本'}), 409
        # 非本机：kimiSessionId/fork 原样透传到执行点侧 fork+load（R4-7）
    # 模型白名单：执行点声明了可选列表时必须命中（§7.1 按活的性质挑模型）
    decl = agent_nodes.nodeModelDecl(node)
    if model and decl['models'] and model not in decl['models']:
        return ({'error': '执行点 %s 不支持模型 %s' % (decl['name'], model),
                        'models': decl['models'],
                        'defaultModel': decl['default']}), 400
    # R4-10 模型可用性门禁：24h 内探测失败的直接拒绝，省一次必然失败的派活；
    # 没探测过的（usable=null）照常放行
    gateErr, gateAvail = model_availability.modelGate(node, model)
    if gateErr:
        return ({'error': gateErr,
                        'usableModels': gateAvail['usableModels'],
                        'model': model,
                        'probedModelStatus': (gateAvail['modelStatus'].get(model))}), 400
    # 实际生效的模型：用户没传就落执行点默认（可追溯"这个结论是哪个模型给的"）
    modelEffective = model or decl['default']
    with store.repo.lock:
        limit = runner.maxRunning()
        running = [t for t in store.repo.list() if t.get('status') == 'running']
        if limit > 0 and len(running) >= limit:
            # 429 带占用明细（R4-9）：调用方不必再查一轮就知道是谁占着名额
            holders = [{'id': t['id'], 'owner': t.get('owner') or '未知',
                        'title': t.get('title') or ''} for t in running]
            return ({'error': '并发任务已达上限 %d，请稍后再试' % limit,
                            'limit': limit, 'running': len(running),
                            'holders': holders}), 429
        taskId = uuid.uuid4().hex[:12]
        workspace = os.path.join(store.taskRoot(),
                                 datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                                 + '-' + store._slug(prompt) + '-' + taskId[:4])
    # 复制/投递可能很慢，不能持有全局锁——否则列表/事件轮询全被阻塞
    attachments = data.get('attachments') or []
    if not isinstance(attachments, list) or len(attachments) > 20:
        return ({'error': 'attachments 必须是数组且不超过 20 项'}), 400
    inputs = list(paths)          # 实际给执行方的输入（远端 = 投递后的绝对路径）
    try:
        os.makedirs(workspace, exist_ok=False)
        if local:
            manifest = store._collectSources(paths, workspace)
            manifest += store._saveAttachments(attachments, workspace)
        else:
            manifest = []
            if inputs or attachments:
                note = '附件已投递到 %s' % task_inputs.dirFor(
                    node, remoteCwd, taskId)
                if len(prompt) + 1 + len(note) > 4000:
                    raise task_inputs.DeliverError(
                        '任务描述加上附件位置后超过 4000 字符', 400)
                delivered = task_inputs.deliver(node, remoteCwd, taskId,
                                                inputs, attachments)
                inputs = delivered['paths']
                prompt = prompt + '\n' + note
    except task_inputs.DeliverError as e:
        shutil.rmtree(workspace, ignore_errors=True)
        return ({'error': str(e)}), e.status
    except (OSError, ValueError, shutil.Error) as e:
        try:
            shutil.rmtree(workspace, ignore_errors=True)
        except OSError:
            pass
        return ({'error': str(e)}), 400
    task = {
        'id': taskId, 'title': prompt.splitlines()[0][:40],
        'prompt': prompt, 'workspace': workspace, 'status': 'queued',
        'createdAt': store._now(), 'startedAt': 0, 'endedAt': 0,
        'seq': 0, 'events': [], 'lastText': '',
        'resultFiles': [], 'error': '', 'sessionId': loadedSessionId,
        'kimiSessionId': kimiSessionId,
        'kimiSessionFork': kimiSessionFork,
        'owner': getattr(g, 'agentTokenName', '') or '面板',
        'mode': mode, 'model': model, 'modelEffective': modelEffective,
        'policy': policy,
        'resume': False, 'closedByUser': False,
        'pendingPermission': '', 'lastActiveAt': 0,
        'node': node,
        'cwd': (taskCwd or workspace) if local else remoteCwd,
        'sources': manifest,
    }
    injected = ''
    if workline:
        # 接力棒注入（R5-1a）：预算按"首条消息总长 ≤ PROMPT_LIMIT"算，
        # 正文放不下就降级成摘要 + 指路工作区的 WORKLINE.md
        doc = worklines_store.repo.readDoc(workline)
        base = store._promptFor(task, manifest)
        _block, injected = worklines_store.attach(task, workline, doc, len(base))
    with store.repo.lock:
        store.repo.put(task)
        store.repo.save()
    store.repo.append_event(task, 'status', text='任务已创建')
    if workline:
        store.repo.append_event(task, 'status',
                                text='工作线 %s（%s）接力棒已注入 rev %s（%s）'
                                % (workline['id'], workline.get('title') or '',
                                   (workline.get('rev') or '')[:8], injected))
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        # 外部智能体经令牌通道建任务: 写系统审计（任务卡事件之外独立留痕）
        detail = prompt[:160] + (' · model=%s' % model if model else '')
        events.log('agent.task.create', tokenName, 'ok', detail,
                   request.remote_addr, **_agentOnBehalfOf())
    runner._startTask(task, manifest)
    waitSec = _parseWaitSeconds(data.get('wait'))
    if waitSec:
        # 发完直接阻塞到回合结束/需决策（R4-3），省一次轮询往返；
        # task: summaryBrief 让调用方不重查任务卡就能拿到元数据
        snap = _blockWait(task, 0, waitSec)
        snap['task'] = store.repo.summaryBrief(task)
        snap['paths'] = inputs
        return (snap), 200
    return ({'task': store.repo.summary(task), 'paths': inputs}), 202


@blueprint.route('/api/tasks/<taskId>', methods=['GET'])
@taskRoute
def getTask(task, taskId):
    out = store.repo.summary(task)
    out['sources'] = task.get('sources', [])
    # R4-8：执行方状态可观测
    usage = task.get('usage') or {}
    if usage.get('used') is not None:
        out['context'] = {'used': usage.get('used'), 'max': usage.get('size')}
    out['commands'] = task.get('commands') or []
    out['lastSeq'] = task.get('seq', 0)
    out['currentTool'] = task.get('_currentTool') or None
    out['queuedMessages'] = len(task.get('messageQueue') or [])
    if request.args.get('events') == '1':
        # 默认不带 events（调用方字节预算）；增量订阅走 /events 端点，
        # ?events=1 保留旧行为供一次性全量对账
        out['events'] = task.get('events', [])[-500:]
    return jsonify({'task': out})


@blueprint.route('/api/tasks/<taskId>/wait')
@taskRoute
def taskWait(task, taskId):
    try:
        after = int(request.args.get('after') or 0)
    except ValueError:
        after = 0
    timeout = _parseWaitSeconds(request.args.get('timeout'), cap=600) or 300
    return jsonify(_blockWait(task, after, timeout))


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
    # R4-0：types=逗号分隔过滤；maxText=n 把 text 截到 n 字并带
    # truncated/textLen——大字段按需取，缺省行为不变
    typesArg = request.args.get('types') or ''
    if typesArg.strip():
        wanted = {t.strip() for t in typesArg.split(',') if t.strip()}
        events = [e for e in events if e.get('type') in wanted]
    maxTextArg = request.args.get('maxText')
    if maxTextArg is not None:
        try:
            maxText = int(maxTextArg)
        except ValueError:
            return jsonify({'error': 'maxText 必须是非负整数'}), 400
        if maxText < 0:
            return jsonify({'error': 'maxText 必须是非负整数'}), 400
        trimmed = []
        for e in events:
            text = e.get('text')
            if isinstance(text, str) and len(text) > maxText:
                e = dict(e)
                e['text'] = text[:maxText]
                e['truncated'] = True
                e['textLen'] = len(text)
            trimmed.append(e)
        events = trimmed
    return jsonify({'status': task.get('status'), 'events': events,
                    'lastText': task.get('lastText', ''),
                    'resultFiles': task.get('resultFiles', []),
                    'error': task.get('error', ''),
                    'queuedMessages': len(task.get('messageQueue') or []),
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
    if client is not None:
        # R4-5/缺陷修复：cancel 只结束当前这一轮（node/联邦执行点与 stdio 同
        # 语义）——轮结束后 runner 把任务置回 waiting，任务保持可续聊；
        # 不再终态化成 cancelled（那会让后续 message 一律 409）
        runner._denyPendingPermission(agent)
        try:
            client.cancel()
        except Exception:
            pass
        store.repo.append_event(task, 'status', text='已请求停止当前这轮')
        return jsonify({'ok': True, 'status': task.get('status')})
    # 没有活客户端（web 重启后的残留 running 等）：无法只停这一轮，保留终态取消
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
    mode = (data.get('mode') or 'queue').lower()
    if mode not in ('queue', 'steer', 'reject'):
        return jsonify({'error': 'mode 只能是 queue / steer / reject'}), 400
    payload, code = deliverMessage(task, text, mode,
                                   _parseWaitSeconds(data.get('wait')))
    return jsonify(payload), code


def deliverMessage(task, text, mode='queue', waitSec=None):
    """投递一条消息到任务（/message 与工作线自动路由共用）。

    R5-5 轮中插话：queue（默认）= 本轮结束后自动投递（服务端持久化，web
    重启不丢）；steer = 插队首并打断当前轮（stopReason=cancelled 落卡后
    立即投递）；reject = 保留旧行为（轮中 409）。mode 只在会 409 的场景
    生效：waiting 直接投、paused 走续聊唤醒，均不受影响。
    返回 (payload, status)。
    """
    taskId = task.get('id') or ''
    status = task.get('status')
    with store.repo.lock:
        agent = store.repo.agentOf(taskId)
    # live 挂起审批：reject 保持 409 带 pending（R4-3c #3 原行为）；
    # queue/steer 照常入队——投递时机由 runner 的审批闸门兜住（审批不落定
    # 不投，否则旧审批 5 分钟超时拒绝会把刚开的新一轮带死，R4-3c #3）
    pending = _waitSnapshot(task, task.get('seq', 0) or 0).get('pending')
    if pending and mode == 'reject':
        return ({'error': '有等待处理的审批（可能是上一轮的后台调用），'
                           '先通过 /permission 批准或拒绝，再续聊',
                 'pending': pending}), 409
    if agent:
        if status == 'running':
            if mode == 'reject':
                return ({'error': '这一轮还在执行，等结束或先点停止'}), 409
            steer = mode == 'steer'
            item, pos = _enqueueMessage(task, text, steer)
            if steer:
                # 插话语义：打断当前轮，队首消息在轮停止后立即投递。
                # 与 /cancel 同链路：先拒掉挂起审批再 cancel，防止旧审批
                # 超时拒绝殃及下一轮
                runner._denyPendingPermission(agent)
                client = agent.get('client')
                if client is not None:
                    try:
                        client.cancel()
                    except Exception:
                        pass
                store.repo.append_event(task, 'status',
                             text='主控插话，正在打断当前轮（队首投递）')
            else:
                store.repo.append_event(task, 'status',
                             text='消息已排队（第 %d 位，本轮结束后投递）' % pos)
            # 唤醒信号：轮中时轮结束被消费，与轮结束竞态（恰好已 waiting）
            # 时立即唤醒清队——多余信号无害
            agent['queue'].put({'type': 'drain'})
            return ({'queued': True, 'position': pos, 'steered': steer,
                     'messageId': item.get('id') or ''}), 202
        if status not in ('waiting',):
            return ({'error': '会话当前状态不能继续: %s' % status}), 409
        if pending:
            # waiting 但有轮间挂起审批（后台调用残响，R4-3c）：入持久化队列，
            # 审批落定后由 runner 的唤醒信号投递（闸门见 _popQueuedMessage）
            item, pos = _enqueueMessage(task, text)
            store.repo.append_event(task, 'status',
                         text='消息已排队（等审批落定后投递，第 %d 位）' % pos)
            agent['queue'].put({'type': 'drain'})
            return ({'queued': True, 'position': pos, 'steered': False,
                     'messageId': item.get('id') or ''}), 202
        agent['queue'].put({'type': 'message', 'text': text})
        with store.repo.lock:
            task_state.transition(task, task_state.TaskState.RUNNING)
            task['awaitingAnswer'] = False
            store.repo.save()
        if waitSec:
            return _blockWait(task, task.get('seq', 0), waitSec), 200
        return ({'ok': True, 'status': 'running'}), 202
    if status in ('closed', 'cancelled'):
        return ({'error': '会话已结束，请新建任务'}), 409
    if not task.get('sessionId'):
        return ({'error': '会话不可恢复，请新建任务'}), 409
    with store.repo.lock:
        task['awaitingAnswer'] = False
        if status in ('failed', 'interrupted'):
            task['error'] = ''
            task['resume'] = True
        store.repo.save()
    afterSeq = task.get('seq', 0)
    try:
        runner._resumeWithMessage(task, text)
    except RuntimeError as e:
        return ({'error': str(e)}), 409
    if waitSec:
        return _blockWait(task, afterSeq, waitSec), 200
    return ({'ok': True, 'status': 'queued'}), 202


def _enqueueMessage(task, text, steer=False):
    """消息入持久化队列（R5-5）：steer 插队首，其余追加队尾。

    队列挂在 task dict 上，随 repo.save() 落盘——web 重启后 resume 时由
    runner 清队投递，主控客户端不再需要自己排队。返回 (item, position)。"""
    item = {'id': uuid.uuid4().hex[:12], 'text': text, 'at': store._now(),
            'by': getattr(g, 'agentTokenName', '') or '面板',
            'steer': bool(steer)}
    with store.repo.lock:
        mq = task.setdefault('messageQueue', [])
        if steer:
            mq.insert(0, item)
        else:
            mq.append(item)
        store.repo.save()
        return item, mq.index(item) + 1


@blueprint.route('/api/tasks/<taskId>/messages', methods=['GET'])
@taskRoute
def taskQueuedMessages(task, taskId):
    """排队中的消息（只列未投递的；已投递的看事件流的 user 事件）。"""
    with store.repo.lock:
        items = [dict(m) for m in (task.get('messageQueue') or [])]
    return jsonify({'items': items, 'count': len(items)})


@blueprint.route('/api/tasks/<taskId>/messages/<mid>', methods=['DELETE'])
@taskRoute
def taskQueuedMessageDelete(task, taskId, mid):
    with store.repo.lock:
        mq = task.get('messageQueue') or []
        idx = next((i for i, m in enumerate(mq)
                    if m.get('id') == mid), None)
        if idx is None:
            return jsonify({'error': '排队消息不存在或已投递'}), 404
        mq.pop(idx)
        remaining = len(mq)
        store.repo.save()
    store.repo.append_event(task, 'status',
                 text='排队消息已撤回（原第 %d 位）' % (idx + 1))
    return jsonify({'ok': True, 'count': remaining})


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
    payload, code = closeTaskCore(task)
    return jsonify(payload), code


def closeTaskCore(task):
    """结束会话（/close 与工作线轮换共用）。返回 (payload, status)。"""
    taskId = task.get('id') or ''
    forced = False
    with store.repo.lock:
        agent = store.repo.agentOf(taskId)
        task['closedByUser'] = True
    if agent:
        runner._denyPendingPermission(agent)
        agent['queue'].put({'type': 'close'})
        client = agent.get('client')
        # 三类客户端（KimiAcp/RemoteAcpClient/FederationTaskClient）都有
        # cancel：不主动掐断回合，Kimi 会在权限被拒后继续尝试别的做法，
        # 任务就长时间停在 running（R4 验收：871e615e3416）
        if client is not None and hasattr(client, 'cancel'):
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
        # 兜底（2026-11-13 实测）：cancel 只是请求停这一轮，若代理正卡在一个
        # 工具调用里（例如跑全量测试），会话不会回结果，托管线程也就永远等不到
        # 终态——任务停在 running，名额一直不释放，close 还回 ok 骗调用方。
        # 这里升级为硬停：先 close() 终止客户端进程（KimiAcp 会 terminate→kill），
        # 仍不落地就强制置 CANCELLED，并在响应里标明 forced，别再说"关掉了"。
        with store.repo.lock:
            stillRunning = (task.get('status') or '') in ('running', 'queued')
        if stillRunning:
            forced = True
            if client is not None and hasattr(client, 'close'):
                try:
                    client.close()
                except Exception:
                    pass
            hard = time.monotonic() + 5
            while time.monotonic() < hard:
                with store.repo.lock:
                    if (task.get('status') or '') not in ('running', 'queued'):
                        break
                time.sleep(0.2)
            with store.repo.lock:
                if (task.get('status') or '') in ('running', 'queued'):
                    task_state.transition(task, task_state.TaskState.CANCELLED,
                                          'close 强制终止（回合未返回结果）')
                    task['endedAt'] = store._now()
                    store.repo.append_event(task, 'status', text='已强制终止')
            if task.get('sessionId'):
                from modules.agent_nodes import shutdownNodeSession
                shutdownNodeSession(task.get('node') or 'local', task['sessionId'])
    else:
        task_state.transition(task, task_state.TaskState.CLOSED)
        task['endedAt'] = store._now()
        store.repo.append_event(task, 'status', text='会话已结束')
        # web 重启后 agent 已不在内存——close 仍须释放 node 侧会话锁，
        # 终止持锁的 kimi 进程（R4-5 余项 A：不能"既不能续、又不释放"）
        if task.get('sessionId'):
            from modules.agent_nodes import shutdownNodeSession
            shutdownNodeSession(task.get('node') or 'local', task['sessionId'])
    # close 语义：返回的就是终态（已 cancel 的任务保持 cancelled，不会改判）
    payload = {'ok': True, 'status': task.get('status')}
    if forced:
        payload['forced'] = True
    return payload, 200


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
    waitSec = _parseWaitSeconds(data.get('wait'))
    if waitSec:
        # 审批完直接等下一个决策点/回合结束（R4-3）
        with store.repo.lock:
            afterSeq = task.get('seq', 0)
        return jsonify(_blockWait(task, afterSeq, waitSec,
                                  ignorePendingRequestId=requestId)), 200
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
    name = staging.safeAttachmentName(data.get('name'))
    if not name:
        return jsonify({'error': 'name 必须是纯文件名（无路径分隔符，≤120 字符）'}), 400
    try:
        raw = fsutil.decodeBase64(data.get('contentB64'))
    except ValueError:
        return jsonify({'error': 'contentB64 不是合法 base64（已拒绝写入）'}), 400
    if len(raw) > store.MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({'error': '文件超过 %dMB 上限' % store.MAX_UPLOAD_MB}), 413
    try:
        path = staging.saveStagedStream(name, io.BytesIO(raw),
                                        store.MAX_UPLOAD_MB * 1024 * 1024, store.taskRoot())
    except ValueError as e:
        return jsonify({'error': str(e)}), 413
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        events.log('agent.task.upload', tokenName, 'ok',
                   '%s (%d bytes)' % (name, len(raw)), request.remote_addr,
                   **_agentOnBehalfOf())
    return jsonify({'ok': True, 'path': os.path.abspath(path),
                    'size': len(raw),
                    'hint': '把该 path 放进 POST /api/tasks 的 paths 数组即可作为任务输入'})


@blueprint.route('/api/tasks/kimi-sessions')
def listKimiSessions():
    """已存在的 Kimi CLI 会话（第二轮反馈 §2.3）：智能体挑接手候选。

    ?node=<节点>：代理远端 node 的 /api/kimi-sessions（R4-7），
    未指定或 local 时列本机 kimi 会话。"""
    cwd = (request.args.get('cwd') or '').strip()
    try:
        limit = int(request.args.get('limit') or 30)
    except ValueError:
        limit = 30
    node = (request.args.get('node') or '').strip()
    if node and node != 'local':
        target = agent_nodes.findNode(node)
        if target is None:
            return jsonify({'error': '节点不存在', 'nodes': agent_nodes.listNodeNames()}), 404
        from urllib.parse import quote
        try:
            status, data = agent_nodes.nodeRequest(
                target, 'GET',
                '/api/kimi-sessions?cwd=%s&limit=%d' % (quote(cwd), limit),
                timeout=120)
        except Exception as e:
            # 隧道瞬断等传输错误：502 JSON 让调用方可重试，不再 500
            return jsonify({'error': '节点请求失败: %s' % str(e)[:200],
                            'node': node}), 502
        if status != 200:
            return jsonify({'error': (data or {}).get('error') or 'HTTP %s' % status,
                            'node': node}), 502
        items = data.get('sessions') or []
    else:
        try:
            items = kimi_acp.listSessions(limit=limit, cwd=cwd or None)
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 502
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        events.log('agent.task.kimilist', tokenName, 'ok',
                   'node=%s cwd=%s' % (node or '-', cwd or '-'), request.remote_addr)
    return jsonify({'sessions': items, 'node': node,
                    'hint': 'POST /api/tasks {"kimiSessionId": "<id>", '
                            '"node": "<执行点>", "cwd": "<目录>"} '
                            '接手（默认 fork 副本，不干扰原会话）'})


@blueprint.route('/api/tasks/<taskId>/policy', methods=['POST'])
@taskRoute
def taskPolicyUpdate(task, taskId):
    """更新审批边界（R4-1/R4-2）：只影响下一次调用，挂起中的审批不重判。"""
    data = request.get_json(force=True, silent=True) or {}
    policy = data.get('policy', data)
    try:
        normalized = task_policy.validatePolicy(policy)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    with store.repo.lock:
        if task.get('status') == 'running':
            return jsonify({'error': '这一轮还在执行，回合结束再更新边界'}), 409
        task['policy'] = normalized
        store.repo.save()
    store.repo.append_event(task, 'status', text='审批边界已更新: '
                            + json.dumps(normalized, ensure_ascii=False)[:200])
    with store.repo.lock:
        agent = store.repo.agentOf(taskId)
    client = agent.get('client') if agent else None
    if normalized and hasattr(client, 'set_policy'):
        try:
            client.set_policy(normalized)
        except Exception as e:
            store.repo.append_event(task, 'status',
                         text='边界已更新，但对端同步失败: %s' % str(e)[:160])
    return jsonify({'ok': True, 'policy': normalized})


def register(app):
    store._ensureRoot()
    store.repo.load()
    log.info('agent tasks root=%s', store.taskRoot())
