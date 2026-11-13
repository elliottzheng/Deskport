# -*- coding: utf-8 -*-
"""联邦文件读写 — 文件页按 machine 参数把请求转发给成员 Deskport。

两种角色由同一份代码承担 (两端跑同一份代码):

    中枢  请求带 machine=<成员id>
          - 只读 browse/download/thumb/read/search 需要 remotes[].files；
          - 写 rename/new-folder/recycle-delete/paste/upload/write 需要
            remotes[].filesWrite（缺省 false，files=true 仍是只读；批次 H
            起这道门经 capability.decide 判定，开关即规则）；
          - 转发件复用 external_proxy: hop 头剥离 + X-Deskport-Token 鉴权 +
            iter_content 生成器；上传体走 request.stream，不在中枢整块缓冲；
          - 永久 delete / extract / compress / zip / remote-download / pins
            永不转发（返回 403，成员端也把它们挡在联邦白名单外）。

    成员  收到带 X-Deskport-From 的文件请求（被中枢转发过来）
          - 只放行 READ_ONLY_PATHS + WRITE_PATHS 的并集；
          - 写操作要求 path/最终目标落点在 config.federationRoots 内
            （本地路径 realpath 后比较，UNC 只做文本归一；空数组 =
            带 X-Deskport-From 的联邦写一律 403；判定经
            capability.decide，见 federation_write.checkPaths）；
          - 记一条审计 from=<中枢> actor=<谁> 后放行本机路由；
          - 收到带 machine 的联邦请求也只在本机处理，绝不二次转发（单跳）。

身份: 中枢用 X-Deskport-Actor 带网页请求来源（IP），成员只把它写进审计，
授权只看根白名单，不引入第二套 ACL。
"""
import json
import logging
import os
import threading
import time
from urllib.parse import quote, unquote

import requests
from flask import Response, jsonify, request

import config
from modules import capability, events
from modules import federation
from modules import federation_write
from modules.external_proxy import HOP_BY_HOP, _dropHopHeaders  # noqa: PLC2701 同包复用转发件

_log = logging.getLogger('files-federation')

READ_ONLY_PATHS = ('/api/files', '/api/files/download', '/api/files/thumb',
                   '/api/files/read', '/api/files/search')
WRITE_PATHS = ('/api/files/rename', '/api/files/new-folder',
               '/api/files/recycle-delete', '/api/files/paste',
               '/api/files/upload', '/api/files/write')
FEDERATED_PATHS = READ_ONLY_PATHS + WRITE_PATHS
# pins 是中枢本地 UI 状态, 永不转发; 联邦请求命中本地状态一律拒绝
LOCAL_PATHS = ('/api/files/pins',)

_OP_NAMES = {'/api/files': 'browse', '/api/files/download': 'download',
             '/api/files/thumb': 'thumb', '/api/files/read': 'read',
             '/api/files/search': 'search', '/api/files/rename': 'rename',
             '/api/files/new-folder': 'new-folder',
             '/api/files/recycle-delete': 'recycle-delete',
             '/api/files/paste': 'paste', '/api/files/upload': 'upload',
             '/api/files/write': 'write'}

_MB = 1024 * 1024
_limitCache = {}      # mid -> (expiresAt, maxMB)
_limitLock = threading.Lock()
_sessionLocal = threading.local()


def _session():
    s = getattr(_sessionLocal, 'session', None)
    if s is None:
        s = requests.Session()
        _sessionLocal.session = s
    return s


def _fromHeader():
    """发起方身份: 本机品牌名, 百分号编码进 header (中文品牌名安全)。"""
    return quote(config.getBrandName(), safe='')


def _actorOf():
    """审计用的操作者: 中枢填的 X-Deskport-Actor；没有则退回 TCP 来源 IP。"""
    raw = request.headers.get('X-Deskport-Actor') or ''
    return unquote(raw).strip() or (request.remote_addr or 'unknown')


def _opOf(path):
    return _OP_NAMES.get(path, path)


def _targetPathOf():
    """审计用文件路径: query 的 path, 没有再看 JSON body 的 path。"""
    p = request.args.get('path')
    if p:
        return p
    if request.method in ('POST', 'PUT'):
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return str(data.get('path') or '')
    return ''


def _machineFromRequest():
    """machine 参数: query 优先, JSON body / 表单兜底 (JSON 里会顺手剥掉)。"""
    mid = request.args.get('machine') or ''
    if mid:
        return mid
    if request.method in ('POST', 'PUT'):
        ctype = (request.headers.get('Content-Type') or '').lower()
        if 'json' in ctype:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                return str(data.pop('machine', '') or '')
        elif 'form' in ctype or 'multipart' in ctype:
            return str(request.form.get('machine') or '')
    return ''


def _stripMachine():
    """成员端剥掉 machine（JSON body 可变；query/form 的 machine 本机路由不读）。"""
    if request.method in ('POST', 'PUT'):
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            data.pop('machine', None)


def _writePathsOf(path):
    """按端点收集"最终会影响磁盘"的路径，供根白名单校验。"""
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    if path in ('/api/files/rename', '/api/files/new-folder'):
        parent = str(data.get('path') or '')
        key = 'newName' if path.endswith('/rename') else 'name'
        name = str(data.get(key) or '')
        paths = [parent]
        if parent and name:
            base = os.path.dirname(os.path.normpath(parent)) \
                if path.endswith('/rename') else parent
            paths.append(os.path.join(base, name))
        return paths
    if path == '/api/files/recycle-delete':
        raw = data.get('paths')
        return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    if path == '/api/files/paste':
        raw = data.get('paths')
        paths = [str(data.get('dest') or '')]
        if isinstance(raw, list):
            paths += [p for p in raw if isinstance(p, str)]
        return paths
    if path == '/api/files/upload':
        if request.form.get('tempdir') == 'true':
            return ['']      # 空路径必然被拒
        return [request.form.get('path') or '']
    if path == '/api/files/write':
        return [data.get('path') or '']
    return []


def register(app):
    app.before_request(_federatedFiles)


def _memberWriteAllowed(remote):
    """中枢侧写门（批次 H 起经 capability.decide 判定）：成员的
    filesWrite 开关降级成一条规则——开 = file:write@*，关 = 无规则
    （空规则 = 一律拒绝）。路径边界在成员自己的 federationRoots，
    中枢这里只回答「这个成员开没开联邦写」。"""
    rules = capability.compile(
        [{'action': 'file:write', 'resource': '*'}]
        if remote.get('filesWrite') else [])
    verdict, _why = capability.decide(
        rules, ('remote', str(remote.get('name') or '')), 'file:write', 'path:*')
    return verdict == 'allow'


def _federatedFiles():
    path = request.path
    if path != '/api/files' and not path.startswith('/api/files/'):
        return None

    src = request.headers.get('X-Deskport-From')
    if src:
        # 成员侧: 即使同时带了 machine 也只在本机处理，绝不二次转发
        return _memberMode(path, src)
    if path in LOCAL_PATHS:
        return None

    mid = _machineFromRequest()
    if not mid:
        return None

    remote = federation.findRemote(mid)
    if remote is None:
        return jsonify({'error': '联邦成员不存在: %s' % mid}), 404
    if not remote.get('files'):
        return jsonify({'error': '成员 %s 未开放文件浏览' % remote['name']}), 403
    if path in WRITE_PATHS:
        if not _memberWriteAllowed(remote):
            return jsonify({'error': '成员 %s 未开放文件写权限'
                                     '（remotes[].filesWrite）' % remote['name']}), 403
        if path == '/api/files/upload':
            tooLarge = _uploadTooLarge(remote)
            if tooLarge is not None:
                return tooLarge
    elif path not in READ_ONLY_PATHS:
        return jsonify({'error': '该文件操作不向联邦转发'
                                 '（永久删除/解压/压缩/远程下载/pins 等）'}), 403
    return _forward(remote, path)


def _memberMode(path, src):
    """成员端: 只放行联邦白名单，写操作再过根白名单，然后交给本机路由。"""
    hubName = unquote(src).strip()
    actor = _actorOf()
    who = 'from=%s actor=%s' % (hubName or '?', actor)
    detail = '%s %s %s' % (_opOf(path), _targetPathOf(), who)
    if path in LOCAL_PATHS:
        events.log('files.federation', '来自 ' + hubName, 'deny',
                   detail + ' 本地状态不向联邦开放', request.remote_addr or '')
        return jsonify({'error': '该端点不向联邦开放: %s' % path}), 403
    if path not in FEDERATED_PATHS:
        events.log('files.federation', '来自 ' + hubName, 'deny',
                   detail + ' 不在联邦白名单', request.remote_addr or '')
        return jsonify({'error': '该端点不向联邦开放: %s' % path}), 403
    _stripMachine()
    if path in WRITE_PATHS:
        ok, why = federation_write.checkPaths(_writePathsOf(path))
        if not ok:
            events.log('files.federation', '来自 ' + hubName, 'deny',
                       detail + ' ' + why, request.remote_addr or '')
            return jsonify({'error': '联邦写操作被拒绝: ' + why}), 403
    events.log('files.federation', '来自 ' + hubName, 'ok', detail,
               request.remote_addr or '')
    return None


def _memberUploadLimitMB(remote):
    """取成员声明的上传上限并与本机取小；失败退回本机值（成员端仍会复核）。"""
    now = time.monotonic()
    with _limitLock:
        hit = _limitCache.get(remote['id'])
        if hit and hit[0] > now:
            return hit[1]
    memberMB = 0
    try:
        resp = _session().get(remote['base'] + '/api/files/federation-info',
                              headers={'X-Deskport-Token': remote['token']},
                              timeout=3)
        if resp.status_code == 200:
            memberMB = int(resp.json().get('maxUploadMB') or 0)
    except (requests.RequestException, ValueError, TypeError):
        pass
    localMB = federation_write.uploadLimitMB()
    limit = min(localMB, memberMB) if memberMB > 0 else localMB
    with _limitLock:
        _limitCache[remote['id']] = (now + 300, limit)
    return limit


def _uploadTooLarge(remote):
    """中枢先拒: Content-Length 超过两端较小上限（留 multipart 余量）。"""
    length = request.content_length or 0
    if not length:
        return None
    limit = _memberUploadLimitMB(remote) * _MB
    if length > limit + 65536:
        events.log('files.federation', remote['name'], 'deny',
                   'upload 超过上限 %dMB' % (limit // _MB),
                   request.remote_addr or '')
        return jsonify({'error': '上传超过两端较小上限 %dMB' % (limit // _MB)}), 413
    return None


def _forward(remote, path):
    """把当前请求流式转发给成员; 返回成员响应或 502。"""
    args = [(k, v) for k, v in request.args.items(multi=True) if k != 'machine']
    target = remote['base'] + path
    if args:
        target += '?' + '&'.join('%s=%s' % (k, quote(str(v), safe=''))
                                 for k, v in args)
    # query 里的 path 常含反斜杠/中文/空格: requests 会原样发, 成员端 Flask
    # 按 UTF-8 解 query, 所以这里只需要百分号编码非 ASCII 与保留字符

    headers = _dropHopHeaders(request.headers)
    # 中枢浏览器的登录态不外送: 成员侧统一靠 X-Deskport-Token 鉴权
    if 'Cookie' in headers:
        del headers['Cookie']
    headers['X-Deskport-Token'] = remote['token']
    headers['X-Deskport-From'] = _fromHeader()
    headers['X-Deskport-Actor'] = quote(request.remote_addr or 'unknown', safe='')

    data = None
    if request.method in ('POST', 'PUT'):
        ctype = (request.headers.get('Content-Type') or '').lower()
        if 'json' in ctype:
            body = request.get_json(silent=True)
            body = body if isinstance(body, dict) else {}
            body.pop('machine', None)
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers.pop('Content-Length', None)
        elif 'multipart/form-data' in ctype or 'application/x-www-form-urlencoded' in ctype:
            # 上传体可能很大: 流式转发, Content-Length/Content-Type 原样保留
            data = request.stream
        else:
            data = request.stream

    name = remote['name']
    try:
        upstream = _session().request(request.method, target, headers=headers,
                                      data=data, stream=True,
                                      allow_redirects=False, timeout=(5, None))
    except requests.RequestException as exc:
        _log.warning('成员 %s 不可达: %s', name, exc)
        events.log('files.federation', name, 'fail',
                   '%s %s actor=%s' % (_opOf(path), _targetPathOf(), _actorOf()),
                   request.remote_addr or '')
        return jsonify({'error': '成员 %s 离线' % name}), 502

    events.log('files.federation', name,
               result='ok' if upstream.status_code < 400 else str(upstream.status_code),
               detail='%s %s actor=%s' % (_opOf(path), _targetPathOf(), _actorOf()),
               ip=request.remote_addr or '')

    respHeaders = [(k, v) for k, v in upstream.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() != 'content-type']

    def generate():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp = Response(generate(), status=upstream.status_code,
                    content_type=upstream.headers.get('Content-Type'))
    for key, value in respHeaders:
        resp.headers.add(key, value)
    return resp