# -*- coding: utf-8 -*-
"""应用设置：收件箱扫描开关与目录、多机联邦、登录设备管理。"""
import os
import re
import secrets

from flask import Blueprint, jsonify, request

import config
from modules import device_auth, events, federation

blueprint = Blueprint('settings', __name__)

MAX_DIRS = 20
MAX_BRAND = 24
MAX_REMOTES = 20
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
REMOTE_ID_RE = re.compile(r'^[a-z0-9_-]{1,20}$')


@blueprint.route('/api/settings/brand', methods=['GET'])
def getBrandSettings():
    return jsonify({'name': config.getBrandName()})


@blueprint.route('/api/settings/brand', methods=['POST'])
def setBrandSettings():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name or len(name) > MAX_BRAND:
        return jsonify({'error': '品牌名需为 1-%d 个字符' % MAX_BRAND}), 400
    if any(c in name for c in '<>"\'') or any(c in name for c in '\r\n\t'):
        return jsonify({'error': '品牌名不能包含 < > " \' 或换行'}), 400
    config.setBrandName(name)
    return jsonify({'ok': True, 'name': name})


@blueprint.route('/api/settings/inbox', methods=['GET'])
def getInboxSettings():
    cfg = config.load()
    dirs = [p for p in (cfg.get('inboxDirs') or []) if isinstance(p, str) and p]
    return jsonify({
        'enabled': config.isInboxEnabled(),
        'dirs': dirs,
        'defaults': config.defaultInboxDirs(),
        'home': os.path.expanduser('~'),
    })


@blueprint.route('/api/settings/inbox', methods=['POST'])
def setInboxSettings():
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get('enabled'))
    raw = data.get('dirs') or []
    if not isinstance(raw, list):
        return jsonify({'error': 'dirs 必须是数组'}), 400
    if len(raw) > MAX_DIRS:
        return jsonify({'error': '最多 %d 个目录' % MAX_DIRS}), 400
    dirs = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        path = os.path.abspath(os.path.expanduser(item.strip()))
        if not os.path.isdir(path):
            return jsonify({'error': '目录不存在: %s' % item}), 400
        if path not in dirs:
            dirs.append(path)
    config.setInboxSettings(enabled, dirs)
    return jsonify({'ok': True, 'enabled': enabled, 'dirs': dirs})


@blueprint.route('/api/settings/federation', methods=['GET'])
def federationSettings():
    return jsonify({
        'token': config.getFederationToken(),
        'remotes': config.getRemotes(),
        'machines': federation.machineOptions(),
        # 文件页的成员选项: 只含开了 files 策略的远端 (不含 token)
        'fileMachines': federation.fileMachines(),
        # 各远端当前广播、且本机策略允许的外部应用 (设置页提示白名单可填什么)
        'discoveredApps': [
            {'remoteId': a['remoteId'], 'appId': a['appId'],
             'label': a.get('label') or '', 'prefix': a.get('prefix') or ''}
            for a in federation.discoveredExternalApps()
        ],
    })


@blueprint.route('/api/settings/federation/token', methods=['POST'])
def setFederationToken():
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get('token') or '').strip()
    if token and not TOKEN_RE.match(token):
        return jsonify({'error': '令牌需为 8-64 位字母、数字、- 或 _'}), 400
    config.setFederationToken(token)
    return jsonify({'ok': True, 'token': token})


@blueprint.route('/api/settings/federation/remotes', methods=['POST'])
def setRemotes():
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get('remotes')
    if not isinstance(raw, list):
        return jsonify({'error': 'remotes 必须是数组'}), 400
    if len(raw) > MAX_REMOTES:
        return jsonify({'error': '最多 %d 台远端' % MAX_REMOTES}), 400
    cleaned, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        rid = str(item.get('id') or '').strip().lower()
        name = str(item.get('name') or '').strip()
        base = str(item.get('base') or '').strip().rstrip('/')
        token = str(item.get('token') or '').strip()
        if not REMOTE_ID_RE.match(rid) or rid in seen:
            return jsonify({'error': 'id 需为 1-20 位小写字母/数字/-/_ 且不重复: %r' % rid}), 400
        if not name or len(name) > 24:
            return jsonify({'error': '名称需为 1-24 个字符'}), 400
        if not base.startswith(('http://', 'https://')):
            return jsonify({'error': '地址需以 http:// 或 https:// 开头: %s' % base}), 400
        if token and len(token) > 128:
            return jsonify({'error': '令牌过长'}), 400
        # 消费策略 (见 config.getRemotes): terminal 缺省 true; apps 白名单缺省 [];
        # files 缺省 false (浏览成员文件比接终端更敏感, 显式开启)
        terminal = item.get('terminal', True)
        if not isinstance(terminal, bool):
            return jsonify({'error': 'terminal 需为布尔'}), 400
        files = item.get('files', False)
        if not isinstance(files, bool):
            return jsonify({'error': 'files 需为布尔'}), 400
        filesWrite = item.get('filesWrite', False)
        if not isinstance(filesWrite, bool):
            return jsonify({'error': 'filesWrite 需为布尔'}), 400
        apps = item.get('apps', [])
        if not isinstance(apps, list) or len(apps) > 20 or \
                any(not isinstance(a, str) or not a.strip() or len(a) > 40 for a in apps):
            return jsonify({'error': 'apps 需为 ≤20 个非空短字符串的白名单'}), 400
        cleaned.append({'id': rid, 'name': name, 'base': base, 'token': token,
                        'terminal': terminal, 'files': files,
                        'filesWrite': filesWrite,
                        'apps': [a.strip() for a in apps]})
        seen.add(rid)
    config.setRemotes(cleaned)
    return jsonify({'ok': True, 'remotes': cleaned,
                    'machines': federation.machineOptions()})


# ---------------------------------------------------------------- 登录设备
# 设备令牌(App 密码换发, 仅 upload)的签发/自吊销在 modules/device_auth.py;
# 这里是设置页的管理面: 列表(无哈希, 令牌签发后永不回显)与按 id 吊销。

@blueprint.route('/api/settings/devices', methods=['GET'])
def listDevices():
    return jsonify({'devices': device_auth.publicDevices()})


@blueprint.route('/api/settings/devices/revoke', methods=['POST'])
def revokeDevice():
    data = request.get_json(force=True, silent=True) or {}
    name = device_auth.revokeById(data.get('id'))
    if name is None:
        return jsonify({'error': '设备不存在'}), 404
    events.log('device.revoke', name, 'ok', '', request.remote_addr)
    return jsonify({'ok': True, 'devices': device_auth.publicDevices()})


# ---------------------------------------------------------------- 上传目标
# 上传目标白名单: 内置 phone(phoneUploadDir) + config uploadTargets。
# 管理面全量替换自定义目标; id 由服务端生成/保留(编辑不改 id, App 已保存
# 的 targets 不失效); path 只留在服务端, 下发 App 的接口只给 id+name。

UPLOAD_TARGET_ID_RE = re.compile(r'^[a-z0-9_-]{1,20}$')
BAD_NAME_CHARS = '<>"\'\r\n\t'
MAX_UPLOAD_TARGETS = 20   # 含内置 phone


@blueprint.route('/api/settings/uploadTargets', methods=['GET'])
def uploadTargetsSettings():
    return jsonify({'targets': config.getUploadTargets(),
                    'builtinId': config.UPLOAD_TARGET_BUILTIN_ID})


@blueprint.route('/api/settings/uploadTargets', methods=['POST'])
def setUploadTargetsSettings():
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get('targets')
    if not isinstance(raw, list):
        return jsonify({'error': 'targets 必须是数组'}), 400
    if len(raw) > MAX_UPLOAD_TARGETS - 1:
        return jsonify({'error': '最多 %d 个自定义目标'
                                 % (MAX_UPLOAD_TARGETS - 1)}), 400
    cleaned, seen, names = [], {'phone'}, set()
    for item in raw:
        if not isinstance(item, dict):
            return jsonify({'error': 'targets 条目必须是对象'}), 400
        tid = str(item.get('id') or '').strip()
        name = str(item.get('name') or '').strip()
        path = os.path.abspath(
            os.path.expanduser(str(item.get('path') or '').strip()))
        if tid == config.UPLOAD_TARGET_BUILTIN_ID:
            continue   # 内置目标由服务端提供, 载荷里忽略
        if tid and (not UPLOAD_TARGET_ID_RE.match(tid) or tid in seen):
            return jsonify({'error': 'id 需为 1-20 位小写字母/数字/-/_ '
                                     '且不重复: %r' % tid}), 400
        if not name or len(name) > 24 or any(c in name for c in BAD_NAME_CHARS):
            return jsonify({'error': '名称需为 1-24 个字符, 不能含 < > " \' '
                                     '或换行'}), 400
        if name in names:
            return jsonify({'error': '目标名称重复: %s' % name}), 400
        if not path or len(path) > 500:
            return jsonify({'error': '目录需为 1-500 个字符的绝对路径'}), 400
        if not os.path.isdir(path):
            return jsonify({'error': '目录不存在: %s' % path}), 400
        if not tid:
            tid = secrets.token_hex(4)
        seen.add(tid)
        names.add(name)
        cleaned.append({'id': tid, 'name': name, 'path': path})
    config.setUploadTargets(cleaned)
    events.log('settings.uploadTargets', '', 'ok',
               'targets=%d' % len(cleaned), request.remote_addr)
    return jsonify({'ok': True, 'targets': config.getUploadTargets(),
                    'builtinId': config.UPLOAD_TARGET_BUILTIN_ID})
