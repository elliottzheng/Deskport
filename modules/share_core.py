# -*- coding: utf-8 -*-
"""分享交付内核 — 三个去处、字段规则、"接到任务"目标校验。

2026-11-13 批次 D 从 modules/agent_share.py 抽出：网页 `/m/share` 的
Cookie 入口与设备令牌入口必须共用同一份逻辑（不许复制第二份）。

调用方:
    modules/agent_share.py   设备令牌入口 /api/agent/share（先落暂存区）
    modules/share_web.py     Cookie 入口 /api/share/submit（文件已由 ticket 暂存）

本模块不注册路由，也不认任何鉴权方式；它只回答"表单怎么校验、文件往哪交付"。
"""
import datetime
import os
import re
import shutil

from flask import request

import config
from modules import events, staging, task_inputs, task_node, tasks_api, tasks_store

MAX_FILES = 10
MAX_PROMPT = 4000
MAX_FILE_MB = 20
ORGANIZE_MODES = ('none', 'folder')
MODES = ('default', 'auto')

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


def _withFolderNote(prompt, name):
    """规格 §8：folder 模式下 prompt 末尾自动补一行目录说明。"""
    note = FOLDER_NOTE % name
    return (prompt + '\n' + note) if prompt else note


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


def _deliverRemote(task, paths):
    """接到远端任务时把本批附件投到该任务执行点；返回 (远端路径, 200) 或 (错误, code)。"""
    try:
        info = task_inputs.deliver(task_node.of(task), task_node.cwdOf(task),
                                   task['id'], paths)
    except task_inputs.DeliverError as e:
        return {'error': str(e)}, e.status
    return info['paths'], 200


# ---------------------------------------------------------------- 表单校验

def parseShareOptions(data, fileCount):
    """校验并归一化分享表单（multipart form 与 JSON dict 通用）。

    /api/agent/share 与 /api/share/submit 共用这一份字段规则，入口只差
    鉴权方式；返回 (opts, error)，error 为 (payload, code)。
    """
    prompt = _text(data.get('prompt'))
    if len(prompt) > MAX_PROMPT:
        return None, ({'error': 'prompt 过长（上限 %d 字符）' % MAX_PROMPT}, 400)
    if not fileCount and not prompt:
        return None, ({'error': 'prompt 与 file 至少给一个'}, 400)

    mode = _text(data.get('mode')).lower() or 'default'
    if mode not in MODES:
        return None, ({'error': 'mode 只能是 default / auto'}, 400)

    node = task_node.normalize(data.get('node'))
    cwd = _text(data.get('cwd'))
    if len(cwd) > 500:
        return None, ({'error': 'cwd 过长'}, 400)

    model = _text(data.get('model'))
    if len(model) > 100:
        return None, ({'error': 'model 不合法（≤100 字符）'}, 400)

    organize = _text(data.get('organize')).lower() or 'none'
    if organize not in ORGANIZE_MODES:
        return None, ({'error': 'organize 只能是 none / folder'}, 400)
    folderName = ''
    if organize == 'folder':
        if not fileCount:
            return None, ({'error': '新建文件夹需要至少一个文件'}, 400)
        rawName = _text(data.get('folderName'))
        if rawName:
            folderName = _safeFolderName(rawName)
            if folderName is None:
                return None, ({'error': '文件夹名不合法（不能含 / \\ .. 盘符或'
                                        '非法字符，≤120 字符）'}, 400)
        else:
            folderName = _defaultFolderName()

    return {'prompt': prompt, 'mode': mode, 'node': node, 'cwd': cwd,
            'model': model, 'taskId': _text(data.get('taskId')),
            'organize': organize, 'folderName': folderName}, None


def findOpenTask(taskId):
    """接到任务的目标校验：不存在或已 closed → (None, (payload, 409))。"""
    repo = tasks_store.repo
    repo.load()
    task = repo.get(taskId)
    if task is None or (task.get('status') or '') == 'closed':
        return None, ({'error': '任务不存在或已结束'}, 409)
    return task, None


# ---------------------------------------------------------------- 交付内核

def deliverShare(opts, files, folder='', folderName='', task=None):
    """把已落暂存区的附件交付到两个 AI 去处（新建任务 / 接到任务）。

    files = 暂存区绝对路径；folder 非空（organize=folder）时 paths 只给目录，
    folderName 是建目录后的实际名（重名 (2) 之后）。task 非空 = 接到该任务。
    失败时清掉本次暂存。返回 (payload, code)。审计在此统一写。
    """
    paths = [folder] if folder else list(files)
    detail = 'files=%d promptLen=%d' % (len(files), len(opts['prompt']))
    if folder:
        detail += ' organize=folder'
    if task is not None:
        return _appendShare(task, opts, files, folder, paths, detail)
    return _createShare(opts, files, folder, paths, folderName, detail)


def _appendShare(task, opts, files, folder, paths, detail):
    taskId = task['id']
    if not task_node.isLocal(task_node.of(task)) and paths:
        payload, code = _deliverRemote(task, paths)
        if code >= 400:
            _discard(files, folder)
            _audit('agent.share', taskId, 'fail', 'appended ' + detail)
            return payload, code
        paths = payload
    basePrompt = opts['prompt']
    if folder:
        basePrompt = _withFolderNote(basePrompt, os.path.basename(paths[0]))
        if len(basePrompt) > MAX_PROMPT:
            _discard(files, folder)
            _audit('agent.share', taskId, 'fail', 'prompt 加目录说明超长')
            return {'error': 'prompt 过长（加上文件夹说明超过 %d 字符）'
                             % MAX_PROMPT}, 400
    text = _messageText(basePrompt, paths)
    payload, code = tasks_api.deliverMessage(task, text, 'queue')
    if code >= 400:
        _discard(files, folder)
        _audit('agent.share', taskId, 'fail', 'appended ' + detail)
        return payload, code
    if not task_node.isLocal(task_node.of(task)):
        _discard(files, folder)   # 本机暂存只是投递中转
    _audit('agent.share', taskId, 'ok', 'appended ' + detail)
    return {'taskId': taskId, 'mode': 'appended',
            'paths': paths, 'prompt': text}, 200


def _createShare(opts, files, folder, paths, folderName, detail):
    prompt = opts['prompt']
    finalPrompt = prompt or (FILES_ONLY_PROMPT % len(files))
    if folder:
        finalPrompt = _withFolderNote(finalPrompt, folderName)
    if len(finalPrompt) > MAX_PROMPT:
        _discard(files, folder)
        _audit('agent.share', '', 'fail', 'prompt 加目录说明超长')
        return {'error': 'prompt 过长（加上文件夹说明超过 %d 字符）'
                         % MAX_PROMPT}, 400
    data = {'prompt': finalPrompt, 'paths': paths, 'mode': opts['mode'],
            'node': opts['node']}
    if opts['cwd']:
        data['cwd'] = opts['cwd']
    if opts['model']:
        data['model'] = opts['model']
    payload, code = tasks_api.createTaskResponse(data)
    if code >= 300:
        _discard(files, folder)
        _audit('agent.share', '', 'fail', 'created ' + detail)
        return payload, code
    newId = (payload.get('task') or {}).get('id') or ''
    finalPrompt = (payload.get('task') or {}).get('prompt') or finalPrompt
    if not task_node.isLocal(opts['node']):
        _discard(files, folder)       # 本机暂存只是投递中转
        paths = payload.get('paths') or paths
    _audit('agent.share', newId, 'ok', 'created ' + detail)
    return {'taskId': newId, 'mode': 'created',
            'paths': paths, 'prompt': finalPrompt}, 200


# ---------------------------------------------------------------- 列表

def nodesPayload():
    """执行点列表：本机 + agent node + 联邦成员（附件由服务端投递过去）。"""
    from modules import agent_nodes, node_path
    models, defaultModel = agent_nodes.nodeModelsFor('local')
    nodes = [{'name': 'local', 'label': '本机', 'status': 'online',
              'models': list(models or []), 'defaultModel': defaultModel or '',
              'os': node_path.nodeOs('local')}]
    for item in list(config.getAgentNodes()) + list(config.getRemotes()):
        models, defaultModel = agent_nodes.nodeModelsFor(item['name'], item)
        nodes.append({'name': item['name'], 'label': item['name'],
                      'status': 'unknown', 'models': list(models or []),
                      'defaultModel': defaultModel or '',
                      'os': node_path.nodeOs(item['name'])})
    return {'nodes': nodes, 'default': 'local'}


def tasksPayload():
    """「接到任务」下拉：最近 20 条非 closed 任务，按最后活动倒序。"""
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
    return {'tasks': rows[:20]}