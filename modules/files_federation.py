# -*- coding: utf-8 -*-
"""联邦文件浏览 — 文件页按 machine 参数把只读请求转发给成员 Deskport。

两种角色由同一个钩子承担 (两端跑同一份代码):
    中枢  请求带 machine=<成员id> 且该成员 files 策略开启 → 流式转发
          (复用 external_proxy 的转发件: hop 头剥离 + X-Deskport-Token 鉴权 +
          iter_content 生成器, 大文件不在中枢内存缓冲);
    成员  收到带 X-Deskport-From 的文件请求 (被中枢转发过来) → 记一条审计后
          原样放行, 让本机 files 路由按自己的路径正常响应。

只读第一版: browse/download/thumb/read/search 五条; 写操作与 pins 不转发
(写操作在成员模式下 403, pins 是中枢本地的界面状态)。
machine 参数转发前一律剥掉 (query 与 JSON body 都剥), 防止成员端再次拦截造成回环。
"""
import json
import logging
import threading
from urllib.parse import quote, unquote

import requests
from flask import Response, jsonify, request

import config
from modules import events
from modules import federation
from modules.external_proxy import HOP_BY_HOP, _dropHopHeaders  # noqa: PLC2701 同包复用转发件

_log = logging.getLogger('files-federation')

# V1 允许转发给成员的只读端点
READ_ONLY_PATHS = ('/api/files', '/api/files/download', '/api/files/thumb',
                   '/api/files/read', '/api/files/search')
# pins 是中枢本地 UI 状态, 永不转发
LOCAL_PATHS = ('/api/files/pins',)

_OP_NAMES = {'/api/files': 'browse', '/api/files/download': 'download',
             '/api/files/thumb': 'thumb', '/api/files/read': 'read',
             '/api/files/search': 'search'}

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


def register(app):
    app.before_request(_federatedFiles)


def _federatedFiles():
    path = request.path
    if path != '/api/files' and not path.startswith('/api/files/'):
        return None
    if path in LOCAL_PATHS:
        return None

    mid = _machineFromRequest()
    if not mid:
        src = request.headers.get('X-Deskport-From')
        if src:
            # 成员侧: 被中枢转发的文件请求, 记审计后放行本机路由
            events.log('files.federation', target='来自 ' + unquote(src),
                       result='ok',
                       detail='%s %s' % (_opOf(path), _targetPathOf()),
                       ip=request.remote_addr or '')
        return None

    remote = federation.findRemote(mid)
    if remote is None:
        return jsonify({'error': '联邦成员不存在: %s' % mid}), 404
    if not remote.get('files'):
        return jsonify({'error': '成员 %s 未开放文件浏览' % remote['name']}), 403
    if path not in READ_ONLY_PATHS:
        return jsonify({'error': '成员文件第一版仅支持只读浏览'}), 403
    if request.headers.get('X-Deskport-From'):
        return jsonify({'error': '不允许联邦接力 (已在中枢 A, 不再向成员外转发)'}), 403
    return _forward(remote, path)


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

    data = None
    if request.method in ('POST', 'PUT'):
        ctype = (request.headers.get('Content-Type') or '').lower()
        if 'json' in ctype:
            body = request.get_json(silent=True)
            body = body if isinstance(body, dict) else {}
            body.pop('machine', None)
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            if 'Content-Length' in headers:
                del headers['Content-Length']
        else:
            data = request.get_data(cache=True)
            if 'Content-Length' in headers:
                del headers['Content-Length']

    name = remote['name']
    try:
        upstream = _session().request(request.method, target, headers=headers,
                                      data=data, stream=True,
                                      allow_redirects=False, timeout=(5, None))
    except requests.RequestException as exc:
        _log.warning('成员 %s 不可达: %s', name, exc)
        events.log('files.federation', target=name, result='fail',
                   detail='%s %s' % (_opOf(path), _targetPathOf()),
                   ip=request.remote_addr or '')
        return jsonify({'error': '成员 %s 离线' % name}), 502

    events.log('files.federation', target=name,
               result='ok' if upstream.status_code < 400 else str(upstream.status_code),
               detail='%s %s' % (_opOf(path), _targetPathOf()),
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
