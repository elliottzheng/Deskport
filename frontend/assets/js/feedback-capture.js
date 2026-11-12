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
    var inFrame = window.self !== window.top;   // 壳（/app）里每页一个 iframe：只上报轨迹，别出按钮
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
        if (inFrame) { return postToTop(action, target, extra); }
        var step = { t: nowMs(), action: action, target: clip(target, 160) };
        if (extra) {
            for (var k in extra) {
                if (Object.prototype.hasOwnProperty.call(extra, k)) { step[k] = extra[k]; }
            }
        }
        trace.push(step);
        if (trace.length > MAX_TRACE) { trace.shift(); }
    }

    // ---- 壳里的 iframe → 顶层：轨迹汇总到同一个信封（跨文档只传结构，不传内容）----
    function postToTop(action, target, extra) {
        var parentWin = window.parent;
        if (!parentWin || parentWin === window) { return; }
        var step = { action: action, target: clip(target, 160) };
        if (extra) { step.extra = extra; }
        try { window.parent.postMessage({ __wpFeedbackTrace: 1, step: step }, location.origin); }
        catch (e) { /* 跨源 iframe：放弃，不打断页面 */ }
    }

    function onFrameTrace(ev) {
        if (ev.origin !== location.origin) { return; }
        var data = ev.data;
        if (!data || data.__wpFeedbackTrace !== 1 || !data.step) { return; }
        var step = data.step;
        var item = { t: nowMs(), action: step.action, target: clip(step.target, 160) };
        if (step.extra) {
            for (var k in step.extra) {
                if (Object.prototype.hasOwnProperty.call(step.extra, k)) { item[k] = step.extra[k]; }
            }
        }
        trace.push(item);
        if (trace.length > MAX_TRACE) { trace.shift(); }
        if (step.action === 'click') { lastTarget = item.target; }
    }

    // 壳里当前可见的那个 iframe（同源才读得到），用来报告真实页面
    function visibleFrame() {
        if (inFrame) { return null; }
        var frames = document.querySelectorAll('iframe');
        for (var i = 0; i < frames.length; i++) {
            var frame = frames[i];
            if (frame.hidden || frame.offsetParent === null || !frame.getAttribute('src')) { continue; }
            try {
                if (frame.contentWindow && frame.contentWindow.location.href) { return frame; }
            } catch (e) { return null; }
        }
        return null;
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

    // 等第一帧真的到（videoWidth 有值）再算成功：固定 sleep 在慢机器/手机上会偶发拿不到
    function waitFirstFrame(timeoutMs) {
        return new Promise(function (resolve) {
            var settled = false;
            var started = Date.now();
            function finish(ok) {
                if (!settled) { settled = true; resolve(ok); }
            }
            if (videoEl.requestVideoFrameCallback) {
                try { videoEl.requestVideoFrameCallback(function () { finish(true); }); } catch (e) { /* 老浏览器 */ }
            }
            (function poll() {
                if (settled) { return; }
                if (videoEl.videoWidth > 0) { return finish(true); }
                if (Date.now() - started > (timeoutMs || 2000)) { return finish(false); }
                setTimeout(poll, 60);
            })();
        });
    }

    function attachStream(got) {
        videoEl = document.createElement('video');
        videoEl.muted = true;
        videoEl.playsInline = true;
        videoEl.setAttribute('playsinline', '');
        videoEl.srcObject = got;
        var playing = videoEl.play ? videoEl.play() : Promise.resolve();
        // play() 在部分浏览器里会一直 pending（等手势），别让它把捕获卡死
        var playSettled = Promise.race([
            Promise.resolve(playing).catch(function () { return null; }),
            new Promise(function (resolve) { setTimeout(resolve, 800); })
        ]);
        return playSettled.then(function () { return waitFirstFrame(2000); });
    }

    function ensureStream() {
        if (!canGrabScreen()) { return Promise.resolve(false); }
        if (stream) { return Promise.resolve(videoEl && videoEl.videoWidth > 0); }
        if (streamDenied) { return Promise.resolve(false); }
        return navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: false })
            .then(function (got) {
                stream = got;
                return attachStream(got);
            })
            .catch(function () {
                streamDenied = true;      // 用户拒绝/不支持：这次会话不再骚扰
                return false;
            });
    }

    function forgetStream() {
        streamDenied = false;
        if (stream && stream.getTracks) {
            stream.getTracks().forEach(function (track) { track.stop(); });
        }
        stream = null;
        videoEl = null;
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
        var route = routeOf();
        var page = pageId();
        var frame = visibleFrame();          // 壳里报告的是 iframe 的真实页面
        if (frame) {
            try {
                var inner = frame.contentWindow.location;
                if (inner.pathname && inner.pathname !== '/') {
                    route = inner.pathname + inner.search;
                    page = PAGE_NAMES[inner.pathname] || page;
                }
            } catch (e) { /* 跨源：退回壳自己的路由 */ }
        }
        return {
            route: route,
            page_id: page,
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
        if (inFrame) { postToTop('route', routeOf()); return; }
        window.addEventListener('message', onFrameTrace);
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
        isFrame: function () { return inFrame; },
        grabCanvas: grabCanvas,
        compress: compress,
        streamDenied: function () { return streamDenied; },
        forgetStream: forgetStream,
    };
})();