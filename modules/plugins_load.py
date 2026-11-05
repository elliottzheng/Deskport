# -*- coding: utf-8 -*-
"""插件加载与注册：动态 import、蓝图收集、url_prefix、legacy 自动路由。

从 plugins 拆出（2026-11-05）。插件代码是外部输入，任何异常只记进 record，
不影响其他插件，也不影响宿主。
"""
import importlib.util
import os
import re
import sys

from flask import Blueprint
from flask_sock import Sock

from modules.plugins_base import DEFAULT_ICON, ICON_HINTS, _log
from modules.plugins_discover import defaultNavDir, discover, getRoots
from modules import plugins_nav
from modules import state


def importModule(pluginId, entryPath):
    moduleName = 'deskport_plugin_' + pluginId
    spec = importlib.util.spec_from_file_location(moduleName, entryPath)
    if spec is None or spec.loader is None:
        raise ImportError('无法加载入口: %s' % entryPath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[moduleName] = module
    spec.loader.exec_module(module)
    return module


def collectBlueprints(module):
    found = []
    for attrName, attrValue in vars(module).items():
        if not isinstance(attrValue, Blueprint):
            continue
        if attrName.endswith('_bp') or attrName in ('bp', 'blueprint'):
            found.append(attrValue)
    return found


def collectSock(module):
    sock = getattr(module, 'sock', None)
    return sock if isinstance(sock, Sock) else None


def registerBlueprint(app, bp, legacy, pluginId):
    if legacy:
        app.register_blueprint(bp)
    else:
        app.register_blueprint(bp, url_prefix='/p/' + pluginId)


def autoRoute(app, bpNames, pluginId):
    """legacy 插件的页面路由：限定插件命名空间内最短的静态 GET 路由。

    插件常在根路径注册杂项（如 office 的 /sw.js 兜底），不限定命名空间
    会把它们误认成页面入口。
    """
    prefix = '/%s' % pluginId
    best = ''
    for rule in app.url_map.iter_rules():
        if not any(rule.endpoint.startswith(name + '.') for name in bpNames):
            continue
        if 'GET' not in rule.methods:
            continue
        if '<' in rule.rule or '/static' in rule.rule:
            continue
        if rule.rule != prefix and not rule.rule.startswith(prefix + '/'):
            continue
        if '/api' in rule.rule:
            continue
        if re.search(r'\.[a-z0-9]{1,12}$', rule.rule):
            continue
        if not best or (len(rule.rule), rule.rule) < (len(best), best):
            best = rule.rule
    return best


def loadOne(app, pluginId, manifest, pluginDir, entryPath, api):
    legacy = api == 0
    record = {
        'id': pluginId,
        'name': str(manifest.get('name') or pluginId)[:24],
        'version': str(manifest.get('version') or '0.0.0')[:24],
        'api': api,
        'legacy': legacy,
        'ok': False,
        'error': '',
        'nav': [],
        'openWith': [],
        'order': 1000,
    }

    lib = manifest.get('lib')
    if lib:
        libPath = os.path.normpath(os.path.join(pluginDir, str(lib)))
        if os.path.isdir(libPath) and libPath not in sys.path:
            sys.path.insert(0, libPath)

    try:
        module = importModule(pluginId, entryPath)
    except Exception as e:  # 插件代码是外部输入，任何异常都只影响自己
        _log.exception('插件导入失败 [%s]', pluginId)
        record['error'] = '导入失败: %s' % e
        return record, None

    try:
        blueprints = collectBlueprints(module)
        if not blueprints:
            raise ValueError('入口模块里没有 *_bp 蓝图')
        for bp in blueprints:
            registerBlueprint(app, bp, legacy, pluginId)
    except Exception as e:
        _log.exception('插件注册失败 [%s]', pluginId)
        record['error'] = '注册失败: %s' % e
        return record, module

    record['ok'] = True
    if legacy:
        bps = [bp.name for bp in blueprints]
        route = autoRoute(app, bps, pluginId)
        if route:
            record['nav'] = [{
                'id': pluginId,
                'label': plugins_nav.legacyLabel(pluginDir, module, pluginId, manifest),
                'icon': ICON_HINTS.get(pluginId, DEFAULT_ICON),
                'href': route,
                'external': True,
            }]
    else:
        record['nav'] = plugins_nav.nativeNav(pluginId, manifest)
        record['openWith'] = plugins_nav.openWith(manifest)
    return record, module


def initSocks(app, modules):
    """插件 Sock 统一 init_app；多个 Sock() 的私有蓝图同名，先改名避免撞车。"""
    for record, module in modules:
        if module is None:
            continue
        sock = collectSock(module)
        if sock is None:
            continue
        bp = getattr(sock, 'bp', None)
        if bp is not None:
            bp.name = 'deskport_sock_%s' % record['id']
        try:
            sock.init_app(app)
        except Exception as e:
            _log.exception('插件 WebSocket 初始化失败 [%s]', record['id'])
            record['ok'] = False
            record['error'] = (record['error'] + '; ' if record['error'] else '') + \
                'WebSocket 初始化失败: %s' % e


def applyOverrides(records, navDir):
    overridePath = os.path.join(navDir, 'nav.override.json')
    autoPath = os.path.join(navDir, 'nav.auto.json')
    overrides = state.readJson(overridePath, {})
    if not isinstance(overrides, dict):
        overrides = {}
    for record in records:
        item = overrides.get(record['id'])
        if not isinstance(item, dict):
            continue
        if item.get('hidden'):
            record['nav'] = []
            continue
        if isinstance(item.get('order'), int):
            record['order'] = item['order']
        if record['nav']:
            if item.get('label'):
                record['nav'][0]['label'] = str(item['label'])[:24]
            if item.get('icon'):
                record['nav'][0]['icon'] = str(item['icon'])
    records.sort(key=lambda r: (r['order'], r['id']))
    if navDir:
        try:
            os.makedirs(navDir, exist_ok=True)
            auto = {r['id']: r['nav'] for r in records if r['nav']}
            if state.readJson(autoPath, None) != auto:
                state.atomicWriteJson(autoPath, auto)
        except OSError:
            pass


def load(app, roots=None, navDir=None):
    """扫描并加载插件；返回加载记录（同时写入进程内登记表）。"""
    from modules import plugins_base
    records = []
    modules = []
    found, errors = discover(roots if roots is not None else getRoots())
    for pluginId, (manifest, pluginDir, entryPath, api) in found.items():
        record, module = loadOne(app, pluginId, manifest, pluginDir, entryPath, api)
        records.append(record)
        modules.append((record, module))
    applyOverrides(records, navDir if navDir is not None else defaultNavDir())
    initSocks(app, modules)
    plugins_base.setRegistry(records, errors)
    for record in records:
        if record['ok']:
            _log.info('插件已加载 [%s] legacy=%s api=%s', record['id'],
                      record['legacy'], record['api'])
        else:
            _log.warning('插件不可用 [%s]: %s', record['id'], record['error'])
    return {'plugins': records, 'errors': errors}
