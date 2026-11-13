# -*- coding: utf-8 -*-
"""分享「只上传」的机器维度 — 目标聚合与跨机上传（批次 E）。

只上传有两个轴：机器（node）与该机器上的上传目标（target id）。本模块
只回答这两件事，页面与交付入口共用一个答案：

    GET /api/share/targets   → {targets: [{node, id, name, status}], nodes, default}
    POST /api/share/submit   way=upload / node!=local 时把文件送到那台机器

路径不出网的实现方式:
    - 对外只给 id/name；本机路径来自 config.getUploadTargets() 也只取
      id/name，远端由对方自己把 target id 映射成目录；
    - 远端落盘走对方现成的 POST /api/agent/upload（target + subdir），
      成员端用自己的 uploadTargets 解析，中枢全程不知道任何绝对路径；
    - 对方回的错误文本里可能带崩溃路径，_scrub 先抹掉再下发。

授权不在这里: `upload_remote` 那道门由入口（share_web）按票据的签发
令牌判断；本模块只负责"怎么连、连哪台、带什么头"。

离线与缓存:
    - 拉不到目标不等于隐藏这台机器：nodes[] 里仍有一行 status=offline，
      目标用上一次成功的结果（若有）并同样标 offline；
    - 缓存 TTL 20 秒，避免每次开页面都串行等一遍网络。
"""
import concurrent.futures
import os
import re
import threading
import time
from urllib.parse import quote

import requests

import config
from modules import agent_nodes
from modules.agent_tunnel import dropTunnelsFor, tunnelPort

CACHE_TTL = 20.0
FETCH_TIMEOUT = 4.0
UPLOAD_TIMEOUT = 300
MAX_TARGETS = 30

_ABS_PATH = re.compile(
    r'(?:[A-Za-z]:[\\/][^\s，。；;、\'"]*'
    r'|\\\\[^\s，。；;、\'"]*'
    r'|(?<![\w.])/[^\s，。；;、\'"]+)')

_cache = {}
_lock = threading.Lock()


class TargetError(Exception):
    """跨机目标的失败：消息给用户看，status 是入口该回的状态码。"""

    def __init__(self, message, status=502):
        super(TargetError, self).__init__(message)
        self.status = status


# ---------------------------------------------------------------- 目标列表

def _entryOf(name):
    """机器名 → ('agent'|'federation', 配置条目)；不存在 ('', None)。"""
    entry = agent_nodes.findNode(name)
    if entry is not None:
        return 'agent', entry
    member = agent_nodes.findFederationMember(name)
    if member is not None:
        return 'federation', member
    return '', None


def _cleanRows(raw):
    """只留 id/name（对方即使误回 path 也不外传），去重、截断。"""
    rows, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get('id') or '').strip()
        name = str(item.get('name') or '').strip()
        if not tid or not name or tid in seen or len(tid) > 40 or len(name) > 40:
            continue
        seen.add(tid)
        rows.append({'id': tid, 'name': name})
        if len(rows) >= MAX_TARGETS:
            break
    return rows


def _localRows():
    rows = []
    for item in config.getUploadTargets():
        rows.append({'id': item['id'], 'name': item['name'],
                     'status': 'online' if str(item.get('path') or '').strip()
                     else 'offline'})
    return rows


def _memberRows(entry):
    resp = requests.get(
        entry['base'] + '/api/agent/upload/targets',
        headers={'X-Deskport-Token': entry.get('token') or '',
                 'X-Deskport-From': quote(config.getBrandName(), safe='')},
        timeout=FETCH_TIMEOUT)
    if resp.status_code != 200:
        raise TargetError('成员机 %s 的上传目标不可用（HTTP %d）'
                          % (entry.get('name') or entry.get('id'),
                             resp.status_code))
    try:
        data = resp.json()
    except ValueError:
        raise TargetError('成员机返回了无法解析的上传目标')
    return _cleanRows(data.get('targets'))


def _fetchNode(name):
    kind, entry = _entryOf(name)
    if not kind:
        raise TargetError('机器不存在: %s' % name, 400)
    try:
        if kind == 'agent':
            status, data = agent_nodes.nodeRequest(
                entry, 'GET', '/api/agent/upload/targets',
                timeout=FETCH_TIMEOUT)
            if status != 200:
                raise TargetError('机器 %s 的上传目标不可用（HTTP %d）'
                                  % (name, status))
            return _cleanRows(data.get('targets'))
        return _memberRows(entry)
    except TargetError:
        raise
    except Exception as e:
        raise TargetError('机器 %s 暂时连不上：%s' % (name, _scrub(e)))


def _remember(name, rows, online):
    with _lock:
        _cache[name] = {'at': time.monotonic(), 'targets': list(rows),
                        'online': online}


def _cached(name):
    with _lock:
        hit = _cache.get(name)
    return (list(hit['targets']), hit['online']) if hit else ([], False)


def remoteTargets(name):
    """某台机器的目标：返回 (rows, online)。

    新鲜缓存直接用；拉取失败退回上一次成功结果（online=False）。机器压根
    不存在 → TargetError(400)，入口应回 400。
    """
    rows, online = _cached(name)
    with _lock:
        hit = _cache.get(name)
    if hit and time.monotonic() - hit['at'] < CACHE_TTL:
        return rows, online
    try:
        rows = _fetchNode(name)
    except TargetError as e:
        if e.status == 400:
            raise
        _remember(name, rows, False)
        return rows, False
    _remember(name, rows, True)
    return rows, True


def _fetchMany(names):
    """并发拉取；单台超时/异常不影响其他机器，也不会隐藏它。"""
    out = {}
    if not names:
        return out
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(names)))
    futures = {name: pool.submit(remoteTargets, name) for name in names}
    try:
        for name, future in futures.items():
            try:
                out[name] = future.result(timeout=FETCH_TIMEOUT + 2)
            except Exception:
                out[name] = (_cached(name)[0], False)
    finally:
        pool.shutdown(wait=False)
    return out


def targetsPayload():
    """页面用：本机 + 各机器的目标；离线机器保留一行（status=offline）。"""
    targets = [dict(row, node='local') for row in _localRows()]
    nodes = [{'name': 'local', 'label': '本机', 'status': 'online'}]
    seen = {'local'}
    names = []
    for item in list(config.getAgentNodes()) + list(config.getRemotes()):
        name = str(item.get('name') or item.get('id') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)

    for name, (rows, online) in _fetchMany(names).items():
        status = 'online' if online else 'offline'
        nodes.append({'name': name, 'label': name, 'status': status})
        for row in rows:
            targets.append({'node': name, 'id': row['id'],
                            'name': row['name'], 'status': status})
    return {'targets': targets, 'nodes': nodes,
            'default': {'node': 'local', 'id': _firstUsable(targets)}}


def _firstUsable(targets):
    for row in targets:
        if row['node'] == 'local' and row['status'] == 'online':
            return row['id']
    return ''


# ---------------------------------------------------------------- 跨机上传

def _endpoint(kind, entry):
    """(base, headers)：agent node 直连/隧道 + 它的令牌；联邦成员用成员令牌。"""
    if kind == 'agent':
        base = entry['url']
        if entry.get('via'):
            base = 'http://127.0.0.1:%d' % tunnelPort(entry)
        return base, {'X-Agent-Token': entry.get('token') or ''}
    return entry['base'], {
        'X-Deskport-Token': entry.get('token') or '',
        'X-Deskport-From': quote(config.getBrandName(), safe=''),
        'X-Deskport-Actor': quote('share-upload', safe='')}


def _scrub(text):
    """错误文本里可能带对方机器的绝对路径：逐段抹掉再下发。"""
    return _ABS_PATH.sub('…', str(text or ''))[:200]


def _memberError(resp, label, target):
    try:
        data = resp.json()
    except ValueError:
        data = {}
    message = _scrub(data.get('error') or 'HTTP %d' % resp.status_code)
    if resp.status_code == 400 and '上传目标' in message:
        return TargetError('成员机 %s 上没有这个上传目标（%s）'
                           % (label, target), 400)
    status = resp.status_code if 400 <= resp.status_code < 500 else 502
    return TargetError('成员机 %s 上传失败：%s' % (label, message), status)


def _post(url, headers, target, subdir, parts):
    return requests.post(url, headers=headers,
                         data={'target': target, 'subdir': subdir or ''},
                         files=parts, timeout=(5, UPLOAD_TIMEOUT),
                         allow_redirects=False)


def sendFiles(name, target, subdir, files):
    """把本地文件送到 name 那台机器的 target 上；返回成员的 JSON 应答。

    成员端只收到 target id 与相对 subdir，绝对路径由它自己解析。连接失败
    （agent node 的 SSH 隧道死掉）先重建隧道再试一次。
    """
    kind, entry = _entryOf(name)
    if not kind:
        raise TargetError('机器不存在: %s' % name, 400)
    label = str(entry.get('name') or entry.get('id') or name)
    base, headers = _endpoint(kind, entry)
    handles, parts = [], []
    try:
        for path in files:
            handle = open(path, 'rb')
            handles.append(handle)
            parts.append(('file', (os.path.basename(path), handle)))
        try:
            resp = _post(base + '/api/agent/upload', headers, target, subdir,
                         parts)
        except requests.RequestException as first:
            if kind != 'agent' or not entry.get('via'):
                raise TargetError('成员机 %s 暂时连不上：%s'
                                  % (label, _scrub(first)), 502)
            dropTunnelsFor(entry)
            base, headers = _endpoint(kind, entry)
            for handle in handles:
                handle.seek(0)
            try:
                resp = _post(base + '/api/agent/upload', headers, target,
                             subdir, parts)
            except requests.RequestException as second:
                raise TargetError('成员机 %s 暂时连不上：%s'
                                  % (label, _scrub(second)), 502)
    finally:
        for handle in handles:
            handle.close()
    if resp.status_code >= 400:
        raise _memberError(resp, label, target)
    try:
        data = resp.json()
    except ValueError:
        raise TargetError('成员机 %s 返回了无法解析的结果' % label, 502)
    return data if isinstance(data, dict) else {}