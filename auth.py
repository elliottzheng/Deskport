# -*- coding: utf-8 -*-
"""认证模块 — 会话登录守卫 + 登录限速"""
import hmac
import html
import threading
import time

from flask import request, session, redirect, jsonify

import config
from modules import events

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60
# 未登录时仍可访问: 登录页 + 登录页自身依赖的静态资源
EXEMPT_PREFIXES = (
    '/login',
    '/api/auth/status',   # 未认证也要可访问: 终端页掉线探测依赖它
    '/assets/css/tokens.css',
    '/assets/css/login.css',
    '/favicon',
)

loginAttempts = {}
loginAttemptsLock = threading.Lock()


def isAuthenticated():
    return bool(session.get('authenticated'))


def isRateLimited(clientIp):
    now = time.time()
    with loginAttemptsLock:
        attempts = [t for t in loginAttempts.get(clientIp, [])
                    if now - t < LOGIN_WINDOW_SECONDS]
        loginAttempts[clientIp] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def recordLoginFailure(clientIp):
    with loginAttemptsLock:
        loginAttempts.setdefault(clientIp, []).append(time.time())


def safeNextTarget(nextTarget):
    if nextTarget and nextTarget.startswith('/') and not nextTarget.startswith('//'):
        return nextTarget
    return '/'


def renderLoginPage(errorMessage, nextTarget):
    errorHtml = '<p class="login-error">%s</p>' % errorMessage if errorMessage else ''
    with open('frontend/login.html', 'r', encoding='utf-8') as f:
        page = f.read()
    from modules import meta as metaModule
    page = page.replace('Deskport', html.escape(config.getBrandName()))
    return (page.replace('<!--ERROR-->', errorHtml)
                .replace('<!--NEXT-->', nextTarget)
                .replace('__BUILD__', metaModule.BUILD))


def register(app):
    app.secret_key = hmac.new(
        (config.getPassword() or 'web-platform').encode(),
        b'session-key-v1', 'sha256').hexdigest()
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        nextTarget = safeNextTarget(request.args.get('next') or request.form.get('next'))
        if isAuthenticated():
            return redirect(nextTarget)

        errorMessage = None
        if request.method == 'POST':
            clientIp = request.remote_addr or 'unknown'
            if isRateLimited(clientIp):
                errorMessage = '尝试次数过多, 请稍后再试'
                events.log('auth.login', clientIp, 'locked', 'rate limited', clientIp)
            else:
                password = request.form.get('password') or ''
                expect = config.getPassword()
                if expect and hmac.compare_digest(expect.encode(), password.encode()):
                    session.clear()
                    session['authenticated'] = True
                    events.log('auth.login', clientIp, 'ok', '', clientIp)
                    return redirect(nextTarget)
                recordLoginFailure(clientIp)
                events.log('auth.login', clientIp, 'fail', 'bad password', clientIp)
                errorMessage = '密码错误'

        return renderLoginPage(errorMessage, nextTarget)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect('/login')

    @app.route('/api/auth/password', methods=['POST'])
    def changePassword():
        """单用户改密: 校验当前密码 → 写 config.json → 换 secret_key 并清会话。"""
        if not isAuthenticated():
            return jsonify({'error': 'unauthorized'}), 401
        data = request.get_json(force=True, silent=True) or {}
        current = data.get('current') or ''
        newPassword = data.get('new') or ''
        if len(newPassword) < 6:
            return jsonify({'error': '新密码至少 6 位'}), 400
        if len(newPassword) > 128:
            return jsonify({'error': '新密码过长'}), 400
        if not hmac.compare_digest(config.getPassword().encode(), current.encode()):
            return jsonify({'error': '当前密码不正确'}), 403
        config.setPassword(newPassword)
        # secret_key 由密码派生: 更新后所有旧会话立即失效
        app.secret_key = hmac.new(newPassword.encode(), b'session-key-v1',
                                  'sha256').hexdigest()
        session.clear()
        return jsonify({'ok': True, 'relogin': True})

    @app.route('/api/auth/status')
    def authStatus():
        """终端页 WS 掉线探测端点: 未认证返回 authenticated=false (需豁免守卫)"""
        return jsonify({'authenticated': isAuthenticated()})

    @app.before_request
    def authGuard():
        if isAuthenticated():
            return None
        path = request.path
        if any(path == p or path.startswith(p + '/') or path.startswith(p + '?')
               for p in EXEMPT_PREFIXES):
            return None
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return 'Unauthorized', 401
        if path.startswith('/api/'):
            return jsonify({'error': 'unauthorized'}), 401
        nextPath = path
        if request.query_string:
            nextPath += '?' + request.query_string.decode('utf-8', 'replace')
        return redirect('/login?next=' + nextPath)
