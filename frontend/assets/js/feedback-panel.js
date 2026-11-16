// 反馈面板：右下角悬浮按钮 + 半屏浮层（截图标注 / 一句话 / 联系方式）+「我的反馈」。
// 只在浮层里操作，绝不路由跳转（§3.2）；截图与标注就是最终上传的内容（所见即所传）。
// 「我的反馈」页签在 feedback-mine.js（批次 F1）；本文件只管采集面板与画布装订。
// FB-5：截图改 DOM 静默快照（feedback-capture.snapViewport，零授权、http 可用），拿不到/
// 终端投屏类内容走手动上传（WPFeedback.uploadManualShot → attachments），
// 快照失败不阻断提交；桌面面板 1080px/90vh，画布滚轮缩放+拖拽平移（视图数学在
// feedback-canvas.js，纯函数有 node 单测）；FAB 桌面滚动隐藏、静置避开可交互元素。
window.WPFeedbackPanel = (function () {
    var root = null;
    var panel = null;
    var maskEl = null;
    var canvas = null;
    var viewEl = null;
    var shotTip = null;
    var messageEl = null;
    var contactEl = null;
    var hintEl = null;
    var submitBtn = null;
    var uploadInput = null;
    var zoomLabel = null;
    var tabs = {};
    var canvasApi = null;
    var baseImage = null;
    var ops = [];
    var tool = 'pen';
    var uploadedFileId = '';
    var onSubmit = null;
    var busy = false;
    var fabApi = null;

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) { node.className = cls; }
        if (text) { node.textContent = text; }
        return node;
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
            if (viewEl) { viewEl.classList.toggle('is-pan', id === 'move'); }
        });
        if (name === tool) { btn.classList.add('is-on'); }
        return btn;
    }

    function toolAction(label, title, fn) {
        var btn = el('button', 'fb-tool', label);
        btn.type = 'button';
        if (title) { btn.title = title; }
        btn.addEventListener('click', fn);
        return btn;
    }

    function buildPanel() {
        maskEl = el('div', 'fb-mask');
        maskEl.hidden = true;
        maskEl.addEventListener('pointerdown', function (ev) { if (ev.target === maskEl) { close(); } });
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
        viewEl = el('div', 'fb-shot-view');
        canvas = document.createElement('canvas');
        canvas.className = 'fb-canvas';
        canvas.hidden = true;
        viewEl.appendChild(canvas);
        shot.appendChild(viewEl);
        shotTip = el('div', 'fb-shot-tip', '正在截取当前画面…');
        shot.appendChild(shotTip);

        var tools = el('div', 'fb-tools');
        tools.appendChild(toolButton('pen', '画笔', 'pen'));
        tools.appendChild(toolButton('rect', '框选', 'rect'));
        tools.appendChild(toolButton('black', '打码', 'black'));
        tools.appendChild(toolButton('move', '移动'));
        tools.appendChild(toolAction('撤销', '', function () { ops.pop(); canvasApi.paint(); }));
        tools.appendChild(toolAction('清空标注', '', function () { ops.length = 0; canvasApi.paint(); }));
        tools.appendChild(toolAction('复位', '缩放回适配大小，平移回居中', function () { canvasApi.reset(); }));
        zoomLabel = el('span', 'fb-zoom', '');
        zoomLabel.title = '滚轮缩放（以光标为中心）；移动工具/空格/右键拖拽平移；100% = 1:1 精批';
        tools.appendChild(zoomLabel);
        // 手动上传兜底：快照拿不到（终端/投屏/画不出来）时选一张图随反馈提交
        var uploadLabel = el('label', 'fb-tool fb-upload', '上传截图');
        uploadLabel.title = '快照拿不到时，选一张图片随反馈提交';
        uploadInput = document.createElement('input');
        uploadInput.type = 'file';
        uploadInput.accept = 'image/png,image/jpeg,image/webp';
        uploadInput.hidden = true;
        uploadInput.addEventListener('change', onUploadPick);
        uploadLabel.appendChild(uploadInput);
        tools.appendChild(uploadLabel);
        shot.appendChild(tools);

        messageEl = el('textarea', 'fb-message');
        messageEl.maxLength = 500;
        messageEl.placeholder = '哪里不满意？你想要什么？';
        messageEl.addEventListener('focus', function () {
            // 手机键盘会把下半屏顶掉：等视口稳定后把输入框滚到中间
            setTimeout(function () {
                var vv = window.visualViewport;
                if ((vv && vv.height < window.innerHeight * 0.75) || window.innerWidth <= 640) {
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
        mine.appendChild(el('div', 'fb-mine-list'));   // 内容由 WPFeedbackMine 填

        panel.appendChild(head);
        panel.appendChild(write);
        panel.appendChild(mine);
        maskEl.appendChild(panel);
        root.appendChild(maskEl);
    }

    // ---- 截图：打开面板时静默快照一帧当前视口（§3.2）----
    function isStreamingPage() {
        var route = (window.WPFeedbackCapture.context().route) || location.pathname;
        return /\/terminal|\/ssh|\/screen/.test(route);
    }

    function setShotTip(text) {
        shotTip.hidden = !text;
        shotTip.textContent = text || '';
    }

    function setBase(img) {
        baseImage = img || null;
        if (canvasApi) { canvasApi.setImage(baseImage); }
    }

    function captureFailed(reason) {
        // 终端/投屏是实时画面（xterm/MJPEG）：明说走手动上传，不静默失败（验收 5）
        setShotTip(isStreamingPage()
            ? '终端/投屏画面抓不全——请手动上传截图，或只提交文字。'
            : (reason || '没抓到画面，可以手动上传截图，或只提交文字。'));
    }

    function captureShot() {
        setShotTip('正在截取当前画面…');
        setBase(null);
        uploadedFileId = '';
        uploadInput.value = '';
        var settled = false;
        // 兜底：重页面画得慢，6 秒还拿不到就按失败走手动上传，别把面板卡在"正在截取"
        var guard = setTimeout(function () {
            if (settled) { return; }
            settled = true;
            captureFailed('这次没抓到画面，等太久了。');
        }, 6000);
        window.WPFeedbackCapture.snapViewport().then(function (res) {
            if (settled) { return; }
            settled = true;
            clearTimeout(guard);
            if (!res || !res.shot) { return captureFailed(); }
            setBase(res.shot);
            if (isStreamingPage()) {
                setShotTip('终端/投屏画面可能不全，需要的话请再手动上传一张。');
            } else if (res.partial) {
                setShotTip('部分内容没抓到（比如内嵌视图），需要的话可以手动上传截图补充。');
            } else {
                setShotTip('');
            }
        }).catch(function () {
            if (settled) { return; }
            settled = true;
            clearTimeout(guard);
            captureFailed();
        });
    }

    function onUploadPick(ev) {
        var input = ev.target;
        var file = input.files && input.files[0];
        input.value = '';
        if (!file) { return; }
        if (!/^image\/(png|jpeg|webp)$/.test(file.type || '')) {
            setShotTip('只支持 png / jpg / webp 图片。');
            return;
        }
        setShotTip('正在处理上传的截图…');
        window.WPFeedback.uploadManualShot(file).then(function (res) {
            uploadedFileId = res.file_id;
            setBase(res.canvas);   // 上传的图也进画布，可以直接接着标注
            setShotTip('');
            setHint('截图已上传，会随这条反馈一起提交；画布上也能直接标注。');
        }).catch(function (err) {
            setShotTip(((err && err.message) || '上传失败') + '。可以重试，或只提交文字。');
        });
    }

    // ---- 页签：写反馈 / 我的反馈（后者内容在 feedback-mine.js）----
    function switchTab(name) {
        tabs.write.classList.toggle('is-on', name === 'write');
        tabs.mine.classList.toggle('is-on', name === 'mine');
        panel.querySelector('.fb-write').hidden = name !== 'write';
        panel.querySelector('.fb-mine').hidden = name !== 'mine';
        if (name === 'mine' && window.WPFeedbackMine) { window.WPFeedbackMine.show(); }
    }

    function open(tab) {
        if (!root) { return; }
        if (fabApi) { fabApi.place(); }
        maskEl.hidden = false;
        document.body.classList.add('fb-open');
        switchTab(tab || 'write');
        if (!tab || tab === 'write') { captureShot(); setTimeout(function () { messageEl.focus(); }, 60); }
    }

    function close() {
        if (!maskEl) { return; }
        maskEl.hidden = true;
        document.body.classList.remove('fb-open');
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
        var annotated = ops.length > 0;
        // 有标注就连画布一起传（含上传图的标注版，uploadedFileId 已让位）；纯手动
        // 上传且没标注就直接用已传的 file_id，不重复上传
        var needBlob = !!baseImage && !canvas.hidden && !!canvas.width
            && (annotated || !uploadedFileId);
        var finish = function (blob) {
            onSubmit({
                message: text,
                contact: contactEl.value.trim(),
                blob: blob || null,
                hasAnnotations: annotated,
                uploadedFileId: annotated ? '' : uploadedFileId,
                attachmentSource: uploadedFileId ? 'upload' : 'page',
                captureNote: (shotTip && shotTip.textContent) || ''
            });
        };
        if (needBlob) {
            window.WPFeedbackCapture.compress(canvas, 1024 * 1024)
                .then(finish, function () { finish(null); });
            return;
        }
        finish(null);
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
        // 画布模块（feedback-canvas.js）挂在同一全局上，这里装订交互
        var canvasMod = window.WPFeedbackPanel && window.WPFeedbackPanel.canvas;
        if (canvasMod && canvasMod.mount) {
            canvasApi = canvasMod.mount({
                wrap: viewEl,
                canvas: canvas,
                getBase: function () { return baseImage; },
                setBase: function (img) { baseImage = img || null; },
                getOps: function () { return ops; },
                getTool: function () { return tool; },
                onView: function (view) {
                    zoomLabel.textContent = baseImage ? canvasMod.zoomPercent(view.scale) + '%' : '';
                    zoomLabel.classList.toggle('is-11', !!baseImage && Math.abs(view.scale - 1) < 0.005);
                }
            });
        }
        // FAB 落位/避让/滚动让位在 feedback-fab.js；点一下就可能切换出底部工具条
        // （多选模式等）：跟着重排，不做轮询
        fabApi = window.WPFeedbackPanel && window.WPFeedbackPanel.fab;
        if (fabApi) {
            document.addEventListener('pointerdown', function () { setTimeout(fabApi.place, 0); }, true);
            document.addEventListener('click', function () { setTimeout(fabApi.place, 0); }, true);
            window.addEventListener('resize', function () {
                fabApi.place();
                if (canvasApi) { canvasApi.onResize(); }
            });
            window.addEventListener('orientationchange', fabApi.place);
            window.addEventListener('scroll', fabApi.onScroll, { passive: true });
            fabApi.watch();
            setTimeout(fabApi.place, 400);
        }
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
        canvasView: function () { return canvasApi ? canvasApi.view() : null; },
        reset: function () {
            messageEl.value = '';
            contactEl.value = '';
            ops = [];
            setBase(null);
            uploadedFileId = '';
            if (uploadInput) { uploadInput.value = ''; }
            setBusy(false);
            setHint('截图与页面信息会自动附带；密码框内容不会采集，手机号/证件号/邮箱会自动打码。');
        }
    };
})();
