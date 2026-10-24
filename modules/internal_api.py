# -*- coding: utf-8 -*-
"""web ↔ 会话守护进程的唯一内部契约。

双方只依赖本模块里的常量与函数:
    - 协议版本与能力 (PROTO / CAPS)
    - 内部路径 (wsPath / sessionsPath / healthPath / disconnectPath)
    - 令牌头 (TOKEN_HEADER)
    - 从 web 侧发起健康检查与 SSH 断开的客户端函数

守护进程实现、web 代理、ssh 模块都不应再互相 import。
"""
import requests

import config

PROTO = 1
CAPS = ['replay', 'preempt', 'resize']
TOKEN_HEADER = 'X-Sessiond-Token'


def wsPath(kind):
    return '/ws/terminal' if kind == 'local' else '/ws/ssh'


def sessionsPath(kind):
    return '/sessions'


def healthPath(kind):
    return '/health'


def disconnectPath():
    return '/disconnect'


def base(kind):
    return config.getSessiondUrl(kind)


def token(kind):
    return config.getSessiondToken(kind)


def headers(kind):
    return {TOKEN_HEADER: token(kind)}


def wsUrl(kind, query=''):
    urlBase = base(kind)
    if urlBase.startswith('https://'):
        urlBase = 'wss://' + urlBase[len('https://'):]
    elif urlBase.startswith('http://'):
        urlBase = 'ws://' + urlBase[len('http://'):]
    elif not urlBase.startswith('ws'):
        urlBase = 'ws://' + urlBase
    url = urlBase + wsPath(kind)
    return url + (('?' + query) if query else '')


def health(kind, timeout=2):
    """返回健康字典; 服务未启用或不可访问时返回 None。"""
    urlBase = base(kind)
    if not urlBase:
        return None
    try:
        resp = requests.get(urlBase + healthPath(kind), headers=headers(kind),
                            timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        data.setdefault('kind', kind)
        return data
    except (requests.RequestException, ValueError):
        return None


def services(timeout=1.5):
    """两个会话守护进程的统一状态视图，供 control/meta 复用。"""
    out = []
    for kind in ('local', 'ssh'):
        configured = bool(base(kind))
        data = health(kind, timeout=timeout) if configured else None
        out.append({
            'kind': kind,
            'configured': configured,
            'ok': bool(data),
            'pid': (data or {}).get('pid'),
            'proto': (data or {}).get('proto', PROTO if data else None),
            'sessions': (data or {}).get('sessions'),
            'url': base(kind),
        })
    return out


def disconnectSsh(hostId):
    """让 SSH 守护进程结束该主机的会话并断开连接。"""
    urlBase = base('ssh')
    if not urlBase:
        return None
    try:
        return requests.post(urlBase + disconnectPath(), params={'hostId': hostId},
                             headers=headers('ssh'), timeout=5)
    except requests.RequestException:
        return None