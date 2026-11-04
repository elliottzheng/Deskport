# -*- coding: utf-8 -*-
"""联邦会话: 把其他 Deskport 实例的终端会话合并进本机列表。

非阻塞设计 (2026-11 修复):
    - 远端列表由后台线程每 5 秒刷新并缓存, 本地会话接口永远不等远端;
    - 远端不可达时返回旧缓存 (条目带 offline 标记), 超时只发生在后台线程;
    - 远端条目标记 remote=True, 客户端恢复时不自动附着, 点侧栏才连
      (否则远端一离线, 自动附着就变成无限重连, 表现为终端页疯狂滚动)。
"""
import threading
import time

import requests

import config

REFRESH_SECONDS = 5.0
REQUEST_TIMEOUT = 4.0

_lock = threading.Lock()
_cache = {}        # mid -> {'entries': [...], 'ok': bool}
_owners = {}       # session id -> mid
_started = False


def remotes():
    return config.getRemotes()


def findRemote(mid):
    for r in remotes():
        if r['id'] == mid:
            return dict(r)
    return None


def _headers(remote):
    return {'X-Deskport-Token': remote['token']}


def _refreshLoop():
    while True:
        for r in remotes():
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
        time.sleep(REFRESH_SECONDS)


def _ensureThread():
    global _started
    if not _started and remotes():
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


def wsUpstreamFor(sessionId, query=''):
    """session id 归属远端时返回 (ws url, headers); 本机会话返回 None。"""
    mid = ownerOf(sessionId)
    remote = findRemote(mid) if mid else None
    if not remote:
        return None
    base = remote['base']
    scheme = 'wss://' if base.startswith('https://') else 'ws://'
    return scheme + base[len('http://'):] + '/api/terminal/ws' + \
        (('?' + query) if query else ''), _headers(remote)


def killUpstreamFor(sessionId):
    """DELETE 转发目标: 返回 (url, headers) 或 None。"""
    mid = ownerOf(sessionId)
    remote = findRemote(mid) if mid else None
    if not remote:
        return None
    return (remote['base'] + '/api/terminal/sessions/' + sessionId,
            _headers(remote))
