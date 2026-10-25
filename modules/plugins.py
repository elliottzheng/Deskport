# -*- coding: utf-8 -*-
"""插件系统 — 原生插件 + RemoteConnecter legacy 兼容。

原生清单 (plugin.json):
    {
      "id": "process", "name": "进程管理", "version": "1.0.0",
      "api": 1, "entry": "process_bp.py",
      "nav": [{"label": "进程", "icon": "grid", "path": ""}],
      "openWith": ["docx", "xlsx"]
    }

legacy 清单 (lhz/RemoteConnecter，原样放入即可):
    {"id": "process", "entry": "process_bp.py", "lib": "lib"}

约定:
    - 入口模块里所有以 _bp 结尾的 Blueprint（原生也接受 blueprint）会被注册；
    - 模块级 sock (flask_sock.Sock) 在全部注册完成后统一 init_app；
    - legacy 保持根路径（/process、/code），原生挂 /p/<id>；
    - 单个插件导入/注册失败只记日志，不影响其他插件，也不影响宿主 WebSocket。
"""
import importlib.util
import json
import logging
import os
import re
import sys
import threading

from flask import Blueprint, jsonify
from flask_sock import Sock

import config
from modules import state

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
TITLE_SUFFIX_RE = re.compile(r'\s*[-–—|]\s*RemoteConnecter\s*$', re.I)
TITLE_BLOCKLIST = {'document', 'untitled', 'index', 'home', 'new page', ''}
DOC_PREFIX_RE = re.compile(r'^[^—–-]{1,24}(插件)?\s*[—–-]\s*')

_log = logging.getLogger('plugins')
_lock = threading.RLock()
_registry = []
_lastErrors = []

blueprint = Blueprint('plugins', __name__)


# ---------------------------------------------------------------- 目录与清单

def getRoots():
    """插件根目录：仓库内置 plugins/ + 用户目录（状态目录下 plugins/）。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [os.path.join(base, 'plugins')]
    try:
        roots.append(os.path.join(config.stateDir(), 'plugins'))
    except OSError:
        pass
    return [r for r in roots if os.path.isdir(r)]


def _defaultNavDir():
    return os.path.join(config.stateDir(), 'plugins')


def _readManifest(pluginDir):
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


def _manifestApi(manifest):
    try:
        api = int(manifest.get('api') or 0)
    except (TypeError, ValueError):
        raise ValueError('api 必须是整数')
    if api < 0 or api > API_VERSION:
        raise ValueError('不支持的 api 版本: %s' % api)
    return api


def _discover(roots):
    """按目录顺序收集，同 id 后者覆盖；返回 (found, errors)。"""
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
                manifest = _readManifest(pluginDir)
                api = _manifestApi(manifest)
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


# ---------------------------------------------------------------- 加载与注册

def _importModule(pluginId, entryPath):
    moduleName = 'deskport_plugin_' + pluginId
    spec = importlib.util.spec_from_file_location(moduleName, entryPath)
    if spec is None or spec.loader is None:
        raise ImportError('无法加载入口: %s' % entryPath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[moduleName] = module
    spec.loader.exec_module(module)
    return module


def _collectBlueprints(module):
    found = []
    for attrName, attrValue in vars(module).items():
        if not isinstance(attrValue, Blueprint):
            continue
        if attrName.endswith('_bp') or attrName in ('bp', 'blueprint'):
            found.append(attrValue)
    return found


def _collectSock(module):
    sock = getattr(module, 'sock', None)
    return sock if isinstance(sock, Sock) else None


def _register(app, bp, legacy, pluginId):
    if legacy:
        app.register_blueprint(bp)
    else:
        app.register_blueprint(bp, url_prefix='/p/' + pluginId)


def _autoRoute(app, bpNames, pluginId):
    best = ''
    for rule in app.url_map.iter_rules():
        if not any(rule.endpoint.startswith(name + '.') for name in bpNames):
            continue
        if 'GET' not in rule.methods:
            continue
        if '<' in rule.rule or '/static' in rule.rule:
            continue
        if rule.rule.startswith('/%s/api' % pluginId):
            continue
        if not best or (len(rule.rule), rule.rule) < (len(best), best):
            best = rule.rule
    return best


def _templateTitle(pluginDir):
    templateDir = os.path.join(pluginDir, 'templates')
    try:
        names = sorted(os.listdir(templateDir))[:20]
    except OSError:
        return ''
    for name in names:
        if not name.endswith('.html'):
            continue
        try:
            with open(os.path.join(templateDir, name), encoding='utf-8',
                      errors='replace') as f:
                text = f.read(8192)
        except OSError:
            continue
        match = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
        if not match:
            continue
        title = re.sub(r'\s+', ' ', match.group(1)).strip()
        if '{{' in title or '{%' in title:
            continue
        title = TITLE_SUFFIX_RE.sub('', title).strip()
        if title.lower() in TITLE_BLOCKLIST or len(title) > 30:
            continue
        return title
    return ''


def _docLabel(module):
    doc = (module.__doc__ or '').strip().splitlines()
    if not doc:
        return ''
    line = doc[0].strip().split('。')[0]
    line = DOC_PREFIX_RE.sub('', line).strip()
    return line if 0 < len(line) <= 30 else ''


def _legacyLabel(pluginDir, module, pluginId, manifest):
    name = str(manifest.get('name') or '').strip()
    if name:
        return name[:24]
    return (_templateTitle(pluginDir) or _docLabel(module)
            or pluginId)


def _nativeNav(pluginId, manifest):
    raw = manifest.get('nav')
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw[:6]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get('label') or '').strip()[:24]
        if not label:
            continue
        path = str(entry.get('path') or '')
        if path and not path.startswith('/'):
            path = '/' + path
        if '..' in path:
            continue
        href = '/p/' + pluginId + (path if path not in ('', '/') else '')
        items.append({'id': pluginId,
                      'label': label,
                      'icon': str(entry.get('icon') or DEFAULT_ICON),
                      'href': href, 'external': False})
    return items


def _openWith(manifest):
    raw = manifest.get('openWith')
    if not isinstance(raw, list):
        return []
    exts = []
    for item in raw[:40]:
        ext = str(item or '').strip().lower().lstrip('.')
        if ext and re.match(r'^[a-z0-9]{1,12}$', ext) and ext not in exts:
            exts.append(ext)
    return exts


def _loadOne(app, pluginId, manifest, pluginDir, entryPath, api):
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
        module = _importModule(pluginId, entryPath)
    except Exception as e:  # 插件代码是外部输入，任何异常都只影响自己
        _log.exception('插件导入失败 [%s]', pluginId)
        record['error'] = '导入失败: %s' % e
        return record, None

    try:
        blueprints = _collectBlueprints(module)
        if not blueprints:
            raise ValueError('入口模块里没有 *_bp 蓝图')
        for bp in blueprints:
            _register(app, bp, legacy, pluginId)
    except Exception as e:
        _log.exception('插件注册失败 [%s]', pluginId)
        record['error'] = '注册失败: %s' % e
        return record, module

    record['ok'] = True
    if legacy:
        bps = [bp.name for bp in blueprints]
        route = _autoRoute(app, bps, pluginId)
        if route:
            record['nav'] = [{
                'id': pluginId,
                'label': _legacyLabel(pluginDir, module, pluginId, manifest),
                'icon': ICON_HINTS.get(pluginId, DEFAULT_ICON),
                'href': route,
                'external': True,
            }]
    else:
        record['nav'] = _nativeNav(pluginId, manifest)
        record['openWith'] = _openWith(manifest)
    return record, module


# ---------------------------------------------------------------- 导航与登记

def _applyOverrides(records, navDir):
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
    global _registry, _lastErrors
    records = []
    modules = []
    found, errors = _discover(roots if roots is not None else getRoots())
    with _lock:
        for pluginId, (manifest, pluginDir, entryPath, api) in found.items():
            record, module = _loadOne(app, pluginId, manifest, pluginDir, entryPath, api)
            records.append(record)
            modules.append((record, module))
        _applyOverrides(records, navDir if navDir is not None else _defaultNavDir())
        _registry = records
        _lastErrors = errors
        _initSocks(app, modules)
        for record in records:
            if record['ok']:
                _log.info('插件已加载 [%s] legacy=%s api=%s', record['id'],
                          record['legacy'], record['api'])
            else:
                _log.warning('插件不可用 [%s]: %s', record['id'], record['error'])
    return {'plugins': records, 'errors': errors}


def _initSocks(app, modules):
    """插件 Sock 统一 init_app；多个 Sock() 的私有蓝图同名，先改名避免撞车。"""
    for record, module in modules:
        if module is None:
            continue
        sock = _collectSock(module)
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
