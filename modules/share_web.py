# -*- coding: utf-8 -*-
"""网页分享表单 — `/m/share` 的暂存票据与 Cookie 会话提交（批次 D/E）。

端点:
    POST /api/agent/share/stage     (scope=share, multipart) 原生交文件换 ticket
        file 0..10, prompt 可选
        → {"ticket": "<32位>", "files": [{name,size}], "expiresAt": <ts>}
    GET  /api/share/stage/<ticket>  (Cookie) 读这批文件元信息（不消费）
    GET  /api/share/nodes           (Cookie) 执行点列表（页面用）
    GET  /api/share/tasks           (Cookie) 可接任务列表（页面用）
    GET  /api/share/targets         (Cookie) 只上传的「机器 + 目标」列表
    POST /api/share/submit          (Cookie) 三选一提交
        {ticket, way:"upload"|"new"|"append", prompt, node?, target?,
         taskId?, model?, mode?, organize?, folderName?, subdir?}

ticket 语义（规格：内存、TTL 300s、一次性、只绑定这批文件）:
    - 不是凭证：泄漏最坏结果是别人多建一个任务（还要先有 Cookie 会话）；
    - 过期后读/提交 → 400，暂存目录当场删除；
    - 提交成功后票据销号，再用 → 400。此时文件已是任务输入（本机建任务
      有工作区副本；接到任务是消息里的路径），删掉会破坏任务，所以复用
      不再删文件——与"过期即删"是两种情形，见 docs/m-share.md。

提交逻辑复用 modules/share_core.py（三个去处、organize=folder 语义逐字不变），
只是入口从 X-Agent-Token 换成 Cookie 会话；原生侧仍用设备令牌调 stage。

批次 E：`way=upload` 多了机器轴。`node=local` 仍是原来的本机落盘（目标可
选，缺省 phone）；`node=远端` 交给 share_targets 送到成员机，落点由成员
自己的 uploadTargets 解析。远端上传按**签发这张票据的令牌**判断
`upload_remote`：设备令牌没勾选就 403，Cookie 本人也绕不过去。
"""
import os
import secrets
import shutil
import threading
import time
import uuid

from flask import jsonify, request

import config
from modules import (agent_upload, events, fsutil, share_core, share_targets,
                     staging, task_node, tasks_store)
from modules.agent_api import (SCOPE_SHARE, SCOPE_UPLOAD_REMOTE, agentHasScope,
                               _agentRequired, blueprint)

TICKET_TTL = 300
WAYS = ('upload', 'new', 'append')

_tickets = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- 票据

def _batchDir():
    path = os.path.join(tasks_store.taskRoot(), '_uploads',
                        'stage-' + uuid.uuid4().hex[:12])
    os.makedirs(path)
    return path


def _removeDir(path):
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _info(rec):
    return {'files': [{'name': f['name'], 'size': f['size']}
                      for f in rec['files']],
            'prompt': rec['prompt'], 'expiresAt': int(rec['expiresAt'])}


def _purgeExpired():
    """顺手清掉没人再读的过期票据，避免暂存区长期占盘。"""
    now = time.time()
    with _lock:
        dead = [rec for rec in _tickets.values() if rec['expiresAt'] <= now]
        for rec in dead:
            _tickets.pop(rec['ticket'], None)
    for rec in dead:
        _removeDir(rec['batch'])


def _peek(ticket):
    """读票据（不消费）；过期时删暂存并返回 400。"""
    dead = None
    with _lock:
        rec = _tickets.get(ticket)
        if rec is None:
            return None, ({'error': 'ticket 无效或已使用，请重新分享'}, 400)
        if rec['expiresAt'] > time.time():
            return rec, None
        dead = _tickets.pop(ticket)
    _removeDir(dead['batch'])
    return None, ({'error': '分享已过期，请重新分享'}, 400)


def _claim(ticket):
    """提交时占用票据：一次性；过期当场删暂存。"""
    dead = None
    with _lock:
        rec = _tickets.get(ticket)
        if rec is None:
            return None, ({'error': 'ticket 无效或已使用，请重新分享'}, 400)
        if rec['expiresAt'] <= time.time():
            dead = _tickets.pop(ticket)
        elif rec.get('busy'):
            return None, ({'error': '这个分享正在提交，请稍候'}, 409)
        else:
            rec['busy'] = True
            return rec, None
    _removeDir(dead['batch'])
    return None, ({'error': '分享已过期，请重新分享'}, 400)


def _release(rec):
    with _lock:
        rec['busy'] = False


def _consume(rec):
    with _lock:
        _tickets.pop(rec['ticket'], None)


# ---------------------------------------------------------------- 暂存入口

@blueprint.route('/api/agent/share/stage', methods=['POST'])
def shareStage():
    """原生 App 交文件换 ticket（设备令牌，scope=share）。"""
    agent, err = _agentRequired(SCOPE_SHARE)
    if agent is None:
        return err
    _purgeExpired()

    prompt = share_core._text(request.form.get('prompt'))
    if len(prompt) > share_core.MAX_PROMPT:
        return jsonify({'error': 'prompt 过长（上限 %d 字符）'
                                 % share_core.MAX_PROMPT}), 400
    uploads = [f for f in request.files.getlist('file')
               if f is not None and (f.filename or '')]
    if len(uploads) > share_core.MAX_FILES:
        return jsonify({'error': '一次最多 %d 个文件'
                                 % share_core.MAX_FILES}), 400
    if not uploads and not prompt:
        return jsonify({'error': 'prompt 与 file 至少给一个'}), 400

    try:
        batch = _batchDir()
    except OSError as e:
        return jsonify({'error': '创建暂存目录失败: %s' % e}), 500
    files = []
    for item in uploads:
        name = staging._safeAttachmentName(item.filename)
        if not name:
            _removeDir(batch)
            return jsonify({'error': '文件名不合法（需纯文件名，≤120 字符）: %s'
                                     % (item.filename or '')}), 400
        try:
            path = staging.saveStagedStream(
                name, item.stream, share_core.MAX_FILE_MB * 1024 * 1024,
                tasks_store.taskRoot(), folder=batch)
        except ValueError:
            _removeDir(batch)
            return jsonify({'error': '单文件超过 %dMB 上限: %s'
                                     % (share_core.MAX_FILE_MB, name)}), 413
        except OSError as e:
            _removeDir(batch)
            return jsonify({'error': '写盘失败: %s' % e}), 500
        files.append({'name': os.path.basename(path), 'path': path,
                      'size': os.path.getsize(path)})

    ticket = secrets.token_hex(16)
    expiresAt = time.time() + TICKET_TTL
    rec = {'ticket': ticket, 'batch': batch, 'files': files, 'prompt': prompt,
           'expiresAt': expiresAt, 'busy': False,
           'remote': agentHasScope(agent, SCOPE_UPLOAD_REMOTE)}
    with _lock:
        _tickets[ticket] = rec
    events.log('share.stage', ticket[:8], 'ok',
               'files=%d promptLen=%d' % (len(files), len(prompt)),
               request.remote_addr)
    return jsonify({'ticket': ticket,
                    'files': [{'name': f['name'], 'size': f['size']}
                              for f in files],
                    'expiresAt': int(expiresAt)})


@blueprint.route('/api/share/stage/<ticket>')
def shareStageInfo(ticket):
    rec, err = _peek(ticket)
    if rec is None:
        payload, code = err
        return jsonify(payload), code
    return jsonify(_info(rec))


@blueprint.route('/api/share/nodes')
def shareNodesWeb():
    return jsonify(share_core.nodesPayload())


@blueprint.route('/api/share/tasks')
def shareTasksWeb():
    return jsonify(share_core.tasksPayload())


@blueprint.route('/api/share/targets')
def shareTargetsWeb():
    """只上传的机器+目标：本机来自 config，远端由那台机器自己声明。"""
    return jsonify(share_targets.targetsPayload())


# ---------------------------------------------------------------- 提交

@blueprint.route('/api/share/submit', methods=['POST'])
def shareWebSubmit():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '需要 JSON 请求体'}), 400
    way = str(data.get('way') or '').strip().lower()
    if way not in WAYS:
        return jsonify({'error': 'way 只能是 upload / new / append'}), 400

    rec, err = _claim(str(data.get('ticket') or '').strip())
    if rec is None:
        payload, code = err
        return jsonify(payload), code
    try:
        if way == 'upload':
            payload, code = _upload(rec, data)
        else:
            payload, code = _delegate(rec, data, way)
    except Exception:
        _release(rec)
        raise
    if code >= 400:
        # 交付失败可能已按"失败不留垃圾"删掉暂存文件：文件没了就销号，
        # 让页面提示重新分享；还在（校验失败 / folder 副本失败）则释放可重试。
        if all(os.path.exists(item['path']) for item in rec['files']):
            _release(rec)
        else:
            _consume(rec)
            _removeDir(rec['batch'])
        return jsonify(payload), code
    _consume(rec)
    detail = 'way=%s' % way
    if way == 'upload':
        detail += ' node=%s target=%s' % (payload.get('node') or 'local',
                                          payload.get('target') or '')
    events.log('share.submit', (payload.get('taskId') or '')[:16], 'ok',
               detail, request.remote_addr)
    return jsonify(payload), code


def _upload(rec, data):
    """只上传：本机落盘或送到远端机器；subdir 复用 /api/agent/upload 校验。"""
    if not rec['files']:
        return {'error': '只上传需要至少一个文件'}, 400
    subdir = agent_upload._safeSubdir(data.get('subdir'))
    if subdir is None:
        return {'error': 'subdir 不合法（需相对路径，不能含 .. / 盘符 /'
                         '非法字符）'}, 400
    node = task_node.normalize(data.get('node'))
    if task_node.isLocal(node):
        return _uploadLocal(rec, data, subdir)
    return _uploadRemote(rec, node, data, subdir)


def _uploadLocal(rec, data, subdir):
    """本机：搬进 uploadTargets（缺省 phone）的 subdir，行为与批次 D 一致。"""
    target = agent_upload._resolveTarget(data.get('target'))
    if target is None:
        return {'error': '上传目标不存在',
                'targets': agent_upload.publicTargets()}, 400
    if not target.get('path'):
        return {'error': '上传目标目录未配置（phoneUploadDir / fallbackDir'
                         ' 均缺失）'}, 500
    root = target['path']
    destDir = os.path.join(root, *subdir.split('/')) if subdir else root
    try:
        os.makedirs(destDir, exist_ok=True)
    except OSError as e:
        return {'error': '创建目录失败: %s' % e}, 500

    uploaded, failed = [], []
    for item in rec['files']:
        dest = fsutil.uniquePath(destDir, item['name'], start=2, sep='')
        try:
            shutil.move(item['path'], dest)
        except OSError as e:
            failed.append({'name': item['name'], 'error': str(e)})
            continue
        relParts = ([target['name']] + (subdir.split('/') if subdir else []) +
                    [os.path.basename(dest)])
        uploaded.append({'path': '/'.join(relParts), 'size': item['size']})
    if not failed:
        _removeDir(rec['batch'])
    body = {'ok': not failed, 'uploaded': uploaded, 'node': 'local',
            'target': target['id']}
    if failed:
        body['failed'] = failed
    return body, 200 if uploaded else 500


def _uploadRemote(rec, node, data, subdir):
    """远端：成员机自己把 target id 解析成目录；中枢只看得到相对路径。

    `upload_remote` 按**签发票据的令牌**判断（stage 时记下），因此没勾选
    远端上传的设备令牌即使主人在网页上提交也发不出去。
    """
    if not rec.get('remote'):
        return {'error': '向远端机器上传需要 upload_remote 权限',
                'need': SCOPE_UPLOAD_REMOTE}, 403
    target = share_core._text(data.get('target')) \
        or config.UPLOAD_TARGET_BUILTIN_ID
    try:
        rows, online = share_targets.remoteTargets(node)
        if not online:
            return {'error': '机器 %s 暂时连不上，文件没有上传' % node}, 502
        if target not in [row['id'] for row in rows]:
            return {'error': '成员机 %s 上没有这个上传目标（%s）'
                             % (node, target)}, 400
        body = share_targets.sendFiles(node, target, subdir,
                                       [item['path'] for item in rec['files']])
    except share_targets.TargetError as e:
        return {'error': str(e)}, e.status

    uploaded = [{'path': share_targets._scrub(item.get('path')),
                 'size': int(item.get('size') or 0)}
                for item in body.get('saved') or [] if isinstance(item, dict)]
    failed = [{'name': str(item.get('name') or ''),
               'error': share_targets._scrub(item.get('error'))}
              for item in body.get('failed') or [] if isinstance(item, dict)]
    if uploaded:
        _removeDir(rec['batch'])
    out = {'ok': not failed and bool(uploaded), 'uploaded': uploaded,
           'node': node, 'target': target}
    if failed:
        out['failed'] = failed
    return out, 200 if uploaded else 502


def _delegate(rec, data, way):
    """交给 AI：新建任务或接到已有任务（字段规则与交付全部复用 share_core）。"""
    source = {k: data.get(k) for k in ('prompt', 'mode', 'node', 'cwd', 'model',
                                       'taskId', 'organize', 'folderName')}
    if not share_core._text(source['prompt']):
        source['prompt'] = rec['prompt']    # 页面没改就用 stage 时带的那句话
    files = [item['path'] for item in rec['files']]
    opts, err = share_core.parseShareOptions(source, len(files))
    if opts is None:
        return err
    if way == 'append' and not opts['taskId']:
        return {'error': '接到任务需要 taskId'}, 400
    if way == 'new':
        opts['taskId'] = ''

    task = None
    if opts['taskId']:
        task, err = share_core.findOpenTask(opts['taskId'])
        if task is None:
            return err

    # organize=folder：复制进命名文件夹再交付。失败时 deliverShare 只清掉
    # 这份副本，批次原件还在，票据可以重试。
    folder = ''
    folderName = ''
    delivered = files
    if opts['organize'] == 'folder':
        try:
            folder = staging.createStagedFolder(opts['folderName'],
                                                tasks_store.taskRoot())
        except ValueError as e:
            return {'error': str(e)}, 400
        except OSError as e:
            return {'error': '建文件夹失败: %s' % e}, 500
        try:
            for path in files:
                shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
        except OSError as e:
            _removeDir(folder)
            return {'error': '复制附件失败: %s' % e}, 500
        folderName = os.path.basename(folder)
        delivered = [os.path.join(folder, os.path.basename(p)) for p in files]

    payload, code = share_core.deliverShare(opts, delivered, folder,
                                            folderName, task)
    if code < 400 and folder:
        _removeDir(rec['batch'])   # 命名文件夹已是正式副本，批次中转可删
    return payload, code