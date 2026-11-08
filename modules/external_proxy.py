# -*- coding: utf-8 -*-
"""外部应用反代挂载 — 把本机自托管 Web 应用挂到面板子路径下。

两种挂载模式:
    full      上游已配 web_prefix: /<prefix>/<path> 原样透传到 upstream 同路径;
    namespace 兼容模式: 页面路径剥前缀转发 (/<prefix>/<x> → /<x>)，同时
              /_x/<path> 原样透传（远窗 anzhuo 前端引用全在 /_x/ 命名空间）。

安全模型: Deskport 登录守卫罩住全部挂载路由 (auth.before_request 天然覆盖)；
装配期做 prefix 冲突检测，与本系统路由重合直接拒绝。
全流式转发: 请求体走 request.stream、响应走 iter_content 生成器
(is_streamed=True 让 gzipText 跳过, SSE / Range 不被缓冲破坏)。
"""
import logging
import threading

import requests
from flask import Response, request
from werkzeug.routing import Rule

_log = logging.getLogger('external_proxy')

# hop-by-hop 头: 逐跳语义, 代理必须剥掉 (RFC 2616 13.5.1 + 常见补充)
HOP_BY_HOP = {'connection', 'keep-alive', 'transfer-encoding', 'upgrade',
              'te', 'trailer', 'proxy-authorization', 'proxy-authenticate'}
FORWARD_METHODS = ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS')
BODY_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')
# namespace 模式下原样透传的命名空间前缀 (远窗约定)
DEFAULT_NS_PREFIX = '/_x'

_apps = []
_appsLock = threading.RLock()

_sessionLocal = threading.local()


def _session():
    """线程内复用 requests.Session: 上游同一台机器, keep-alive 收益大。"""
    s = getattr(_sessionLocal, 'session', None)
    if s is None:
        s = requests.Session()
        _sessionLocal.session = s
    return s


def getApps():
    with _appsLock:
        return [dict(item) for item in _apps]


def isApiPath(path):
    """挂载路由下的 API 形态 (/<prefix>/api/...): 未登录返回 401 JSON 而非 302。"""
    with _appsLock:
        if any(path.startswith(app['prefix'] + '/api/') for app in _apps):
            return True
    with _discoveredLock:
        return any(path.startswith(p + '/api/') for p in _discovered)


def getNavItems():
    with _appsLock:
        return [{'id': app['id'], 'label': app['label'], 'icon': app['icon'],
                 'href': app['prefix'], 'external': True}
                for app in _apps]


# ---- 联邦发现的外部应用 (其他 Deskport 广播、本机策略允许的) ----
# 前缀 -> {remote, base, token, id, label, icon, mode}; 路由规则在首次发现时
# 注册一次并常驻 (指向同一个动态视图), 远端撤销只清映射 —— 视图对不在
# 映射里的前缀回 404, 不动 werkzeug 私有结构。
_discovered = {}
_discoveredLock = threading.RLock()
_FORWARDED_METHODS = list(FORWARD_METHODS)


def _resolveDiscovered(path):
    """按最长前缀匹配发现条目; 没有则 None (视图回 404)。"""
    best = None
    with _discoveredLock:
        for p, entry in _discovered.items():
            if path == p or path.startswith(p + '/'):
                if best is None or len(p) > len(best[0]):
                    best = (p, dict(entry))
    return best


def discoveredView(**kwargs):
    found = _resolveDiscovered(request.path)
    if not found:
        return Response('{"error": "not mounted"}', status=404,
                        mimetype='application/json')
    return _forward(found[1])


def syncDiscovered(app, apps):
    """把联邦发现的外部应用同步进路由: 新增注册规则 (只加一次), 撤销清映射。

    apps: [{remote, base, token, id, label, icon, prefix, mode}]。
    与本机路由/本机挂载冲突的条目直接跳过 (发现是远端意志, 不能让本机装配失败)。
    """
    wanted = {}
    for item in apps:
        prefix = str(item.get('prefix') or '').strip()
        if not prefix.startswith('/') or prefix.endswith('/') or '..' in prefix:
            continue
        if item.get('mode') not in ('full', 'namespace'):
            continue
        # 接力只支持 full 模式: namespace 的前端引用挂在宿主根 /_x/* 上,
        # 多台远端在 A 的根命名空间里会互相冲突 (A 无从区分归属)。
        # 远端要用 web_prefix 的 full 模式广播; 依赖远端自治的 /_x/ 约定
        # 属于单宿主场景, 不进联邦。
        if item.get('mode') != 'full':
            _log.warning('external app %s 跳过: 联邦接力仅支持 full 模式, '
                         'namespace 挂载请留在单宿主', item.get('id'))
            continue
        wanted[prefix] = item
    with _discoveredLock:
        for prefix in [p for p in _discovered if p not in wanted]:
            _log.info('external app %s unmounted (远端撤销 %s)',
                      _discovered[prefix].get('id'), prefix)
            del _discovered[prefix]
        for prefix, item in wanted.items():
            if prefix in _discovered:
                _discovered[prefix] = dict(item)
                continue
            try:
                validatePrefix(prefix, app)
            except ValueError as exc:
                _log.warning('external app %s 跳过: %s', item.get('id'), exc)
                continue
            _discovered[prefix] = dict(item)
            endpoint = 'federation_' + prefix.strip('/').replace('/', '_')
            if endpoint not in app.view_functions:
                app.url_map.add(Rule(prefix, endpoint=endpoint,
                                     methods=_FORWARDED_METHODS, strict_slashes=False))
                app.url_map.add(Rule(prefix + '/<path:filename>', endpoint=endpoint + '_path',
                                     methods=_FORWARDED_METHODS))
                app.view_functions[endpoint] = discoveredView
                app.view_functions[endpoint + '_path'] = discoveredView
            _log.info('external app %s discovered at %s (%s) <- %s via %s',
                      item.get('id'), prefix, item.get('mode'),
                      item.get('base'), item.get('remote'))


def discoveredNavItems():
    """导航项: 与本机挂载同形 (external: true → 新标签页)。"""
    with _discoveredLock:
        return [{'id': e['id'], 'label': e.get('label') or e['id'],
                 'icon': e.get('icon') or 'plug', 'href': p, 'external': True}
                for p, e in sorted(_discovered.items())]


def validatePrefix(prefix, app):
    """prefix 与已注册路由比对: 与任一静态路由重合即冲突, 报清晰错误。

    动态路由 (/<path:filename> 之类) 不参与: werkzeug 按具体度匹配,
    静态路由必然胜出, 不构成冲突。
    """
    if not prefix.startswith('/') or prefix.endswith('/') or '..' in prefix:
        raise ValueError('externalApp prefix 非法: %r (须以 / 开头、不以 / 结尾)' % prefix)
    for rule in app.url_map.iter_rules():
        # 动态路由取其静态段 (/<path:filename> → '') 参与比对:
        # werkzeug 虽按具体度让静态路由胜出, 但 prefix 覆盖到动态路由的
        # 静态段意味着语义冲突 (如 prefix=/assets 撞 /assets/<path:filename>)
        staticPart = rule.rule.split('<')[0].rstrip('/')
        if not staticPart:
            continue
        mine, theirs = prefix.rstrip('/'), staticPart
        if mine == theirs or mine.startswith(theirs + '/') or theirs.startswith(mine + '/'):
            raise ValueError(
                'externalApp prefix %s 与已注册路由 %s 冲突, 拒绝装配' % (prefix, rule.rule))


def _dropHopHeaders(headers):
    """剥 hop-by-hop 头, 同时解析 Connection 列出的逐跳头一并剥掉。"""
    connTokens = {t.strip().lower()
                  for t in (headers.get('Connection') or '').split(',') if t.strip()}
    return {key: value for key, value in headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() not in connTokens
            and key.lower() != 'host'}


def makeProxy(prefix, upstream, mode, token=''):
    """构造挂载在 prefix 下的透传视图。mode: 'full' | 'namespace'。

    token 非空时追加 X-Deskport-Token（联邦接力：上游是另一台 Deskport）。
    """
    nsPrefix = DEFAULT_NS_PREFIX

    def proxy(**kwargs):
        return _forward({'prefix': prefix, 'upstream': upstream,
                         'mode': mode, 'token': token})

    proxy.__name__ = 'external_' + prefix.strip('/').replace('/', '_')
    return proxy


def _forward(entry):
    """通用转发核心。entry: {prefix, upstream, mode, token}。"""
    prefix, upstream, mode = entry['prefix'], entry['upstream'], entry['mode']
    path = _resolveTargetPath(prefix, mode, request.path)
    target = upstream.rstrip('/') + path
    if request.query_string:
        target += '?' + request.query_string.decode('utf-8', 'replace')

    headers = _dropHopHeaders(request.headers)
    # 流式转发未知长度的 body 时 requests 会改用 chunked, 转发下来的
    # Content-Length 必须剥掉: CL 与 TE 并存是非法组合, aiohttp 直接 400
    if request.method in BODY_METHODS and 'Content-Length' in headers:
        del headers['Content-Length']
    remote = request.remote_addr or ''
    # 远窗防暴力只认 XFF 最后一段, 必须追加真实来源 IP (原值在前)
    existingXff = headers.get('X-Forwarded-For')
    headers['X-Forwarded-For'] = (
        existingXff + ', ' + remote if existingXff else remote)
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Host'] = request.host
    if entry.get('token'):
        # 联邦接力: 上游是另一台 Deskport, token 即身份 (对方守卫放行,
        # 免去第二层网页登录 —— 否则两台的同名 session Cookie 会互相顶掉)
        headers['X-Deskport-Token'] = entry['token']

    # 请求体: 完整读入再转发 (bytes 语义明确, 不依赖流读取 —— 流式转发在
    # 部分环境下会卡死在读 body, 整条 POST 挂住; 远窗场景 body 都是表单/JSON
    # 级小体, 内存代价可忽略)。响应侧仍保持流式 (SSE/大文件下载不受影响)。
    data = request.get_data(cache=True) if request.method in BODY_METHODS else None
    try:
        upstreamResp = _session().request(
            request.method, target, headers=headers, data=data,
            stream=True, allow_redirects=False, timeout=(5, None))
    except requests.RequestException as exc:
        _log.warning('upstream %s unreachable: %s', upstream, exc)
        return Response('{"error": "upstream unreachable"}',
                        status=502, mimetype='application/json')

    respHeaders = [(k, v) for k, v in upstreamResp.headers.items()
                   if k.lower() not in HOP_BY_HOP
                   and k.lower() != 'content-type']

    def generate():
        try:
            for chunk in upstreamResp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            upstreamResp.close()

    resp = Response(generate(), status=upstreamResp.status_code,
                    content_type=upstreamResp.headers.get('Content-Type'))
    for key, value in respHeaders:
        resp.headers.add(key, value)
    return resp


def _resolveTargetPath(prefix, mode, path):
    # namespace 模式: /_x/* 原样透传; 其余 (页面路径) 剥前缀
    if mode == 'namespace':
        if path == DEFAULT_NS_PREFIX or path.startswith(DEFAULT_NS_PREFIX + '/'):
            return path
        return path[len(prefix):] or '/'
    return path


def register(app, apps):
    """装配外部应用挂载。apps: config.getExternalApps() 的列表。

    冲突 (prefix 与已有路由或彼此重合) 直接抛 ValueError, 拒绝装配。
    """
    global _apps
    seen = {}
    for item in apps:
        prefix = item['prefix']
        validatePrefix(prefix, app)
        if prefix in seen:
            raise ValueError('externalApp prefix %s 重复 (与 %s), 拒绝装配'
                             % (prefix, seen[prefix]))
        seen[prefix] = item['id']

    with _appsLock:
        _apps = [dict(item) for item in apps]

    for item in apps:
        proxy = makeProxy(item['prefix'], item['upstream'], item['mode'])
        endpoint = proxy.__name__
        # strict_slashes=False: /<prefix> 与 /<prefix>/ 都命中（守卫 302 的 next 常带尾斜杠）
        app.add_url_rule(item['prefix'], endpoint=endpoint, view_func=proxy,
                         methods=list(FORWARD_METHODS), strict_slashes=False)
        app.add_url_rule(item['prefix'] + '/<path:filename>', endpoint=endpoint + '_path',
                         view_func=proxy, methods=list(FORWARD_METHODS))
        if item['mode'] == 'namespace':
            app.add_url_rule(DEFAULT_NS_PREFIX, endpoint=endpoint + '_ns',
                             view_func=proxy, methods=list(FORWARD_METHODS))
            app.add_url_rule(DEFAULT_NS_PREFIX + '/<path:filename>',
                             endpoint=endpoint + '_ns_path', view_func=proxy,
                             methods=list(FORWARD_METHODS))
        _log.info('external app %s mounted at %s (%s) -> %s',
                  item['id'], item['prefix'], item['mode'], item['upstream'])

    # 联邦广播: 本机的挂载清单对持有本机 token 的联邦对端可见
    # (守卫罩 /api/*: 会话或 X-Deskport-Token 放行)。远端接不接由远端的
    # remotes[].apps 白名单决定 —— 提供方不过滤, 消费方说了算。
    def listExternalApps():
        from flask import jsonify
        with _appsLock:
            return jsonify({'apps': [dict(item) for item in _apps]})

    if '/api/external-apps' not in [r.rule for r in app.url_map.iter_rules()]:
        app.add_url_rule('/api/external-apps', endpoint='listExternalApps',
                         view_func=listExternalApps, methods=['GET'])
