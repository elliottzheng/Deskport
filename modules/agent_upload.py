# -*- coding: utf-8 -*-
"""手机分享上传 — POST /api/agent/upload（scope=upload）。

手机端「分享」直达 Deskport 的落盘通道: multipart 直传，不经 base64、
不经任务区、不需要会话 Cookie。与 agent_sftp 同模式挂在 agent_api 的
蓝图上（app.py 显式 import 即随 agentApiModule 一次注册）。

上传目标（契约 2026-11-12）:
    - 落盘位置 = uploadTargets 白名单之一, form 字段 target 选 id,
      缺省 "phone"（内置手机上传, path=phoneUploadDir）; 不存在 400
      {"error":"上传目标不存在","targets":[{id,name}...]}（不含 path）;
    - GET /api/agent/upload/targets 给 App 拉目标列表（只含 id+name,
      default=phone）; 登录响应同样只带 id+name;
    - 响应 saved[].path = 目标名/相对路径（如 教研资料/11月/a.pdf）,
      服务器绝对路径不出网。

设计约束（评估报告 §1/§5）:
    - 令牌必须持有 upload scope（全权令牌也放行）——手机丢失只暴露
      「往白名单目录写文件」一种能力，不是整套远程执行权；
    - 目标目录自动创建；phone 目标目录自动并入收件箱观察目录
      （config.getInboxDirs）；
    - 重名自动 (2)（复用 fsutil.uniquePath），绝不静默覆盖；
      文件名逐个过 tasks_store._safeAttachmentName；subdir 拒绝绝对
      路径、盘符、.. 与 Windows 非法字符；
    - 流式写盘: 1MB 一块边收边落并顺带算 SHA-256，全程不把整个文件
      读进内存；单文件上限 phoneUploadMaxMB（缺省 2048），超限删除
      半成品并记 413；
    - 全部失败按原因返回 4xx（超限 413 / 校验 400），不一律 500；
    - 审计 agent.upload 记令牌名与 target id；越权尝试由
      _agentRequired 记 agent.scope（need=upload）。
"""
import hashlib
import os
import re

from flask import jsonify, request

import config
from modules import events, fsutil
from modules.agent_api import SCOPE_UPLOAD, _agentRequired, blueprint
from modules.tasks_store import _safeAttachmentName

READ_CHUNK = 1024 * 1024


def _resolveTarget(rawId):
    """form 的 target 字段 → uploadTargets 条目；缺省/空串 = 内置 phone。
    不存在返回 None。"""
    wanted = str(rawId or '').strip() or config.UPLOAD_TARGET_BUILTIN_ID
    for item in config.getUploadTargets():
        if item['id'] == wanted:
            return item
    return None


def _safeSubdir(raw):
    """subdir 只接受相对子路径: 逐级过 _safeAttachmentName，拒绝绝对
    路径、盘符、..、非法字符。合法返回规范化相对路径（/ 分隔）；空 = 根；
    非法返回 None。"""
    text = str(raw or '').strip()
    if not text:
        return ''
    if re.match(r'^[A-Za-z]:', text) or text.startswith(('/', '\\')):
        return None
    parts = []
    for part in re.split(r'[\\/]+', text):
        if part in ('', '.'):
            continue
        clean = _safeAttachmentName(part)
        if part == '..' or not clean:
            return None
        parts.append(clean)
    if not parts or len('/'.join(parts)) > 500:
        return None
    return '/'.join(parts)


def _saveOne(fs, destDir, limitMB):
    """流式落盘单个文件。返回 (saved 条目, None) 或 (None, (错误, status))。
    条目带 name（重名处理后的最终文件名）, path 由调用方拼成
    目标名/相对路径后下发——绝对路径不出网。"""
    name = _safeAttachmentName(fs.filename)
    if not name:
        return None, ('文件名不合法（需纯文件名，≤120 字符）', 400)
    target = fsutil.uniquePath(destDir, name, start=2, sep='')
    digest = hashlib.sha256()
    size = 0
    limitBytes = limitMB * 1024 * 1024
    try:
        with open(target, 'wb') as out:
            while True:
                chunk = fs.stream.read(READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limitBytes:
                    break
                digest.update(chunk)
                out.write(chunk)
    except OSError as e:
        return None, ('写盘失败: %s' % e, 500)
    if size > limitBytes:
        try:
            os.remove(target)   # 删半成品，不留垃圾
        except OSError:
            pass
        return None, ('超过单文件上限 %dMB' % limitMB, 413)
    return {'name': os.path.basename(target), 'size': size,
            'sha256': digest.hexdigest()}, None


@blueprint.route('/api/agent/upload/targets')
def agentUploadTargets():
    """App 拉上传目标列表: 只含 id+name, path 不出网; default=phone。"""
    agent, err = _agentRequired(SCOPE_UPLOAD)
    if agent is None:
        return err
    targets = [{'id': t['id'], 'name': t['name']}
               for t in config.getUploadTargets()]
    return jsonify({'targets': targets,
                    'default': config.UPLOAD_TARGET_BUILTIN_ID})


@blueprint.route('/api/agent/upload', methods=['POST'])
def agentUpload():
    agent, err = _agentRequired(SCOPE_UPLOAD)
    if agent is None:
        return err
    tokenName = str(agent.get('name') or '')

    def audit(result, detail):
        events.log('agent.upload', tokenName, result,
                   'scope=%s %s' % (SCOPE_UPLOAD, detail), request.remote_addr)

    target = _resolveTarget(request.form.get('target'))
    if target is None:
        audit('fail', 'target 不存在')
        return jsonify({'error': '上传目标不存在',
                        'targets': [{'id': t['id'], 'name': t['name']}
                                    for t in config.getUploadTargets()]}), 400
    subdir = _safeSubdir(request.form.get('subdir'))
    if subdir is None:
        audit('fail', 'subdir 不合法 target=%s' % target['id'])
        return jsonify({'error': 'subdir 不合法（需相对路径，不能含 .. / 盘符 /'
                                 '非法字符）'}), 400
    files = request.files.getlist('file')
    if not files:
        audit('fail', '缺少 file target=%s' % target['id'])
        return jsonify({'error': '缺少 file 字段'}), 400

    root = target['path']
    if not root:
        audit('fail', '目标目录缺失 target=%s' % target['id'])
        return jsonify({'error': '上传目标目录未配置（phoneUploadDir /'
                                 'fallbackDir 均缺失）'}), 500
    destDir = os.path.join(root, *subdir.split('/')) if subdir else root
    try:
        os.makedirs(destDir, exist_ok=True)
    except OSError as e:
        audit('fail', '建目录失败 target=%s %s' % (target['id'], e))
        return jsonify({'error': '创建目录失败: %s' % e}), 500

    limitMB = config.getPhoneUploadMaxMB()
    saved, failed, statuses = [], [], []
    for item in files:
        entry, failure = _saveOne(item, destDir, limitMB)
        if failure:
            error, status = failure
            failed.append({'name': item.filename or '', 'error': error})
            statuses.append(status)
        else:
            relParts = ([target['name']] +
                        (subdir.split('/') if subdir else []) +
                        [entry['name']])
            saved.append({'path': '/'.join(relParts), 'size': entry['size'],
                          'sha256': entry['sha256']})

    detail = 'target=%s saved=%d failed=%d' % (target['id'],
                                               len(saved), len(failed))
    if subdir:
        detail += ' subdir=' + subdir
    for f in failed[:3]:
        detail += ' | %s: %s' % (f['name'][:60], f['error'][:60])
    audit('ok' if saved else 'fail', detail)

    status = 200
    if not saved and statuses:
        unique = set(statuses)
        status = unique.pop() if len(unique) == 1 else (
            413 if 413 in unique else sorted(unique)[0])
    return jsonify({'ok': bool(saved), 'saved': saved, 'failed': failed}), status
