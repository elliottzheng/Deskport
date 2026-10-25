# -*- coding: utf-8 -*-
"""Web 管理平台 — 入口。端口 6789, 页面与 API 均受登录守卫保护。

`create_app()` 负责装配；模块级 `app` 仅为兼容既有入口与测试。
"""
import gzip
import html
import json
import logging
import mimetypes
import os
import sys
from urllib.parse import urlencode

from flask import (Blueprint, Flask, Response, abort, redirect, request,
                   send_from_directory)
from flask_sock import Sock

import auth
import config

from modules import meta as metaModule  # noqa: E402
from modules import system as systemModule  # noqa: E402
from modules import files as filesModule  # noqa: E402
from modules import command as commandModule  # noqa: E402
from modules import screen as screenModule  # noqa: E402
from modules import aihandoff as aiModule  # noqa: E402
from modules import control as controlModule  # noqa: E402
from modules import settings as settingsModule  # noqa: E402
from modules import ssh as sshModule  # noqa: E402
from modules import tasks as tasksModule  # noqa: E402
from modules import terminal as terminalModule  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')


def _setupLogging():
    """会话生命周期日志 (spawn/reap/exit) 落到 run.log, 便于事后定位。
    只接管本项目模块, 不碰 werkzeug 的访问日志, 避免重复行。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    for name in ('termcore', 'localterm', 'ssh', 'files', 'sessionproxy', 'control'):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


_setupLogging()

pages = Blueprint('pages', __name__)

# /app 是唯一壳入口；旧深链保留给 ?embed=1 (壳内 iframe) 与兼容跳转
PAGES = {
    '/app': 'app.html',
    '/tasks': 'tasks.html',
}

LEGACY_PAGES = {
    '/': ('index.html', None),
    '/terminal': ('terminal.html', {'view': 'session', 'kind': 'local'}),
    '/ssh': ('ssh.html', {'view': 'session', 'kind': 'ssh'}),
    '/files': ('files.html', {'view': 'files'}),
    '/screen': ('screen.html', {'view': 'screen'}),
    '/command': ('command.html', {'view': 'command'}),
    '/settings': ('settings.html', {'view': 'settings'}),
}


@pages.route('/inbox')
def inboxLocation():
    """收件箱已并入文件页, 作为虚拟位置; 统一回到壳内文件视图。"""
    return redirect('/app?view=files&loc=inbox', code=302)


def _makePageHandler(filename):
    def handler():
        # 页面里的 __BUILD__ 占位替换为当前构建号, 静态资源带上缓存戳
        path = os.path.join(FRONTEND_DIR, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                page = f.read().replace('__BUILD__', metaModule.BUILD)
        except OSError:
            return send_from_directory(FRONTEND_DIR, filename)
        brand = html.escape(config.getBrandName())
        page = page.replace('Deskport', brand)
        page = page.replace('<head>', '<head>\n<meta name="wp-brand" content="%s">' % brand, 1)
        resp = Response(page, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
    handler.__name__ = 'page_' + filename.replace('.', '_')
    return handler


def _makeLegacyHandler(filename, target):
    page = _makePageHandler(filename)

    def handler():
        # 壳内 iframe 直接取旧页面，绝不能再次重定向
        if request.args.get('embed') == '1':
            return page()
        if not target:
            return redirect('/app', code=302)
        params = request.args.to_dict(flat=True)
        params.pop('embed', None)
        params.update(target)
        return redirect('/app?' + urlencode(params), code=302)
    handler.__name__ = 'legacy_' + filename.replace('.', '_')
    return handler


@pages.route('/assets/<path:filename>')
def assets(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in ('.css', '.js', '.svg', '.json', '.webmanifest'):
        # 文本资源读入内存，交给 gzipText 压缩；send_from_directory 的
        # direct_passthrough 会绕过 after_request，导致 JS/CSS 无法 gzip
        path = os.path.join(FRONTEND_DIR, 'assets', filename)
        if not os.path.isfile(path):
            abort(404)
        mimetype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        with open(path, 'rb') as f:
            resp = Response(f.read(), mimetype=mimetype)
    else:
        resp = send_from_directory(os.path.join(FRONTEND_DIR, 'assets'), filename,
                                   conditional=True)
    if request.args.get('v'):
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    else:
        resp.headers['Cache-Control'] = 'no-cache'
    return resp


@pages.route('/manifest.webmanifest')
def manifest():
    """PWA 清单：让手机把 /app 装成独立应用(standalone)，名称跟随品牌设置。"""
    path = os.path.join(FRONTEND_DIR, 'manifest.webmanifest')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {'name': 'Deskport', 'short_name': 'Deskport', 'start_url': '/app',
                'display': 'standalone', 'icons': []}
    brand = config.getBrandName()
    data['name'] = brand
    data['short_name'] = brand[:12]
    resp = Response(json.dumps(data, ensure_ascii=False),
                    mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@pages.route('/sw.js')
def serviceWorker():
    """Service Worker 必须从根路径提供, 才能拿到 scope=/。"""
    resp = send_from_directory(FRONTEND_DIR, 'sw.js', mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


def gzipText(resp):
    """文本资源 gzip：Vue 158KB / xterm 284KB 原样传输在手机远控下很慢。
    跳过流式响应(MJPEG)、已编码、非文本与 <1KB 的响应。"""
    try:
        if resp.direct_passthrough or resp.is_streamed or resp.status_code != 200:
            return resp
        if resp.headers.get('Content-Encoding'):
            return resp
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return resp
        mimetype = (resp.mimetype or '').lower()
        if mimetype not in ('text/html', 'text/css', 'text/plain', 'text/javascript',
                            'application/javascript', 'application/json',
                            'application/manifest+json', 'image/svg+xml', 'text/xml'):
            return resp
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        resp.set_data(gzip.compress(data, 6))
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Vary'] = 'Accept-Encoding'
    except (RuntimeError, OSError, ValueError):
        pass
    return resp


# 显式注册表：蓝图模块与 register(app) 模块分开，新增模块必须在这里登记。
BLUEPRINT_MODULES = (filesModule, commandModule, screenModule, aiModule,
                     controlModule, tasksModule, settingsModule)
REGISTER_MODULES = (systemModule, metaModule, tasksModule)


def _registerModules(app, sock):
    for module in REGISTER_MODULES:
        module.register(app)
    for module in BLUEPRINT_MODULES:
        app.register_blueprint(module.blueprint)

    # 会话托管: 本机终端与远程 SSH 各自一个守护进程, 按配置分别代理
    sshModule.register(app, sock,
                       sessionRoutes=not bool(config.getSessiondUrl('ssh')))
    terminalModule.register(app, sock,
                            sessionRoutes=not bool(config.getSessiondUrl('local')))
    if config.getSessiondUrl('local') or config.getSessiondUrl('ssh'):
        from modules import sessionproxy
        sessionproxy.register(app, sock)
        logging.getLogger('app').info(
            'sessiond local=%s ssh=%s', config.getSessiondUrl('local'),
            config.getSessiondUrl('ssh'))


def create_app():
    """装配一个独立 Flask 实例；测试可多次调用，互不共享路由状态。"""
    app = Flask(__name__)
    sock = Sock(app)

    auth.register(app)
    app.register_blueprint(pages)
    for route, filename in PAGES.items():
        app.add_url_rule(route, endpoint='page_' + filename.replace('.', '_'),
                         view_func=_makePageHandler(filename), methods=['GET'])
    for route, (filename, target) in LEGACY_PAGES.items():
        app.add_url_rule(route, endpoint='legacy_' + filename.replace('.', '_'),
                         view_func=_makeLegacyHandler(filename, target), methods=['GET'])
    app.after_request(gzipText)

    _registerModules(app, sock)
    sock.init_app(app)
    return app, sock


app, sock = create_app()


if __name__ == '__main__':
    port = config.getPort()
    # 生产化运行: 无 debug / 无 reloader (后端调研报告建议)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
