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
    var appVersion = 'dev';

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

    // ---- 截图：DOM 静默快照一帧当前视口（FB-5；零授权，http 非安全上下文可用）----
    // 壳顶层 document 整页快照；当前可见 iframe 同源时再截它的内容合成上去，
    // 取不到就只截壳（partial 标记告知上层）。
    function snapFrame(frame) {
        var doc = null, win = null;
        try {
            doc = frame.contentDocument;
            win = frame.contentWindow;
        } catch (e) { return Promise.reject(e); }
        if (!doc || !doc.body) { return Promise.reject(new Error('frame document unavailable')); }
        var rect = frame.getBoundingClientRect();
        var width = Math.max(1, Math.round(rect.width));
        var height = Math.max(1, Math.round(rect.height));
        var snap = window.WPFeedbackCapture && window.WPFeedbackCapture.snap;
        if (!snap || !snap.snapDocument) { return Promise.reject(new Error('snapshot module missing')); }
        return snap.snapDocument(doc, {
            width: width, height: height,
            scrollX: win.scrollX || 0, scrollY: win.scrollY || 0
        });
    }

    // 壳快照 + iframe 内容合成到同一张画布上（坐标即视口坐标，scale=1）
    function compositeShot(shell, frame, inner) {
        if (!inner || !inner.shot) { return; }
        var rect = frame.getBoundingClientRect();
        var ctx = shell.getContext('2d');
        ctx.drawImage(inner.shot, rect.left, rect.top, rect.width, rect.height);
    }

    // 空白检测：整张图几乎纯色 = 没画出来（foreignObject 被拦、终端/投屏类内容），
    // 别把一张白图当截图传给用户。取不到像素（污染画布）时不当空白。
    function looksBlank(shot) {
        try {
            var ctx = shot.getContext('2d');
            var w = shot.width, h = shot.height;
            if (!w || !h || !ctx.getImageData) { return false; }
            var data = ctx.getImageData(0, 0, w, h).data;
            var step = Math.max(4, Math.floor(Math.min(w, h) / 80));
            var first = [data[0], data[1], data[2], data[3]];
            var total = 0, same = 0;
            for (var y = 0; y < h; y += step) {
                for (var x = 0; x < w; x += step) {
                    var i = (y * w + x) * 4;
                    total += 1;
                    if (Math.abs(data[i] - first[0]) + Math.abs(data[i + 1] - first[1])
                        + Math.abs(data[i + 2] - first[2]) + Math.abs(data[i + 3] - first[3]) < 12) {
                        same += 1;
                    }
                }
            }
            return total > 0 && same / total > 0.995;
        } catch (e) {
            return false;
        }
    }

    // 面板打开时调：静默快照当前视口 → {shot, partial}；失败/空白给 null（不阻断提交）
    function snapViewport() {
        var snap = window.WPFeedbackCapture && window.WPFeedbackCapture.snap;
        if (!snap || !snap.snapDocument) { return Promise.resolve(null); }
        var width = Math.max(1, window.innerWidth || document.documentElement.clientWidth || 0);
        var height = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 0);
        var frame = inFrame ? null : visibleFrame();
        var partial = false;
        var shellJob = snap.snapDocument(document, {
            width: width, height: height,
            scrollX: window.scrollX || 0, scrollY: window.scrollY || 0
        });
        var frameJob = frame
            ? snapFrame(frame).then(function (res) { return res; }, function () {
                partial = true;   // 跨源/取不到：只截壳，告知上层
                return null;
            })
            : Promise.resolve(null);
        return Promise.all([shellJob, frameJob]).then(function (res) {
            var shell = res[0], inner = res[1];
            if (inner && inner.degraded) { partial = true; }
            if (!shell || !shell.shot || looksBlank(shell.shot)) { return null; }
            if (frame && inner && inner.shot) { compositeShot(shell.shot, frame, inner); }
            return { shot: shell.shot, partial: partial };
        }).catch(function () {
            return null;
        });
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
            schema_version: '2',
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
        isFrame: function () { return inFrame; },
        compress: compress,
        snapViewport: snapViewport,
    };
})();