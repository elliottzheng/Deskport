# -*- coding: utf-8 -*-
"""联邦: 把其他 Deskport 实例的终端会话与外部应用挂载合并进本机。

消费策略全在本机 (config.json 的 remotes[] 条目):
    terminal (缺省 true)  是否接受该机终端: 拉取会话/合并列表/WS 路由/新建会话选项
    apps (缺省 [] 不接)   接受该机哪些外部应用 (id 白名单, ["*"] 全收)
提供方只广播自己的挂载清单 (/api/external-apps, token 鉴权), 不过滤 —— 接不接是消费方的事。
外部应用发现后挂到与远端相同的前缀下, 转发自动带 X-Deskport-Token,
远端自身的登录/安全模型原样保留。

非阻塞设计 (2026-11 修复):
    - 远端列表由后台线程每 5 秒刷新并缓存, 本地会话接口永远不等远端;
    - 远端不可达时返回旧缓存 (条目带 offline 标记), 超时只发生在后台线程;
    - 远端条目标记 remote=True, 客户端恢复时不自动附着, 点侧栏才连
      (否则远端一离线, 自动附着就变成无限重连, 表现为终端页疯狂滚动)。
"""
import logging
import threading

import requests

import config
from modules import external_proxy as externalProxy

_log = logging.getLogger('federation')

REFRESH_SECONDS = 5.0
REQUEST_TIMEOUT = 4.0

_lock = threading.Lock()
_cache = {}        # mid -> {'entries': [...], 'ok': bool}
_extCache = {}     # mid -> {'apps': [...], 'ok': bool}
_owners = {}       # session id -> mid
_started = False
_appRef = None     # create_app 注入的 Flask 实例, 发现挂载用


def bindApp(app):
    """create_app 注入 Flask 实例, 并立即拉起联邦刷新线程。

    线程无条件启动 (无远端时空转, 每 5s 一次空循环, 开销可忽略):
    发现必须先于第一次页面渲染就绪, 否则开机后第一次点击外部应用会 404。
    """
    global _appRef
    _appRef = app
    _ensureThread(force=True)


def remotes():
    return config.getRemotes()


def findRemote(mid):
    for r in remotes():
        if r['id'] == mid:
            return dict(r)
    return None


def _headers(remote):
    return {'X-Deskport-Token': remote['token']}


def fetchExternalApps(remote):
    """拉取远端广播的外部应用清单; 失败返回 None (保留上次已知)。"""
    try:
        resp = requests.get(remote['base'] + '/api/external-apps',
                            headers=_headers(remote), timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get('apps') or []
    except (requests.RequestException, ValueError):
        pass
    return None


def _allowedApp(remote, appId):
    return '*' in remote['apps'] or appId in remote['apps']


def _refreshLoop():
    while True:
        refreshOnce()
        time.sleep(REFRESH_SECONDS)


def refreshOnce():
    """刷新一轮: 按各远端的消费策略拉取终端会话与外部应用清单, 并同步发现挂载。"""
    for r in remotes():
            # 终端: 策略 terminal=false 的机器不拉取、不清缓存条目 (直接清空)
            if r['terminal']:
                entries = None
                try:
                    resp = requests.get(r['base'] + '/api/terminal/sessions',
                                        headers=_headers(r), timeout=REQUEST_TIMEOUT)
                    if resp.status_code == 200:
                        entries = resp.json().get('sessions') or []
                except (requests.RequestException, ValueError):
                    pass
                with _lock:
                    prev = _cache.get(r['id']) or {'entries': [], 'ok': False}
                    if entries is not None:
                        # 刷新成功: 整体替换; 失败: 保留上次已知会话, 仅标记 offline
                        prev['entries'] = entries
                        prev['ok'] = True
                        for e in entries:
                            if e.get('id'):
                                _owners[e['id']] = r['id']
                    else:
                        prev['ok'] = False
                    _cache[r['id']] = prev
            else:
                with _lock:
                    _cache.pop(r['id'], None)

            # 外部应用发现: apps 白名单为空的机器不接 (缺省不接, 显式接受)
            if r['apps']:
                apps = fetchExternalApps(r)
                picked = None
                if apps is not None:
                    picked = [dict(a) for a in apps
                              if isinstance(a, dict) and _allowedApp(r, str(a.get('id') or ''))]
                with _lock:
                    prev = _extCache.get(r['id']) or {'apps': [], 'ok': False}
                    if picked is not None:
                        prev['apps'] = picked
                        prev['ok'] = True
                    else:
                        prev['ok'] = False
                    _extCache[r['id']] = prev
            else:
                with _lock:
                    _extCache.pop(r['id'], None)
    _syncDiscovered()


def _syncDiscovered():
    """把缓存里策略允许的远端应用同步进本机路由 (发现是远端意志, 失败不影响刷新)。"""
    app = _appRef
    if not app:
        return
    try:
        externalProxy.syncDiscovered(app, discoveredExternalApps())
    except Exception as exc:  # noqa: BLE001 动态注册竞态等, 下一轮重试
        _log.warning('sync discovered external apps failed: %s', exc)


def _ensureThread(force=False):
    global _started
    if not _started and (force or remotes()):
        _started = True
        threading.Thread(target=_refreshLoop, daemon=True,
                         name='federation-refresh').start()


def remember(mid, sessionId):
    if sessionId:
        _owners[sessionId] = mid


def ownerOf(sessionId):
    return _owners.get(sessionId)


def remoteEntries():
    """缓存的远端会话条目; 不发起任何网络请求。"""
    _ensureThread()
    out = []
    with _lock:
        for r in remotes():
            cached = _cache.get(r['id'])
            if not cached:
                continue
            for e in cached['entries']:
                item = dict(e)
                item['machine'] = r['name']
                item['machineId'] = r['id']
                item['remote'] = True
                item['offline'] = not cached['ok']
                item['label'] = ('[%s] %s' % (r['name'], item.get('label')
                                              or item.get('cmd') or '')).strip()
                out.append(item)
    return out


def mergeTerminalSessions(sessions):
    """本地会话 + 缓存的远端会话 (remote 标记, 客户端不自动附着)。"""
    if not remotes():
        return sessions
    return list(sessions) + remoteEntries()


def discoveredExternalApps():
    """缓存里策略允许的远端外部应用, 摊平成挂载条目 (不发起网络请求)。

    id 加机器前缀避免与本地挂载撞名; base/token 供反代透传与联邦鉴权。
    """
    out = []
    with _lock:
        for r in remotes():
            cached = _extCache.get(r['id'])
            if not cached or not cached['apps']:
                continue
            for a in cached['apps']:
                item = dict(a)
                item['remote'] = r['name']
                item['remoteId'] = r['id']
                item['base'] = r['base']
                item['upstream'] = r['base']     # 挂载条目与本地同形, _forward 消费
                item['token'] = r['token']
                item['id'] = '%s:%s' % (r['id'], item.get('id') or '?')
                out.append(item)
    return out


def externalNavItems():
    """导航项: 发现的外部应用与本机挂载同形 (external: true → 新标签页)。"""
    _ensureThread()
    return externalProxy.discoveredNavItems()


def wsUpstreamFor(sessionId, query=''):
    """session id 归属远端时返回 (ws url, headers); 本机会话返回 None。

    terminal=false 的机器不接受其终端: 不路由 WS。
    """
    mid = ownerOf(sessionId)
    remote = findRemote(mid) if mid else None
    if not remote or not remote.get('terminal'):
        return None
    base = remote['base']
    scheme = 'wss://' if base.startswith('https://') else 'ws://'
    return scheme + base[len('http://'):] + '/api/terminal/ws' + \
        (('?' + query) if query else ''), _headers(remote)


def killUpstreamFor(sessionId):
    """DELETE 转发目标: 返回 (url, headers) 或 None。"""
    mid = ownerOf(sessionId)
    remote = findRemote(mid) if mid else None
    if not remote or not remote.get('terminal'):
        return None
    return (remote['base'] + '/api/terminal/sessions/' + sessionId,
            _headers(remote))


def machineOptions():
    """新建会话的机器选项: 本机 + 接受其终端的远端（id 为空串表示本机）。"""
    out = [{'id': '', 'name': '本机'}]
    for r in remotes():
        if r.get('terminal'):
            out.append({'id': r['id'], 'name': r['name']})
    return out
