// 反馈面板：右下角悬浮按钮 + 半屏浮层（截图标注 / 一句话 / 联系方式）+「我的反馈」。
// 只在浮层里操作，绝不路由跳转（§3.2）；截图与标注就是最终上传的内容（所见即所传）。
window.WPFeedbackPanel = (function () {
    var STATUS_TEXT = {
        submitted: '已提交', acknowledged: '已收到', processing: '处理中',
        resolved: '已解决', rejected: '未采纳', user_confirmed: '已确认'
    };
    var root = null;
    var panel = null;
    var maskEl = null;
    var canvas = null;
    var retryBtn = null;
    var messageEl = null;
    var contactEl = null;
    var hintEl = null;
    var submitBtn = null;
    var shotTip = null;
    var mineList = null;
    var tabs = {};
    var baseImage = null;
    var ops = [];
    var tool = 'pen';
    var drawing = null;
    var onSubmit = null;
    var busy = false;

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) { node.className = cls; }
        if (text) { node.textContent = text; }
        return node;
    }

    // 底部有工具条（文件多选、收件箱操作栏、终端控制键、控制台 FAB）时把按钮抬上去，别压住别人的按钮
    function placeFab() {
        // 控制键条的容器是 #control-keys（旧选择器 .term-vkeys 根本不存在，等于没抬过——
        // 手机反馈「反馈按钮挡住了到底部的按钮」就是它压在控制键条上）
        var raise = document.querySelector('.bottombar:not([hidden]), .inbox-actionbar:not([hidden]), #control-keys:not(.hidden), .ctl-fab');
        // 壳（/app）里终端与 SSH 是 iframe：控制键条在 iframe 里面，外层选择器看不见它，
        // 但浮按钮画在壳文档上、永远盖在 iframe 之上，所以按"当前视图"判断
        var frame = document.querySelector('iframe');
        var src = frame ? (frame.getAttribute('src') || '') : '';
        if (/\/terminal|\/ssh/.test(src)) { raise = raise || document.body; }
        document.body.classList.toggle('fb-raise', !!raise);
        // 全屏浮层（移动端 SFTP 面板铺满整屏）打开时直接收起：那时抬起来也会压在
        // 浮层底部的操作行（新建/上传/删除）上，用户报过"挡住到底部的按钮"
        var covered = false;
        ['sftp-panel'].forEach(function (id) {
            var node = document.getElementById(id);
            if (node && node.offsetParent !== null && getComputedStyle(node).display !== 'none') {
                covered = true;
            }
        });
        if (document.querySelector('.more-sheet:not([hidden])')) { covered = true; }
        document.body.classList.toggle('fb-hide', covered);
    }

    function buildFab() {
        var fab = el('button', 'fb-fab');
        fab.type = 'button';
        fab.title = '反馈（Ctrl+Shift+F）';
        fab.setAttribute('aria-label', '反馈');
        fab.appendChild(el('span', 'fb-fab-dot'));
        fab.appendChild(el('span', 'fb-fab-text', '反馈'));
        fab.addEventListener('click', function () { open('write'); });
        return fab;
    }

    function toolButton(id, label, name) {
        var btn = el('button', 'fb-tool', label);
        btn.type = 'button';
        btn.setAttribute('data-tool', id);
        btn.addEventListener('click', function () {
            tool = id;
            Array.prototype.forEach.call(panel.querySelectorAll('.fb-tool'), function (node) {
                node.classList.toggle('is-on', node.getAttribute('data-tool') === id);
            });
        });
        if (name === tool) { btn.classList.add('is-on'); }
        return btn;
    }

    function buildPanel() {
        maskEl = el('div', 'fb-mask');
        maskEl.hidden = true;
        maskEl.addEventListener('pointerdown', function (ev) {
            if (ev.target === maskEl) { close(); }
        });
        panel = el('div', 'fb-panel');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.setAttribute('aria-label', '反馈');

        var head = el('div', 'fb-head');
        var titleWrap = el('div', 'fb-tabs');
        tabs.write = el('button', 'fb-tab is-on', '写反馈');
        tabs.write.type = 'button';
        tabs.write.addEventListener('click', function () { switchTab('write'); });
        tabs.mine = el('button', 'fb-tab', '我的反馈');
        tabs.mine.type = 'button';
        tabs.mine.addEventListener('click', function () { switchTab('mine'); });
        titleWrap.appendChild(tabs.write);
        titleWrap.appendChild(tabs.mine);
        var closeBtn = el('button', 'fb-close', '×');
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', '关闭');
        closeBtn.addEventListener('click', close);
        head.appendChild(titleWrap);
        head.appendChild(closeBtn);

        var write = el('div', 'fb-body fb-write');
        var shot = el('div', 'fb-shot');
        canvas = document.createElement('canvas');
        canvas.className = 'fb-canvas';
        shotTip = el('div', 'fb-shot-tip', '正在截取当前画面…');
        shot.appendChild(canvas);
        shot.appendChild(shotTip);
        retryBtn = el('button', 'fb-retry', '重新截图');
        retryBtn.type = 'button';
        retryBtn.hidden = true;
        retryBtn.addEventListener('click', function () {
            window.WPFeedbackCapture.forgetStream();
            retryBtn.hidden = true;
            captureShot();
        });
        shot.appendChild(retryBtn);

        var tools = el('div', 'fb-tools');
        tools.appendChild(toolButton('pen', '画笔', 'pen'));
        tools.appendChild(toolButton('rect', '框选', 'rect'));
        tools.appendChild(toolButton('black', '打码', 'black'));
        var undo = el('button', 'fb-tool', '撤销');
        undo.type = 'button';
        undo.addEventListener('click', function () { ops.pop(); paint(); });
        var clear = el('button', 'fb-tool', '清空标注');
        clear.type = 'button';
        clear.addEventListener('click', function () { ops = []; paint(); });
        tools.appendChild(undo);
        tools.appendChild(clear);
        shot.appendChild(tools);

        messageEl = el('textarea', 'fb-message');
        messageEl.maxLength = 500;
        messageEl.placeholder = '哪里不满意？你想要什么？';
        messageEl.addEventListener('focus', function () {
            // 手机键盘会把下半屏顶掉：等视口稳定后把输入框滚到中间
            setTimeout(function () {
                var vv = window.visualViewport;
                var shrunk = vv && vv.height < window.innerHeight * 0.75;
                if (shrunk || window.innerWidth <= 640) {
                    messageEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
                }
            }, 260);
        });

        var contactWrap = document.createElement('details');
        contactWrap.className = 'fb-contact';
        contactWrap.appendChild(el('summary', '', '联系方式（可选）'));
        contactEl = el('input', 'fb-contact-input');
        contactEl.type = 'text';
        contactEl.placeholder = '邮箱 / 微信 / 其它，方便回复你';
        contactWrap.appendChild(contactEl);

        hintEl = el('div', 'fb-hint', '截图与页面信息会自动附带；密码框内容不会采集，手机号/证件号/邮箱会自动打码。');
        submitBtn = el('button', 'fb-submit', '提交');
        submitBtn.type = 'submit';
        submitBtn.addEventListener('click', doSubmit);
        var foot = el('div', 'fb-foot');
        foot.appendChild(hintEl);
        foot.appendChild(submitBtn);

        write.appendChild(shot);
        write.appendChild(messageEl);
        write.appendChild(contactWrap);
        write.appendChild(foot);

        var mine = el('div', 'fb-body fb-mine');
        mine.hidden = true;
        mineList = el('div', 'fb-mine-list');
        mine.appendChild(mineList);

        panel.appendChild(head);
        panel.appendChild(write);
        panel.appendChild(mine);
        maskEl.appendChild(panel);
        root.appendChild(maskEl);
    }

    // ---- 画布标注：底图 + 操作序列重绘（撤销就是弹掉最后一步）----
    function paint() {
        if (!canvas) { return; }
        var ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (baseImage) { ctx.drawImage(baseImage, 0, 0); }
        for (var i = 0; i < ops.length; i++) {
            var op = ops[i];
            ctx.save();
            ctx.strokeStyle = op.type === 'black' ? '#111827' : '#DC2626';
            ctx.fillStyle = '#111827';
            ctx.lineWidth = Math.max(2, Math.round(canvas.width / 400));
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            if (op.type === 'pen' && op.points.length > 1) {
                ctx.beginPath();
                ctx.moveTo(op.points[0][0], op.points[0][1]);
                for (var j = 1; j < op.points.length; j++) {
                    ctx.lineTo(op.points[j][0], op.points[j][1]);
                }
                ctx.stroke();
            } else if (op.type === 'rect' || op.type === 'black') {
                var x = Math.min(op.x1, op.x2);
                var y = Math.min(op.y1, op.y2);
                var w = Math.abs(op.x2 - op.x1);
                var h = Math.abs(op.y2 - op.y1);
                if (op.type === 'black') { ctx.fillRect(x, y, w, h); } else { ctx.strokeRect(x, y, w, h); }
            }
            ctx.restore();
        }
    }

    function pointOf(ev) {
        var rect = canvas.getBoundingClientRect();
        var sx = canvas.width / (rect.width || 1);
        var sy = canvas.height / (rect.height || 1);
        return [Math.round((ev.clientX - rect.left) * sx), Math.round((ev.clientY - rect.top) * sy)];
    }

    function onCanvasDown(ev) {
        if (!baseImage) { return; }
        ev.preventDefault();
        var point = pointOf(ev);
        if (tool === 'pen') {
            drawing = { type: 'pen', points: [point] };
        } else {
            drawing = { type: tool, x1: point[0], y1: point[1], x2: point[0], y2: point[1] };
        }
        ops.push(drawing);
        paint();
        try {
            if (canvas.setPointerCapture) { canvas.setPointerCapture(ev.pointerId); }
        } catch (e) { /* 合成事件没有真实 pointerId，忽略 */ }
    }

    function onCanvasMove(ev) {
        if (!drawing) { return; }
        var point = pointOf(ev);
        if (drawing.type === 'pen') {
            drawing.points.push(point);
        } else {
            drawing.x2 = point[0];
            drawing.y2 = point[1];
        }
        paint();
    }

    function onCanvasUp() {
        drawing = null;
    }

    // ---- 截图：打开面板时抓一帧（§3.2）----
    function captureFailed(capture) {
        shotTip.hidden = false;
        shotTip.textContent = capture.canGrabScreen()
            ? '这次没拿到截图（可能拒绝了共享），可以重试，或只提交文字。'
            : '这个浏览器不支持截屏，你仍可以只提交文字。';
        if (retryBtn) { retryBtn.hidden = !capture.canGrabScreen(); }
    }

    function captureShot() {
        var capture = window.WPFeedbackCapture;
        shotTip.hidden = false;
        shotTip.textContent = '正在截取当前画面…';
        baseImage = null;
        ops = [];
        canvas.hidden = true;
        if (retryBtn) { retryBtn.hidden = true; }
        var settled = false;
        // 兜底：共享授权/播放偶尔会一直 pending（尤其手机上），4 秒后按失败处理，别让面板卡在“正在截取”
        var guard = setTimeout(function () {
            if (settled) { return; }
            settled = true;
            captureFailed(capture);
        }, 4000);
        capture.ensureStream().then(function (ok) {
            if (settled) { return; }
            settled = true;
            clearTimeout(guard);
            var grabbed = ok ? capture.grabCanvas() : null;
            if (!grabbed) { return captureFailed(capture); }
            baseImage = grabbed;
            canvas.width = grabbed.width;
            canvas.height = grabbed.height;
            canvas.hidden = false;
            shotTip.hidden = true;
            paint();
        }).catch(function () {
            if (settled) { return; }
            settled = true;
            clearTimeout(guard);
            captureFailed(capture);
        });
    }

    // ---- 我的反馈（§7.2 必做）----
    function statusBadge(status) {
        var span = el('span', 'fb-badge', STATUS_TEXT[status] || status);
        span.setAttribute('data-status', status || 'submitted');
        return span;
    }

    function renderMine(items) {
        mineList.textContent = '';
        if (!items.length) {
            mineList.appendChild(el('div', 'fb-empty', '还没有提交过反馈。'));
            return;
        }
        items.forEach(function (item) {
            var card = el('div', 'fb-card');
            var top = el('div', 'fb-card-top');
            top.appendChild(statusBadge(item.status));
            top.appendChild(el('span', 'fb-card-route', item.route || ''));
            top.appendChild(el('span', 'fb-card-time', (item.submitted_at || '').slice(0, 16).replace('T', ' ')));
            card.appendChild(top);
            card.appendChild(el('div', 'fb-card-msg', item.message || '（无文字）'));
            if (item.status_note) {
                card.appendChild(el('div', 'fb-card-note', '处理说明：' + item.status_note));
            }
            (item.attachments || []).forEach(function (att) {
                var img = document.createElement('img');
                img.className = 'fb-card-shot';
                img.loading = 'lazy';
                img.src = att.url;
                img.alt = '截图';
                card.appendChild(img);
            });
            mineList.appendChild(card);
        });
    }

    function loadMine() {
        mineList.textContent = '';
        mineList.appendChild(el('div', 'fb-empty', '读取中…'));
        fetch('/api/feedback?size=20', { credentials: 'same-origin' })
            .then(function (res) { return res.ok ? res.json() : Promise.reject(res.status); })
            .then(function (data) { renderMine((data && data.items) || []); })
            .catch(function () { renderMine([]); });
    }

    function switchTab(name) {
        tabs.write.classList.toggle('is-on', name === 'write');
        tabs.mine.classList.toggle('is-on', name === 'mine');
        panel.querySelector('.fb-write').hidden = name !== 'write';
        panel.querySelector('.fb-mine').hidden = name !== 'mine';
        if (name === 'mine') { loadMine(); }
    }

    function open(tab) {
        if (!root) { return; }
        placeFab();
        maskEl.hidden = false;
        document.body.classList.add('fb-open');
        switchTab(tab || 'write');
        if (!tab || tab === 'write') {
            captureShot();
            setTimeout(function () { messageEl.focus(); }, 60);
        }
    }

    function close() {
        if (!maskEl) { return; }
        maskEl.hidden = true;
        document.body.classList.remove('fb-open');
        drawing = null;
    }

    function toast(text) {
        var node = el('div', 'fb-toast', text);
        document.body.appendChild(node);
        setTimeout(function () { node.classList.add('is-on'); }, 10);
        setTimeout(function () {
            node.classList.remove('is-on');
            setTimeout(function () { node.remove(); }, 300);
        }, 2600);
    }

    function setBusy(value, label) {
        busy = value;
        submitBtn.disabled = value;
        submitBtn.textContent = value ? (label || '提交中…') : '提交';
    }

    function doSubmit() {
        if (busy) { return; }
        var text = (messageEl.value || '').trim();
        if (!text) {
            setHint('写一句话就行：哪里不满意、你想要什么。', true);
            messageEl.focus();
            return;
        }
        setBusy(true);
        var blob = null;
        var capture = window.WPFeedbackCapture;
        if (baseImage && canvas.width) {
            capture.compress(canvas, 1024 * 1024).then(function (out) {
                blob = out;
                onSubmit({ message: text, contact: contactEl.value.trim(), blob: blob,
                           hasAnnotations: ops.length > 0 });
            });
            return;
        }
        onSubmit({ message: text, contact: contactEl.value.trim(), blob: null,
                   hasAnnotations: false, captureNote: shotTip && shotTip.textContent || '' });
    }

    function setHint(text, isError) {
        hintEl.textContent = text;
        hintEl.classList.toggle('is-error', !!isError);
    }

    function init(handler) {
        onSubmit = handler;
        root = el('div', 'fb-root');
        root.appendChild(buildFab());
        buildPanel();
        document.body.appendChild(root);
        // 点一下就可能切换出底部工具条（多选模式等）：跟着重排，不做轮询
        document.addEventListener('pointerdown', function () { setTimeout(placeFab, 0); }, true);
        document.addEventListener('click', function () { setTimeout(placeFab, 0); }, true);
        window.addEventListener('resize', placeFab);
        window.addEventListener('orientationchange', placeFab);
        setTimeout(placeFab, 400);
        canvas.addEventListener('pointerdown', onCanvasDown);
        canvas.addEventListener('pointermove', onCanvasMove);
        canvas.addEventListener('pointerup', onCanvasUp);
        canvas.addEventListener('pointercancel', onCanvasUp);
        document.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape' && maskEl && !maskEl.hidden) { close(); }
        });
    }

    return {
        init: init,
        open: open,
        close: close,
        toast: toast,
        setBusy: setBusy,
        setHint: setHint,
        reset: function () {
            messageEl.value = '';
            contactEl.value = '';
            ops = [];
            baseImage = null;
            canvas.hidden = true;
            setBusy(false);
            setHint('截图与页面信息会自动附带；密码框内容不会采集，手机号/证件号/邮箱会自动打码。');
        }
    };
})();