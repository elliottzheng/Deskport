# -*- coding: utf-8 -*-
"""设备登录 — App 用网页密码一次性换只带 upload 权限的设备令牌。

端点（挂 agent_api 蓝图, app.py import 即随 agentApiModule 一次注册）:
    POST /api/agent/device/login  {password, device}
      → {token, name, scopes:["upload"], createdAt}
      密码与网页登录同一来源(config.getPassword)同一比对方式; 令牌只在
      这次响应出现, 任何接口都不再回显。同设备名重登=换新令牌, 旧令牌即废。
    POST /api/agent/device/logout   凭设备令牌自吊销（智能体令牌 403）
    管理面（设置页, 会话态, 路由在 modules/settings.py）:
      GET /api/settings/devices; POST /api/settings/devices/revoke {id}

存储: config.statePath('devices.json') —— %APPDATA%\\<stateDirName>\\ 状态
目录。不进 config.json（签发/吊销/每次使用都要写，不能频繁重写主配置），
也不放项目目录（本机清理进程删过项目目录文件，SSH 状态因此迁出，见
config.py 头注）；stateDirName 自带 dev/prod 隔离。条目:
    {id, name, tokenHash(SHA-256), kind:'device', scopes:['upload'],
     createdAt, lastUsedAt, lastIp}
令牌校验经 agent_api.agentFromRequest 回退进来：设备令牌与智能体令牌走
同一个鉴权入口/scope 白名单——upload 放行，exec 与 /api/tasks 照常 403。

防暴力破解（接口暴露公网；内存计数，重启清零，与网页登录同档）:
    - 每 IP: 60 秒内失败≥5 → 拒到失败滚出窗口（与 auth.py 同款）；
    - 全局: 10 分钟内失败≥20 → 全局锁 10 分钟，触发写审计 device.lockout；
    - 锁定期间统一 429「尝试次数过多」；网页登录计数独立，不受影响。
审计: device.login ok/fail/locked + device.lockout + device.logout；
设置页吊销的审计 device.revoke 在 settings.py。
"""
import datetime
import hmac
import os
import secrets
import threading
import time

from flask import jsonify, request

import config
from modules import events, state
from modules.agent_api import SCOPE_UPLOAD, agentFromRequest, blueprint, tokenHash

DEVICES_FILE = 'devices.json'
MAX_DEVICES = 20
MAX_NAME = 24
BAD_NAME_CHARS = '<>"\'\r\n\t'

IP_MAX_FAILURES = 5
IP_WINDOW_SECONDS = 60
GLOBAL_MAX_FAILURES = 20
GLOBAL_WINDOW_SECONDS = 600
GLOBAL_LOCK_SECONDS = 600

_cache = {'mtime': None, 'data': None}
_cacheLock = threading.Lock()

_ipFailures = {}
_globalFailures = []
_globalLockedUntil = 0.0
_rateLock = threading.Lock()


# ---------------------------------------------------------------- 存储

def devicesPath():
    return config.statePath(DEVICES_FILE)


def _loadDevices():
    """mtime 缓存读；缺失/损坏回退空表（同 config.load 哲学）。"""
    path = devicesPath()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    with _cacheLock:
        if mtime is not None and _cache['mtime'] == mtime and _cache['data'] is not None:
            return list(_cache['data'])
    raw = state.readJson(path, [])
    devices = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get('tokenHash') or '') \
                    and item.get('kind') == 'device':
                devices.append(item)
    with _cacheLock:
        _cache['mtime'] = mtime
        _cache['data'] = devices
    return list(devices)


def _saveDevices(devices):
    state.atomicWriteJson(devicesPath(), devices)
    with _cacheLock:
        _cache['mtime'] = None
        _cache['data'] = None


def _issueDevice(devices, name, token, ip):
    """同设备名重登 = 替换旧条目（旧令牌即刻失效）。返回 createdAt。"""
    nowText = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    kept = [d for d in devices if d.get('name') != name]
    kept.append({'id': secrets.token_hex(8), 'name': name,
                 'tokenHash': tokenHash(token), 'kind': 'device',
                 'scopes': [SCOPE_UPLOAD], 'createdAt': nowText,
                 'lastUsedAt': '', 'lastIp': ip})
    _saveDevices(kept)
    return nowText


def _cleanDeviceName(raw):
    text = str(raw or '').strip()
    if not text or len(text) > MAX_NAME or any(c in text for c in BAD_NAME_CHARS):
        return None
    return text


def publicDevices():
    """设置页列表：不含 tokenHash——令牌签发后永不回显。"""
    return [{'id': d.get('id'), 'name': d.get('name'),
             'scopes': list(d.get('scopes') or []),
             'createdAt': d.get('createdAt'), 'lastUsedAt': d.get('lastUsedAt'),
             'lastIp': d.get('lastIp')} for d in _loadDevices()]


def revokeById(rid):
    """设置页按 id 吊销。返回设备名；不存在返回 None。"""
    rid = str(rid or '').strip()
    devices = _loadDevices()
    hit = next((d for d in devices if str(d.get('id') or '') == rid), None)
    if hit is None:
        return None
    _saveDevices([d for d in devices if str(d.get('id') or '') != rid])
    return str(hit.get('name') or '')


def revokeByHash(digest):
    """设备自吊销：按 tokenHash 删除。删到返回 True，没有返回 False。"""
    devices = _loadDevices()
    kept = [d for d in devices
            if not hmac.compare_digest(str(d.get('tokenHash') or ''), digest)]
    if len(kept) == len(devices):
        return False
    _saveDevices(kept)
    return True


# ---------------------------------------------------------------- 鉴权回退

def agentFromDevices(digest):
    """agent_api.agentFromRequest 的回退查询：devices.json 按 tokenHash
    找设备条目，命中视同智能体令牌条目（kind='device', scopes=['upload']）。"""
    for device in _loadDevices():
        stored = str(device.get('tokenHash') or '')
        if stored and hmac.compare_digest(stored, digest):
            _touch(device)
            return dict(device)
    return None


def _touch(device):
    """回写最后使用时间/IP：展示串精确到秒，同一秒内不重复写盘；写失败
    不影响本次请求（并发下丢一次更新可接受，下一秒会再写）。"""
    nowText = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if device.get('lastUsedAt') == nowText:
        return
    try:
        devices = _loadDevices()
        for item in devices:
            if hmac.compare_digest(str(item.get('tokenHash') or ''),
                                   str(device.get('tokenHash') or '')):
                item['lastUsedAt'] = nowText
                item['lastIp'] = str(request.remote_addr or '')[:64]
                _saveDevices(devices)
                break
    except OSError:
        pass


# ---------------------------------------------------------------- 防爆破

def _tooMany():
    return jsonify({'error': '尝试次数过多, 请稍后再试'}), 429


def _rateLimited(ip):
    """锁定中返回 429 应答；可尝试返回 None。"""
    now = time.time()
    with _rateLock:
        if now < _globalLockedUntil:
            return _tooMany()
        recent = [t for t in _ipFailures.get(ip, []) if now - t < IP_WINDOW_SECONDS]
        _ipFailures[ip] = recent
        if len(recent) >= IP_MAX_FAILURES:
            return _tooMany()
        _globalFailures[:] = [t for t in _globalFailures
                              if now - t < GLOBAL_WINDOW_SECONDS]
    return None


def _recordFailure(ip):
    """记一次失败。返回 True = 本次触发了全局锁定（由调用方写审计）。"""
    global _globalLockedUntil
    now = time.time()
    with _rateLock:
        _ipFailures.setdefault(ip, []).append(now)
        _globalFailures.append(now)
        _globalFailures[:] = [t for t in _globalFailures
                              if now - t < GLOBAL_WINDOW_SECONDS]
        if len(_globalFailures) >= GLOBAL_MAX_FAILURES:
            _globalLockedUntil = now + GLOBAL_LOCK_SECONDS
            return True
    return False


# ---------------------------------------------------------------- 端点

@blueprint.route('/api/agent/device/login', methods=['POST'])
def deviceLogin():
    ip = request.remote_addr or 'unknown'
    limited = _rateLimited(ip)
    if limited is not None:
        events.log('device.login', '', 'locked', 'rate limited', ip)
        return limited
    data = request.get_json(force=True, silent=True) or {}
    name = _cleanDeviceName(data.get('device'))
    if name is None:
        return jsonify({'error': 'device 需为 1-%d 个字符, 不能含 < > " \' '
                                 '或换行' % MAX_NAME}), 400
    devices = _loadDevices()
    fresh = not any(d.get('name') == name for d in devices)
    if fresh and len(devices) >= MAX_DEVICES:
        return jsonify({'error': '设备数量已达上限 %d, 请先在设置页吊销不用的'
                                 '设备' % MAX_DEVICES}), 400
    password = str(data.get('password') or '')
    expect = config.getPassword()
    if expect and hmac.compare_digest(expect.encode(), password.encode()):
        token = secrets.token_urlsafe(32)
        createdAt = _issueDevice(devices, name, token, ip)
        events.log('device.login', name, 'ok', '', ip)
        return jsonify({'token': token, 'name': name,
                        'scopes': [SCOPE_UPLOAD], 'createdAt': createdAt})
    if _recordFailure(ip):
        events.log('device.lockout', 'global', 'on',
                   '10 分钟内全局失败≥%d' % GLOBAL_MAX_FAILURES, ip)
    events.log('device.login', name, 'fail', 'bad password', ip)
    return jsonify({'error': '密码错误'}), 401


@blueprint.route('/api/agent/device/logout', methods=['POST'])
def deviceLogout():
    agent = agentFromRequest()
    if agent is None:
        return jsonify({'error': 'unauthorized (需要 X-Agent-Token)'}), 401
    ip = request.remote_addr or ''
    name = str(agent.get('name') or '')
    if agent.get('kind') != 'device':
        events.log('device.logout', name, 'deny', '仅设备令牌可自吊销', ip)
        return jsonify({'error': '仅设备令牌可自吊销'}), 403
    if not revokeByHash(str(agent.get('tokenHash') or '')):
        return jsonify({'error': '设备不存在'}), 404
    events.log('device.logout', name, 'ok', '', ip)
    return jsonify({'ok': True})


def _resetForTests():
    """清空限速计数与设备缓存（仅测试用；每个用例需要独立状态）。"""
    global _globalLockedUntil
    with _rateLock:
        _ipFailures.clear()
        del _globalFailures[:]
        _globalLockedUntil = 0.0
    with _cacheLock:
        _cache['mtime'] = None
        _cache['data'] = None
