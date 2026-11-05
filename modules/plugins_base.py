# -*- coding: utf-8 -*-
"""插件系统基座：登记表、错误收集、蓝图与版本常量。

从 plugins 拆出（2026-11-05）：原 439 行混了目录扫描/清单解析、动态导入与
注册、导航与标签推导、登记查询四个变化理由。本文件只放被另三个模块共用的
状态与常量，不注册路由、不碰文件系统。
"""
import logging
import re
import threading

from flask import Blueprint, jsonify

API_VERSION = 1
DEFAULT_ICON = 'plug'
ID_RE = re.compile(r'^[a-z][a-z0-9_-]{1,31}$')
RESERVED_IDS = {
    'api', 'app', 'assets', 'command', 'files', 'inbox', 'login', 'logout',
    'manifest.webmanifest', 'p', 'plugins', 'screen', 'settings', 'ssh',
    'sw.js', 'tasks', 'terminal',
}

ICON_HINTS = {'process': 'grid', 'code': 'fileText', 'office': 'fileText',
              'proxy': 'plug', 'sample': 'plug'}

_log = logging.getLogger('plugins')
_lock = threading.RLock()
_registry = []
_lastErrors = []

blueprint = Blueprint('plugins', __name__)


def setRegistry(records, errors):
    """整体替换登记表与发现错误（plugins_load 加载完成后调用）。"""
    global _registry, _lastErrors
    with _lock:
        _registry = list(records or [])
        _lastErrors = list(errors or [])


def getRegistry():
    with _lock:
        return [dict(record) for record in _registry]


def getNavItems():
    items = []
    with _lock:
        for record in _registry:
            if record['ok']:
                items.extend(dict(item) for item in record['nav'])
    return items


def isPluginApiPath(path):
    """插件 API 的 401 语义：/<id>/api/...（legacy）或 /p/<id>/api/...（原生）。"""
    with _lock:
        for record in _registry:
            pid = record['id']
            prefix = '/%s/api/' % pid if record['legacy'] else '/p/%s/api/' % pid
            if path.startswith(prefix):
                return True
    return False


@blueprint.route('/api/plugins')
def apiPlugins():
    with _lock:
        errors = [dict(item) for item in _lastErrors]
    return jsonify({'api': API_VERSION, 'plugins': getRegistry(), 'errors': errors})
