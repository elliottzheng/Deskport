// 反馈捕获层：用户点反馈的瞬间，自动把「我正在看什么」抓下来。
// 契约见 docs/feedback.md §5；这里只产出信封片段，不碰 UI（UI 在 feedback-panel.js）。
//
// 三件事：
//   1. 现场：路由 / 页面标识 / 组件标识 / 视口 / 语言
//   2. 轨迹与技术现场：最近 50 步点击与聚焦、路由、慢或失败的请求、console 报错
//   3. 脱敏：密码框内容永不采集；文本里的手机号/身份证/银行卡/邮箱打码
window.WPFeedbackCapture = (function () {
    var MAX_TRACE = 50;
    var MAX_ERRORS = 10;
    var t0 = window.performance && performance.now ? performance.now() : Date.now();
    var trace = [];
    var consoleErrors = [];
    var failedRequests = [];
    var clicks = {};          // callId 之类不需要；这里只留最后一个点击的组件
    var lastTarget = '';
    var stream = null;        // getDisplayMedia 的流：一次授权，本次会话复用
    var streamDenied = false;
    var appVersion = 'dev';
    var videoEl = null;

    function nowMs() {
        return Math.round((window.performance && performance.now ? performance.now() : Date.now()) - t0);
    }

    function clip(text, max) {
        var s = String(text == null ? '' : text);
        return s.length > max ? s.slice(0, max) : s;
    }

    // ---- 脱敏（§4 规则 1、2；上传前执行，所见即所传）----
    var MASK_RULES = [
        [/(1[3-9]\d)\d{4}(\d{4})/g, '$1****$2'],                       // 手机号
        [/\b(\d{6})\d{8}(\d{3}[\dXx])\b/g, '$1********$2'],            // 身份证
        [/\b(\d{4})\d{8,11}(\d{4})\b/g, '$1********$2'],               // 银行卡
        [/([\w.+-]{1,2})[\w.+-]*(@[\w.-]+\.\w+)/g, '$1***$2']          // 邮箱
    ];

    function maskText(text) {
        var s = String(text == null ? '' : text);
        for (var i = 0; i < MASK_RULES.length; i++) {
            s = s.replace(MASK_RULES[i][0], MASK_RULES[i][1]);
        }
        return s;
    }

    // ---- 组件引用：`组件名#实例`（尽力精确到控件，§5.2）----
    function refOf(el) {
        if (!el || el.nodeType !== 1) { return ''; }
        var tag = el.tagName.toLowerCase();
        if (el.id) { return tag + '#' + el.id; }
        var name = el.getAttribute && (el.getAttribute('data-ref') || el.getAttribute('name'));
        if (name) { return tag + '#' + clip(name, 40); }
        var cls = (el.className && typeof el.className === 'string')
            ? el.className.trim().split(/\s+/)[0] : '';
        return cls ? tag + '.' + clip(cls, 40) : tag;
    }

    var PAGE_NAMES = {
        '/': 'DashboardPage', '/app': 'DashboardPage', '/tasks': 'TasksPage',
        '/files': 'FilesPage', '/settings': 'SettingsPage', '/screen': 'ScreenPage',
        '/command': 'CommandPage', '/ssh': 'SshPage', '/terminal': 'TerminalPage',
        '/inbox': 'InboxPage'
    };

    function pageId() {
        var body = document.body;
        if (body && body.getAttribute('data-page')) { return body.getAttribute('data-page'); }
        return PAGE_NAMES[location.pathname] || clip(location.pathname, 60) || 'UnknownPage';
    }

    function routeOf() {
        return location.pathname + location.search;
    }

    function push(action, target, extra) {
        var step = { t: nowMs(), action: action, target: clip(target, 160) };
        if (extra) {
            for (var k in extra) {
                if (Object.prototype.hasOwnProperty.call(extra, k)) { step[k] = extra[k]; }
            }
        }
        trace.push(step);
        if (trace.length > MAX_TRACE) { trace.shift(); }
    }

    // ---- 行为轨迹：点击与聚焦（不记录输入值，§4 规则 4）----
    function onPointerDown(ev) {
        var el = ev.target;
        if (!el || el.closest && el.closest('.fb-root')) { return; }   // 反馈自己的交互不记账
        lastTarget = refOf(el);
        push('click', lastTarget, { ok: true });
    }

    function onFocusIn(ev) {
        var el = ev.target;
        if (!el || !el.tagName) { return; }
        var tag = el.tagName.toLowerCase();
        if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') { return; }
        // 只记「聚焦了哪个输入框」，绝不含值；密码类连标识都不细记
        var isSecret = (el.type === 'password')
            || /password|passcode|otp|secret|token/i.test(el.name || el.id || '');
        push('input_focus', isSecret ? 'secret-input' : refOf(el));
    }

    // ---- 技术现场：慢/失败请求 ----
    function recordHttp(method, url, status, durationMs) {
        var slow = durationMs >= 1500;
        var failed = status === 0 || status >= 400;
        if (!slow && !failed) { return; }
        var target = method + ' ' + clip(url, 120);
        push('http', target, { status: status, duration_ms: Math.round(durationMs) });
        if (failed) {
            failedRequests.push({ url: clip(url, 300), status: status, method: method,
                                  duration_ms: Math.round(durationMs) });
            if (failedRequests.length > MAX_ERRORS) { failedRequests.shift(); }
        }
    }

    function hookFetch() {
        if (!window.fetch || window.fetch.__wpFeedback) { return; }
        var original = window.fetch;
        var wrapped = function (input, init) {
            var started = Date.now();
            var url = typeof input === 'string' ? input : (input && input.url) || '';
            var method = (init && init.method) || (input && input.method) || 'GET';
            return original.apply(this, arguments).then(function (res) {
                recordHttp(method.toUpperCase(), url, res.status, Date.now() - started);
                return res;
            }, function (err) {
                recordHttp(method.toUpperCase(), url, 0, Date.now() - started);
                throw err;
            });
        };
        wrapped.__wpFeedback = true;
        window.fetch = wrapped;
    }

    function hookXhr() {
        if (!window.XMLHttpRequest || XMLHttpRequest.prototype.__wpFeedback) { return; }
        var open = XMLHttpRequest.prototype.open;
        var send = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (method, url) {
            this.__wp = { method: method, url: url, started: 0 };
            return open.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function () {
            var xhr = this;
            if (xhr.__wp) { xhr.__wp.started = Date.now(); }
            xhr.addEventListener('loadend', function () {
                if (!xhr.__wp) { return; }
                recordHttp(String(xhr.__wp.method || 'GET').toUpperCase(), xhr.__wp.url,
                    xhr.status, Date.now() - xhr.__wp.started);
            });
            return send.apply(this, arguments);
        };
        XMLHttpRequest.prototype.__wpFeedback = true;
    }

    function errText(err) {
        if (!err) { return ''; }
        if (typeof err === 'string') { return clip(err, 300); }
        var head = err.name ? err.name + ': ' : '';
        return clip(head + (err.message || String(err)), 300);
    }

    function addConsoleError(text) {
        var line = clip(maskText(text), 300);
        if (!line || consoleErrors.indexOf(line) >= 0) { return; }
        consoleErrors.push(line);
        if (consoleErrors.length > MAX_ERRORS) { consoleErrors.shift(); }
    }

    function hookErrors() {
        var onError = window.onerror;
        window.onerror = function (msg, src, line, col, err) {
            addConsoleError(errText(err) || (msg + ' @' + line + ':' + col));
            if (typeof onError === 'function') { return onError.apply(this, arguments); }
            return false;
        };
        window.addEventListener('unhandledrejection', function (ev) {
            addConsoleError('unhandledrejection: ' + errText(ev.reason));
        });
        if (window.console && console.error) {
            var originalError = console.error;
            console.error = function () {
                try {
                    addConsoleError(Array.prototype.map.call(arguments, errText).join(' '));
                } catch (e) { /* 忽略：捕获层不能反过来弄坏页面 */ }
                return originalError.apply(console, arguments);
            };
        }
    }

    // ---- 截图：getDisplayMedia 一次授权，之后随时取帧；拿不到就如实标记 ----
    function canGrabScreen() {
        return !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
    }

    function ensureStream() {
        if (stream) { return Promise.resolve(true); }
        if (!canGrabScreen() || streamDenied) { return Promise.resolve(false); }
        return navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: false })
            .then(function (got) {
                stream = got;
                videoEl = document.createElement('video');
                videoEl.muted = true;
                videoEl.playsInline = true;
                videoEl.srcObject = got;
                return videoEl.play().then(function () {
                    return new Promise(function (resolve) {
                        setTimeout(function () { resolve(true); }, 180);   // 等首帧
                    });
                });
            })
            .catch(function () {
                streamDenied = true;      // 用户拒绝过就这次会话不再骚扰
                return false;
            });
    }

    function grabCanvas() {
        if (!stream || !videoEl || !videoEl.videoWidth) { return null; }
        var canvas = document.createElement('canvas');
        canvas.width = videoEl.videoWidth;
        canvas.height = videoEl.videoHeight;
        canvas.getContext('2d').drawImage(videoEl, 0, 0);
        return canvas;
    }

    function toBlob(canvas, quality) {
        return new Promise(function (resolve) {
            if (canvas.toBlob) {
                canvas.toBlob(function (blob) { resolve(blob); }, 'image/jpeg', quality);
            } else {
                resolve(null);
            }
        });
    }

    function shrink(canvas, scale) {
        var out = document.createElement('canvas');
        out.width = Math.max(1, Math.round(canvas.width * scale));
        out.height = Math.max(1, Math.round(canvas.height * scale));
        out.getContext('2d').drawImage(canvas, 0, 0, out.width, out.height);
        return out;
    }

    // 压到 1MB 以内（§8.2）；实在压不下去返回 null，由上层决定怎么标注
    function compress(canvas, limit) {
        var max = limit || 1024 * 1024;
        var quality = 0.92;
        var current = canvas;
        var round = 0;
        function attempt() {
            return toBlob(current, quality).then(function (blob) {
                if (blob && blob.size <= max) { return blob; }
                round += 1;
                if (round > 8) { return null; }
                if (quality > 0.6) { quality -= 0.12; } else { current = shrink(current, 0.8); }
                return attempt();
            });
        }
        return attempt();
    }

    // ---- 信封（§5）----
    function context() {
        return {
            route: routeOf(),
            page_id: pageId(),
            component_ref: lastTarget || '',
            viewport: { width: window.innerWidth || 0, height: window.innerHeight || 0 },
            locale: navigator.language || ''
        };
    }

    function envelope(fields) {
        fields = fields || {};
        return {
            schema_version: '1.0',
            feedback_id: fields.feedback_id || '',
            idempotency_key: fields.idempotency_key || '',
            submitted_at: new Date().toISOString(),
            user: fields.user || { id: '', role: '', anonymous: false },
            client: {
                platform: 'web',
                app_version: fields.app_version || appVersion,
                user_agent: navigator.userAgent
            },
            context: context(),
            trace: trace.slice(-MAX_TRACE),
            errors: { console: consoleErrors.slice(), failed_requests: failedRequests.slice() },
            attachments: fields.attachments || [],
            message: maskText(fields.message || ''),
            contact: fields.contact || '',
            capture: fields.capture || {}
        };
    }

    function setAppVersion(value) {
        appVersion = String(value || 'dev').slice(0, 40);
    }

    function init() {
        document.addEventListener('pointerdown', onPointerDown, true);
        document.addEventListener('focusin', onFocusIn, true);
        hookFetch();
        hookXhr();
        hookErrors();
        push('route', routeOf());
    }

    return {
        init: init,
        setAppVersion: setAppVersion,
        envelope: envelope,
        context: context,
        maskText: maskText,
        pageId: pageId,
        refOf: refOf,
        ensureStream: ensureStream,
        canGrabScreen: canGrabScreen,
        grabCanvas: grabCanvas,
        compress: compress,
        streamDenied: function () { return streamDenied; }
    };
})();