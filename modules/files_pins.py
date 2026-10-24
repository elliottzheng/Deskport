# -*- coding: utf-8 -*-
"""快速访问固定与图片缩略图。"""
import json
import os

from flask import jsonify, request, send_file

import config
from modules import media
from modules.files_base import blueprint

def _pinsFile():
    return config.statePath('files_pins.json')


def _loadPins():
    home = os.path.expanduser('~')

    def sub(name):
        p = os.path.join(home, name)
        return p if os.path.isdir(p) else None
    try:
        with open(_pinsFile(), encoding='utf-8') as f:
            pins = json.load(f)
        if isinstance(pins, list):
            return [p for p in pins if isinstance(p, dict) and p.get('path')]
    except (OSError, ValueError):
        pass
    defaults = [{'path': home, 'label': '主目录'}]
    for name, label in (('Desktop', '桌面'), ('Downloads', '下载'), ('Documents', '文档')):
        p = sub(name)
        if p:
            defaults.append({'path': p, 'label': label})
    _savePins(defaults)
    return defaults


def _savePins(pins):
    with open(_pinsFile(), 'w', encoding='utf-8') as f:
        json.dump(pins, f, ensure_ascii=False, indent=1)


@blueprint.route('/api/files/pins', methods=['GET', 'POST'])
def pinsRoute():
    if request.method == 'GET':
        return jsonify({'pins': _loadPins()})
    pins = request.json or {}
    paths = pins.get('pins')
    if not isinstance(paths, list):
        return jsonify({'error': 'pins 应为列表'}), 400
    clean = [{'path': p.get('path'), 'label': p.get('label') or os.path.basename(p.get('path') or '') or p.get('path')}
             for p in paths if isinstance(p, dict) and p.get('path')]
    _savePins(clean)
    return jsonify({'ok': True, 'pins': clean})


# ---- 图片缩略图 (FM-13): 共享 media 服务 ----
@blueprint.route('/api/files/thumb')
def thumb():
    path = request.args.get('path') or ''
    try:
        cached = media.thumbPath(path)
    except media.MediaError as e:
        return jsonify({'error': str(e)}), e.status
    return send_file(cached, mimetype='image/png')


# ---- 复制/移动粘贴 (FM-14): Explorer 剪贴板语义 ----