# -*- coding: utf-8 -*-
"""应用设置：收件箱扫描开关与目录。"""
import os

from flask import Blueprint, jsonify, request

import config

blueprint = Blueprint('settings', __name__)

MAX_DIRS = 20
MAX_BRAND = 24


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
