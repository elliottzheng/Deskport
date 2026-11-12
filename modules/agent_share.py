# -*- coding: utf-8 -*-
"""手机转发文件 → 交给执行方 — /api/agent/share（scope=share）。

手机令牌只有 upload 时只能往收件箱丢文件；要建任务就得 full，等于把
exec / SFTP / 改配置的全权交给手机。本模块新增 share scope，只放行
「投文件 + 建任务 / 往已有任务追一条消息」这一件事。

端点（挂在 agent_api 的蓝图上，app.py import 即随 agentApiModule 注册）:
    GET  /api/agent/share/nodes   → {nodes:[{name,label,status,models,defaultModel}], default}
    GET  /api/agent/share/tasks   → {tasks:[{id,title,status,updatedAt,node}]} 最近 20 条非 closed
    POST /api/agent/share         multipart:
        file    可重复 0..10（空 prompt 时至少 1 个）
        prompt  ≤4000 字（空且有文件 → 服务端补一句最小说明）
        taskId  可选；给了就是「接到这个任务」（追加一条消息）
        node    可选；只接受本机（见下）
        model   可选
        mode    可选 default|auto（缺省 default = 逐项审批）
        organize 可选 none（默认）| folder：folder 时文件进新建文件夹，paths 给文件夹
        folderName 可选；缺省 share-<YYYYMMDD-HHmm>；重名自动 (2)
      → 200 {taskId, mode:"created"|"appended", paths:[...], prompt}

行为:
    - 文件先落任务暂存区（modules/staging.saveStagedStream，与 /api/tasks/files
      同一份逻辑），响应/消息里给绝对路径；
    - organize=folder 时先在暂存区建文件夹（folderName 校验同文件名：
      不许 .. / 绝对路径 / 分隔符），paths 给文件夹路径，prompt 末尾补
      「以上附件在 <folder> 目录下」；文件名冲突自动 (2)，绝不覆盖；
    - taskId 给了 → 走 tasks_api.deliverMessage（回合中排队，不另造通道）；
    - 没给 → tasks_api.createTaskResponse 建任务（engine=acp，paths 带上）；
    - 令牌缺 share → 403 {"need":"share"}（full 视为含 share）；
    - 审计 agent.share（target=taskId, detail=文件数+prompt 长度）。

执行点边界（2026-11-13 与主控确认）:
    engine=remote 的任务在远端机器上跑，本机暂存区（_agent_tasks/_uploads/…）
    在那边不存在，tasks_api 也会丢弃 paths——附件等于丢失。因此本通道只
    接受本机执行点（node 缺省或 "local"）；远端投递需要另一条跨机文件通道，
    不在本批范围，显式 400 而不是假装成功。
"""
import datetime
import os
import re
import shutil

from flask import jsonify, request

from modules import events, staging, tasks_api, tasks_store
from modules.agent_api import SCOPE_SHARE, _agentRequired, blueprint

MAX_FILES = 10
MAX_PROMPT = 4000
MAX_FILE_MB = 20
ORGANIZE_MODES = ('none', 'folder')

# 没写处理要求、只发了文件时的最小说明（执行方先看再问，不擅自猜目的）
FILES_ONLY_PROMPT = '（未写处理要求）请先查看随附的 %d 个附件，告诉我你建议怎么处理。'
FOLDER_NOTE = '以上附件在 %s 目录下'


def _audit(action, target, result, detail):
    events.log(action, target, result, detail, request.remote_addr)


def _text(raw):
    return str(raw or '').strip()


def _safeFolderName(raw):
    """文件夹名：与文件名同一套校验（纯名称、无分隔符/盘符/控制字符、
    ≤120），额外拒绝 . 与 ..。空返回 ''，非法返回 None。"""
    text = str(raw or '').strip()
    if not text:
        return ''
    if text in ('.', '..') or re.match(r'^[A-Za-z]:', text):
        return None
    return staging._safeAttachmentName(text)


def _defaultFolderName():
    return 'share-' + datetime.datetime.now().strftime('%Y%m%d-%H%M')


def _folderNote(name):
    return FOLDER_NOTE % name


def _withFolderNote(prompt, name):
    """规格 §8：folder 模式下 prompt 末尾自动补一行目录说明。"""
    note = _folderNote(name)
    return (prompt + '\n' + note) if prompt else note


def _localNodeEntry():
    """本机执行点：models/defaultModel 走与 /api/tasks/nodes 相同的来源。"""
    from modules import agent_nodes
    models, defaultModel = agent_nodes.nodeModelsFor('local')
    return {'name': 'local', 'label': '本机', 'status': 'online',
            'models': list(models or []), 'defaultModel': defaultModel or ''}


def _updatedAt(task):
    return float(task.get('lastActiveAt') or 0) or float(task.get('createdAt') or 0)


def _messageText(prompt, paths):
    """追加消息正文：prompt + 附件路径清单（规格 §2.2）。"""
    if not paths:
        return prompt
    lines = []
    if prompt:
        lines.extend([prompt, ''])
    lines.append('附件：')
    lines.extend('- ' + p for p in paths)
    return '\n'.join(lines)


def _discard(paths, folder=''):
    """撤回本次已落的暂存文件（校验/投递失败时不留垃圾）。

    folder 非空时整棵目录删掉（organize=folder 的批次目录）。
    """
    if folder:
        parent = os.path.dirname(folder)
        shutil.rmtree(folder, ignore_errors=True)
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
        return
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass
    for path in paths:
        parent = os.path.dirname(path)
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass


@blueprint.route('/api/agent/share/nodes')
def shareNodes():
    """执行点列表。只列本机：远端拿不到本机暂存区（见模块头注）。"""
    agent, err = _agentRequired(SCOPE_SHARE)
    if agent is None:
        return err
    return jsonify({'nodes': [_localNodeEntry()], 'default': 'local'})


@blueprint.route('/api/agent/share/tasks')
def shareTasks():
    """「接到任务」下拉：最近 20 条非 closed 任务，按最后活动倒序。"""
    agent, err = _agentRequired(SCOPE_SHARE)
    if agent is None:
        return err
    repo = tasks_store.repo
    repo.load()
    items = repo.list()
    rows = [{'id': t.get('id') or '',
             'title': t.get('title') or '',
             'status': t.get('status') or '',
             'updatedAt': _updatedAt(t),
             'node': t.get('node') or 'local'}
            for t in items if (t.get('status') or '') != 'closed']
    rows.sort(key=lambda r: -r['updatedAt'])
    return jsonify({'tasks': rows[:20]})


@blueprint.route('/api/agent/share', methods=['POST'])
def shareSubmit():
    agent, err = _agentRequired(SCOPE_SHARE)
    if agent is None:
        return err

    prompt = _text(request.form.get('prompt'))
    if len(prompt) > MAX_PROMPT:
        _audit('agent.share', '', 'fail', 'prompt=%d 超长' % len(prompt))
        return jsonify({'error': 'prompt 过长（上限 %d 字符）' % MAX_PROMPT}), 400

    mode = _text(request.form.get('mode')).lower() or 'default'
    if mode not in ('default', 'auto'):
        _audit('agent.share', '', 'fail', 'mode=%s' % mode[:20])
        return jsonify({'error': 'mode 只能是 default / auto'}), 400

    node = _text(request.form.get('node'))
    if node and node != 'local':
        _audit('agent.share', '', 'fail', 'node=%s' % node[:40])
        return jsonify({'error': '暂不支持远端执行点：转发附件只在本机执行点可用',
                        'nodes': ['local']}), 400

    model = _text(request.form.get('model'))
    if len(model) > 100:
        _audit('agent.share', '', 'fail', 'model 过长')
        return jsonify({'error': 'model 不合法（≤100 字符）'}), 400

    taskId = _text(request.form.get('taskId'))
    uploads = [f for f in request.files.getlist('file')
               if f is not None and (f.filename or '')]
    if len(uploads) > MAX_FILES:
        _audit('agent.share', taskId, 'fail', 'files=%d 超过 %d' % (len(uploads), MAX_FILES))
        return jsonify({'error': '一次最多 %d 个文件' % MAX_FILES}), 400
    if not uploads and not prompt:
        _audit('agent.share', taskId, 'fail', 'prompt 与 file 均为空')
        return jsonify({'error': 'prompt 与 file 至少给一个'}), 400

    organize = _text(request.form.get('organize')).lower() or 'none'
    if organize not in ORGANIZE_MODES:
        _audit('agent.share', taskId, 'fail', 'organize=%s' % organize[:20])
        return jsonify({'error': 'organize 只能是 none / folder'}), 400
    folderName = ''
    if organize == 'folder':
        if not uploads:
            _audit('agent.share', taskId, 'fail', 'folder 模式没有文件')
            return jsonify({'error': '新建文件夹需要至少一个文件'}), 400
        rawName = _text(request.form.get('folderName'))
        if rawName:
            folderName = _safeFolderName(rawName)
            if folderName is None:
                _audit('agent.share', taskId, 'fail', '文件夹名不合法')
                return jsonify({'error': '文件夹名不合法（不能含 / \\ .. 盘符或'
                                         '非法字符，≤120 字符）'}), 400
        else:
            folderName = _defaultFolderName()

    # 接到已有任务：先确认目标存在且可投，再落盘（避免无主垃圾文件）
    task = None
    if taskId:
        repo = tasks_store.repo
        repo.load()
        task = repo.get(taskId)
        if task is None or (task.get('status') or '') == 'closed':
            _audit('agent.share', taskId, 'fail', '任务不存在或已结束')
            return jsonify({'error': '任务不存在或已结束'}), 409

    stagedFiles = []
    stagedFolder = ''
    if organize == 'folder':
        try:
            stagedFolder = staging.createStagedFolder(folderName, tasks_store.taskRoot())
        except ValueError as e:
            _audit('agent.share', taskId, 'fail', '文件夹名不合法')
            return jsonify({'error': str(e)}), 400
        except OSError as e:
            _audit('agent.share', taskId, 'fail', '建文件夹失败')
            return jsonify({'error': '建文件夹失败: %s' % e}), 500
        folderName = os.path.basename(stagedFolder)   # 重名时以 (2) 后的实际名为准
    for item in uploads:
        name = staging._safeAttachmentName(item.filename)
        if not name:
            _discard(stagedFiles, stagedFolder)
            _audit('agent.share', taskId, 'fail', '文件名不合法')
            return jsonify({'error': '文件名不合法（需纯文件名，≤120 字符）: %s'
                                     % (item.filename or '')}), 400
        try:
            stagedFiles.append(staging.saveStagedStream(
                name, item.stream, MAX_FILE_MB * 1024 * 1024,
                tasks_store.taskRoot(), folder=stagedFolder or None))
        except ValueError:
            _discard(stagedFiles, stagedFolder)
            _audit('agent.share', taskId, 'fail', '文件超过 %dMB' % MAX_FILE_MB)
            return jsonify({'error': '单文件超过 %dMB 上限: %s'
                                     % (MAX_FILE_MB, name)}), 413
        except OSError as e:
            _discard(stagedFiles, stagedFolder)
            _audit('agent.share', taskId, 'fail', '写盘失败')
            return jsonify({'error': '写盘失败: %s' % e}), 500

    # folder 模式下 paths 给文件夹（规格 §8），否则给逐个文件路径（v3 行为）
    paths = [stagedFolder] if stagedFolder else list(stagedFiles)
    detail = 'files=%d promptLen=%d' % (len(stagedFiles), len(prompt))
    if stagedFolder:
        detail += ' organize=folder'

    if task is not None:
        basePrompt = prompt
        if stagedFolder:
            basePrompt = _withFolderNote(prompt, folderName)
            if len(basePrompt) > MAX_PROMPT:
                _discard(stagedFiles, stagedFolder)
                _audit('agent.share', taskId, 'fail', 'prompt 加目录说明超长')
                return jsonify({'error': 'prompt 过长（加上文件夹说明超过 %d 字符）'
                                         % MAX_PROMPT}), 400
        text = _messageText(basePrompt, paths)
        payload, code = tasks_api.deliverMessage(task, text, 'queue')
        if code >= 400:
            _discard(stagedFiles, stagedFolder)
            _audit('agent.share', taskId, 'fail', 'appended ' + detail)
            return jsonify(payload), code
        _audit('agent.share', taskId, 'ok', 'appended ' + detail)
        return jsonify({'taskId': taskId, 'mode': 'appended',
                        'paths': paths, 'prompt': text}), 200

    finalPrompt = prompt or (FILES_ONLY_PROMPT % len(stagedFiles))
    if stagedFolder:
        finalPrompt = _withFolderNote(finalPrompt, folderName)
    if len(finalPrompt) > MAX_PROMPT:
        _discard(stagedFiles, stagedFolder)
        _audit('agent.share', '', 'fail', 'prompt 加目录说明超长')
        return jsonify({'error': 'prompt 过长（加上文件夹说明超过 %d 字符）'
                                 % MAX_PROMPT}), 400
    data = {'prompt': finalPrompt, 'paths': paths,
            'engine': 'acp', 'mode': mode}
    if model:
        data['model'] = model
    payload, code = tasks_api.createTaskResponse(data)
    if code >= 300:
        _discard(stagedFiles, stagedFolder)
        _audit('agent.share', '', 'fail', 'created ' + detail)
        return jsonify(payload), code
    newId = (payload.get('task') or {}).get('id') or ''
    _audit('agent.share', newId, 'ok', 'created ' + detail)
    return jsonify({'taskId': newId, 'mode': 'created',
                    'paths': paths, 'prompt': finalPrompt}), 200