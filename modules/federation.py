# -*- coding: utf-8 -*-
"""联邦会话: 把其他 Deskport 实例的终端会话合并进本机列表。

中转模式: 会话清单由门面 (hub) 聚合下发, WS 由 hub 按 session id 路由到
归属机器的 Deskport (带联邦 token)。会话本体仍活在各机器的进程里,
hub 重启不影响任何会话; 远端条目在侧栏带 [机器名] 标签。

约定:
    - 远端 base 指向其 web 根; 认证走 X-Deskport-Token (与 auth.py 一致)
    - 首条上游 hello 帧的 session id 会登记到归属表, 供 WS 路由与 DELETE 转发
    - 未配置 remotes 时本模块全部为空操作
"""
import requests

import config


def remotes():
    return config.getRemotes()


def findRemote(mid):
    for r in remotes():
        if r['id'] == mid:
            return dict(r)
    return None


_owners = {}


def remember(mid, sessionId):
    if sessionId:
        _owners[sessionId] = mid


def ownerOf(sessionId):
    return _owners.get(sessionId)


def _headers(remote):
    return {'X-Deskport-Token': remote['token']}


def _wsBase(base):
    if base.startswith('https://'):
        return 'wss://' + base[len('https://'):]
    return 'ws://' + base[len('http://'):]


def mergeTerminalSessions(sessions):
    """本地会话列表 + 各远端终端会话; 远端条目打 [机器名] 标签并登记归属。"""
    remoteList = remotes()
    if not remoteList:
        return sessions
    out = list(sessions)
    for r in remoteList:
        try:
            resp = requests.get(r['base'] + '/api/terminal/sessions',
                                headers=_headers(r), timeout=4)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue
        for entry in data.get('sessions') or []:
            entry['machine'] = r['name']
            entry['machineId'] = r['id']
            entry['label'] = ('[%s] %s' % (r['name'], entry.get('label')
                                           or entry.get('cmd') or '')).strip()
            if entry.get('id'):
                remember(r['id'], entry['id'])
            out.append(entry)
    return out


def wsUpstreamFor(sessionId, query=''):
    """session id 归属远端时返回 (ws url, headers); 本机会话返回 None。"""
    mid = ownerOf(sessionId)
    remote = findRemote(mid) if mid else None
    if not remote:
        return None
    return _wsBase(remote['base']) + '/api/terminal/ws' + \
        (('?' + query) if query else ''), _headers(remote)


def killUpstreamFor(sessionId):
    """DELETE 转发目标: 返回 (url, headers) 或 None。"""
    mid = ownerOf(sessionId)
    remote = findRemote(mid) if mid else None
    if not remote:
        return None
    return (remote['base'] + '/api/terminal/sessions/' + sessionId,
            _headers(remote))
