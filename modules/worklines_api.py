# -*- coding: utf-8 -*-
"""工作线路由（R5-1a）：建线、看接力棒、在工作线上派活、轮换会话。

主控侧不再手工维护 status.md：建线一次，之后在工作线上发消息，服务端
按当前任务状态自动决定"续聊 / 排队 / 新开一个带接力棒的任务"。路由层
只做参数校验与路由决策；状态在 worklines_store，任务编排复用 tasks_api。
"""
from flask import Blueprint, g, jsonify, request

from modules import task_node, task_policy
from modules import tasks_api
from modules import tasks_store
from modules import worklines_store

blueprint = Blueprint('worklines', __name__)

DEFAULT_ROTATE_PROMPT = ('接上接力棒继续推进；若接力棒里已无待办，只回报当前'
                         '状态，不要新增工作。')
DOC_MAX = 64 * 1024


def _json():
    return request.get_json(force=True, silent=True) or {}


def _by():
    return getattr(g, 'agentTokenName', '') or '面板'


def _taskOf(wl):
    tid = (wl or {}).get('currentTask') or ''
    if not tid:
        return None
    tasks_store.repo.load()
    return tasks_store.repo.get(tid)


def _nextAction(wl, task):
    """给主控的一句话结论：还能续、还是该轮换了。"""
    if task is None:
        return ''
    info = worklines_store.contextInfo(task)
    if info and info['ratio'] >= worklines_store.warnRatio(wl):
        return 'rotate'
    return ''


def _brief(wl):
    task = _taskOf(wl)
    info = worklines_store.contextInfo(task) if task else None
    return {'id': wl['id'], 'title': wl.get('title') or '',
            'rev': wl.get('rev') or '',
            'currentTask': (task or {}).get('id') or '',
            'currentStatus': (task or {}).get('status') or '',
            'tasks': len(wl.get('tasks') or []),
            'autoRotate': bool(wl.get('autoRotate')),
            'contextWarn': wl.get('contextWarn'),
            'context': info, 'nextAction': _nextAction(wl, task),
            'repo': wl.get('repo') or '', 'branch': wl.get('branch') or '',
            'updatedAt': wl.get('updatedAt') or 0,
            'updatedBy': wl.get('updatedBy') or ''}


def _detail(wl):
    out = _brief(wl)
    out['doc'] = worklines_store.repo.readDoc(wl)
    out['policy'] = wl.get('policy') or {}
    out['node'] = task_node.worklineNode(wl)[0]
    out['cwd'] = wl.get('cwd') or ''
    out['defaults'] = wl.get('defaults') or {}
    tasks_store.repo.load()
    ids = (wl.get('tasks') or [])[-20:]
    out['taskList'] = [tasks_store.repo.summaryBrief(tasks_store.repo.get(tid))
                       for tid in ids if tasks_store.repo.get(tid)]
    return out


def _notFound(wid):
    return jsonify({'error': '工作线不存在: %s' % wid}), 404


def _parsePolicy(raw):
    try:
        return task_policy.validatePolicy(raw), None
    except ValueError as e:
        return None, ({'error': str(e)}, 400)


def _policyError(err):
    payload, code = err
    return jsonify(payload), code


@blueprint.route('/api/worklines', methods=['GET'])
def listWorklines():
    worklines_store.repo.load()
    tasks_store.repo.load()
    items = sorted(worklines_store.repo.list(), key=lambda w: -w.get('updatedAt', 0))
    return jsonify({'worklines': [_brief(wl) for wl in items],
                    'count': len(items)})


@blueprint.route('/api/worklines', methods=['POST'])
def createWorkline():
    data = _json()
    title = str(data.get('title') or '').strip()[:80]
    if not title:
        return jsonify({'error': '工作线需要 title（一句话说明这条线的目标）'}), 400
    doc = str(data.get('doc') or '')
    if len(doc) > DOC_MAX:
        return jsonify({'error': '接力棒正文过长（上限 %d 字符）' % DOC_MAX}), 400
    policy, err = _parsePolicy(data.get('policy'))
    if err:
        return _policyError(err)
    if (legacy := task_node.rejectLegacyEngine(data)):
        return jsonify({'error': legacy}), 400
    defaults = data.get('defaults') if isinstance(data.get('defaults'), dict) else {}
    worklines_store.repo.load()
    try:
        wl = worklines_store.repo.create({
            'id': data.get('id'), 'title': title, 'policy': policy,
            'node': data.get('node'), 'cwd': data.get('cwd'),
            'repo': data.get('repo'), 'branch': data.get('branch'),
            'defaults': defaults, 'autoRotate': data.get('autoRotate'),
            'contextWarn': data.get('contextWarn'), 'by': _by()})
    except ValueError as e:
        code = 409 if '已存在' in str(e) else 400
        return jsonify({'error': str(e)}), code
    worklines_store.repo.writeDoc(wl, doc, _by())
    return jsonify({'workline': _detail(wl)}), 201


@blueprint.route('/api/worklines/<wid>', methods=['GET'])
def getWorkline(wid):
    wl = worklines_store.repo.get(wid)
    if not wl:
        return _notFound(wid)
    out = {'workline': _detail(wl)}
    if request.args.get('doc') == '0':
        out['workline'].pop('doc', None)
    return jsonify(out)


@blueprint.route('/api/worklines/<wid>', methods=['PUT'])
def updateWorkline(wid):
    wl = worklines_store.repo.get(wid)
    if not wl:
        return _notFound(wid)
    data = _json()
    if (legacy := task_node.rejectLegacyEngine(data)):
        return jsonify({'error': legacy}), 400
    patch = {'by': _by()}
    for key in ('title', 'node', 'cwd', 'repo', 'branch'):
        if key in data:
            patch[key] = data.get(key)
    if 'policy' in data:
        policy, err = _parsePolicy(data.get('policy'))
        if err:
            return _policyError(err)
        patch['policy'] = policy
    for key in ('defaults', 'autoRotate', 'contextWarn'):
        if key in data:
            patch[key] = data.get(key)
    if 'doc' in data:
        doc = str(data.get('doc') or '')
        if len(doc) > DOC_MAX:
            return jsonify({'error': '接力棒正文过长（上限 %d 字符）' % DOC_MAX}), 400
        patch['doc'] = doc
    try:
        wl = worklines_store.repo.update(wid, patch, str(data.get('baseRev') or ''))
    except worklines_store.RevConflict as e:
        return jsonify({'error': '接力棒已被改动（当前 rev %s），请带上 baseRev 重取'
                                % (str(e) or '')[:8], 'rev': str(e)}), 409
    return jsonify({'workline': _detail(wl)})


@blueprint.route('/api/worklines/<wid>', methods=['DELETE'])
def deleteWorkline(wid):
    wl = worklines_store.repo.get(wid)
    if not wl:
        return _notFound(wid)
    task = _taskOf(wl)
    if task and task.get('status') in ('queued', 'running'):
        return jsonify({'error': '当前任务还在跑，先 close 再删工作线'}), 409
    worklines_store.repo.remove(wid)
    return jsonify({'ok': True})


def _routeOf(task):
    """这条消息该走哪条路：续聊 / 排队 / 新开一个带接力棒的任务。"""
    if task is None:
        return 'new'
    status = task.get('status') or ''
    if status in ('queued', 'running'):
        return 'queued'
    if status in ('waiting', 'paused', 'failed', 'interrupted') \
            and task.get('sessionId'):
        return 'message'
    return 'new'


def _dispatchNew(wl, data, prompt):
    body = {'prompt': prompt, 'workline': wl['id']}
    for key in ('node', 'cwd', 'model', 'mode', 'policy', 'wait'):
        if key in data and data.get(key) not in (None, ''):
            body[key] = data[key]
    payload, code = tasks_api.createTaskResponse(body)
    return payload, code


@blueprint.route('/api/worklines/<wid>/tasks', methods=['POST'])
def worklineTask(wid):
    wl = worklines_store.repo.get(wid)
    if not wl:
        return _notFound(wid)
    data = _json()
    if (legacy := task_node.rejectLegacyEngine(data)):
        return jsonify({'error': legacy}), 400
    prompt = str(data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'prompt 不能为空'}), 400
    task = _taskOf(wl)
    route = _routeOf(task)
    if route == 'queued' and (task.get('status') or '') == 'queued':
        # 刚建还没开跑：直接进持久化队列，runner 起跑后清队
        item, pos = tasks_api._enqueueMessage(task, prompt)
        return jsonify({'routed': 'queued', 'taskId': task['id'],
                        'queued': True, 'position': pos,
                        'messageId': item.get('id') or ''}), 202
    if route in ('queued', 'message'):
        mode = 'steer' if (data.get('mode') or '').lower() == 'steer' else 'queue'
        payload, code = tasks_api.deliverMessage(
            task, prompt, mode,
            tasks_api._parseWaitSeconds(data.get('wait')))
        payload['routed'] = 'queued' if route == 'queued' else 'message'
        payload['taskId'] = task['id']
        return jsonify(payload), code
    payload, code = _dispatchNew(wl, data, prompt)
    if code not in (200, 202):
        return jsonify(payload), code
    payload['routed'] = 'new'
    return jsonify(payload), code


@blueprint.route('/api/worklines/<wid>/rotate', methods=['POST'])
def rotateWorkline(wid):
    """换一个新会话接上接力棒：新任务带棒，旧任务 close（轮末用）。"""
    wl = worklines_store.repo.get(wid)
    if not wl:
        return _notFound(wid)
    data = _json()
    if (legacy := task_node.rejectLegacyEngine(data)):
        return jsonify({'error': legacy}), 400
    oldId = wl.get('currentTask') or ''
    prompt = str(data.get('prompt') or '').strip() or DEFAULT_ROTATE_PROMPT
    payload, code = _dispatchNew(wl, data, prompt)
    if code not in (200, 202):
        return jsonify(payload), code
    newId = (payload.get('task') or {}).get('id') or ''
    closedId = ''
    if oldId and oldId != newId:
        old = tasks_store.repo.get(oldId)
        if old and old.get('status') not in ('closed', 'cancelled'):
            _closePayload, _code = tasks_api.closeTaskCore(old)
            closedId = oldId
    worklines_store.repo.setCurrent(wid, newId)
    payload['routed'] = 'rotate'
    payload['closedTaskId'] = closedId
    payload['docRev'] = wl.get('rev') or ''
    return jsonify(payload), code


def register(app):
    worklines_store.repo.load()