# -*- coding: utf-8 -*-
"""插件发现与清单解析：根目录、plugin.json 校验、entry 路径安全。

从 plugins 拆出（2026-11-05）。只做"有哪些插件、清单合不合法"，
不 import 插件代码、不注册路由。
"""
import json
import os

import config
from modules.plugins_base import ID_RE, RESERVED_IDS, _log

API_VERSION = 1


def getRoots():
    """插件根目录：仓库内置 plugins/ + 用户目录（状态目录下 plugins/）。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [os.path.join(base, 'plugins')]
    try:
        roots.append(os.path.join(config.stateDir(), 'plugins'))
    except OSError:
        pass
    return [r for r in roots if os.path.isdir(r)]


def defaultNavDir():
    return os.path.join(config.stateDir(), 'plugins')


def readManifest(pluginDir):
    """读并校验 plugin.json; 非法抛 ValueError (由调用方记为错误并跳过)。"""
    path = os.path.join(pluginDir, 'plugin.json')
    with open(path, encoding='utf-8-sig') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('清单不是 JSON 对象')
    pluginId = str(data.get('id') or '').strip()
    if not ID_RE.match(pluginId):
        raise ValueError('id 非法: %r' % pluginId)
    if pluginId in RESERVED_IDS:
        raise ValueError('id 与内置路由冲突: %s' % pluginId)
    entry = str(data.get('entry') or '').strip()
    if not entry.endswith('.py'):
        raise ValueError('entry 必须是 .py 文件')
    data['id'] = pluginId
    data['entry'] = entry
    return data


def manifestApi(manifest):
    try:
        api = int(manifest.get('api') or 0)
    except (TypeError, ValueError):
        raise ValueError('api 必须是整数')
    if api < 0 or api > API_VERSION:
        raise ValueError('不支持的 api 版本: %s' % api)
    return api


def discover(roots):
    """按目录顺序收集，同 id 后者覆盖；返回 (found, errors)。

    found: {id: (manifest, pluginDir, entryPath, api)}
    """
    found = {}
    errors = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            pluginDir = os.path.join(root, name)
            if not os.path.isdir(pluginDir):
                continue
            try:
                manifest = readManifest(pluginDir)
                api = manifestApi(manifest)
            except (OSError, ValueError) as e:
                errors.append({'dir': pluginDir, 'error': str(e)})
                _log.warning('插件跳过 [%s]: %s', name, e)
                continue
            entryPath = os.path.normpath(os.path.join(pluginDir, manifest['entry']))
            if not entryPath.startswith(os.path.normpath(pluginDir) + os.sep):
                errors.append({'dir': pluginDir, 'error': 'entry 越出插件目录'})
                continue
            if not os.path.isfile(entryPath):
                errors.append({'dir': pluginDir, 'error': '入口不存在: %s' % manifest['entry']})
                continue
            found[manifest['id']] = (manifest, pluginDir, entryPath, api)
    return found, errors
