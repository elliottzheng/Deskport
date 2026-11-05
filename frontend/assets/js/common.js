// 共享格式化与转义 — 全站唯一实现（页面通过 window.WPCommon 调用）
(function () {
    'use strict';

    function fmtSize(n) {
        if (n === null || n === undefined || n === '') { return '-'; }
        var v = Number(n);
        if (!isFinite(v)) { return '-'; }
        var units = ['B', 'KB', 'MB', 'GB', 'TB'];
        var i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
        return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
    }

    function fmtTime(ts) {
        if (!ts) { return '-'; }
        return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
    }

    // 收件箱用的相对时间
    function fmtAgo(ts) {
        if (!ts) { return ''; }
        var diff = Date.now() / 1000 - ts;
        if (diff < 60) { return '刚刚'; }
        if (diff < 3600) { return Math.floor(diff / 60) + ' 分钟前'; }
        if (diff < 86400) { return Math.floor(diff / 3600) + ' 小时前'; }
        if (diff < 172800) { return '昨天'; }
        var d = new Date(ts * 1000);
        return (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
            String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }

    function escapeHtml(s) {
        return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    // ---- 统一确认对话框 ----
    // 取代分散各页的原生 confirm(): 原生弹窗在移动端是系统样式、按钮文案不可定制,
    // 桌面端也会被浏览器当成阻塞式对话框。返回 Promise<boolean>。
    // opts: {title, okText, cancelText, danger}
    // DOM 只创建一次并复用: 若每次打开都重建, 旧节点仍留在文档里,
    // getElementById 会拿到已orphan 的按钮, 点击无响应 (由回归测试抓出)。
    var confirmEl = null;
    var confirmResolve = null;

    function confirmClose(result) {
        if (confirmEl) { confirmEl.style.display = 'none'; }
        var resolve = confirmResolve;
        confirmResolve = null;
        if (resolve) { resolve(result); }
    }

    function confirmEnsureDom() {
        if (confirmEl) { return; }
        confirmEl = document.createElement('div');
        confirmEl.className = 'modal-mask';
        confirmEl.id = 'wp-confirm';
        confirmEl.style.display = 'none';
        confirmEl.innerHTML =
            '<div class="modal" role="dialog" aria-modal="true">' +
            '<div class="modal-title" id="wp-confirm-title"></div>' +
            '<p class="modal-meta" id="wp-confirm-msg"></p>' +
            '<div class="modal-actions">' +
            '<button class="btn btn-primary" id="wp-confirm-ok"></button>' +
            '<button class="btn btn-ghost" id="wp-confirm-cancel"></button>' +
            '</div></div>';
        document.body.appendChild(confirmEl);
        confirmEl.querySelector('#wp-confirm-ok')
            .addEventListener('click', function () { confirmClose(true); });
        confirmEl.querySelector('#wp-confirm-cancel')
            .addEventListener('click', function () { confirmClose(false); });
        // 点遮罩 = 取消 (与其它弹层一致); Esc 同样取消
        confirmEl.addEventListener('click', function (e) {
            if (e.target === confirmEl) { confirmClose(false); }
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && confirmResolve) { confirmClose(false); }
        });
    }

    function confirmDialog(message, opts) {
        opts = opts || {};
        confirmEnsureDom();
        confirmEl.querySelector('#wp-confirm-title').textContent = opts.title || '请确认';
        var msgEl = confirmEl.querySelector('#wp-confirm-msg');
        // textContent 写入不解析 HTML; \n 靠 pre-wrap 保留换行
        msgEl.textContent = String(message === null || message === undefined ? '' : message);
        msgEl.style.whiteSpace = 'pre-wrap';
        var ok = confirmEl.querySelector('#wp-confirm-ok');
        ok.textContent = opts.okText || '确定';
        ok.classList.toggle('btn-danger', !!opts.danger);
        confirmEl.querySelector('#wp-confirm-cancel').textContent =
            opts.cancelText || '取消';
        confirmEl.style.display = 'flex';
        // 上一个未答复的确认按取消处理, 避免悬挂 Promise
        if (confirmResolve) { confirmResolve(false); }
        return new Promise(function (resolve) { confirmResolve = resolve; });
    }

    window.WPCommon = {
        fmtSize: fmtSize,
        fmtTime: fmtTime,
        fmtAgo: fmtAgo,
        escapeHtml: escapeHtml,
        confirm: confirmDialog,
    };

    // ---- 统一 tooltip：把分散的原生 title 升级成可读的悬浮说明 ----
    (function () {
        if (!window.matchMedia || !window.matchMedia('(hover: hover)').matches) {
            return;
        }
        var box = null;
        var timer = null;
        var active = null;

        function ensureBox() {
            if (!box) {
                box = document.createElement('div');
                box.className = 'ui-tooltip';
                box.setAttribute('role', 'tooltip');
                box.hidden = true;
                document.body.appendChild(box);
            }
            return box;
        }

        function restore() {
            if (active && active.getAttribute('data-tip-title')) {
                active.setAttribute('title', active.getAttribute('data-tip-title'));
                active.removeAttribute('data-tip-title');
            }
        }

        function hide() {
            clearTimeout(timer);
            timer = null;
            if (box) { box.hidden = true; }
            restore();
            active = null;
        }

        function place(el) {
            var r = el.getBoundingClientRect();
            var b = ensureBox();
            b.textContent = el.getAttribute('data-tip-title') || '';
            b.hidden = false;
            var bw = b.offsetWidth;
            var bh = b.offsetHeight;
            var left = r.left + r.width / 2 - bw / 2;
            left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
            var top = r.top - bh - 8;
            if (top < 8) { top = r.bottom + 8; }
            b.style.left = left + 'px';
            b.style.top = top + 'px';
        }

        function show(el) {
            var text = el.getAttribute('title');
            if (!text) { return; }
            el.setAttribute('data-tip-title', text);
            el.removeAttribute('title');
            active = el;
            place(el);
        }

        function targetOf(e) {
            var el = e.target.closest('[title]');
            if (!el || el.tagName === 'IFRAME') { return null; }
            return el;
        }

        document.addEventListener('mouseover', function (e) {
            var el = targetOf(e);
            if (!el || el === active || (active && active.contains(el))) { return; }
            hide();
            timer = setTimeout(function () { show(el); }, 350);
        });
        document.addEventListener('mouseout', function (e) {
            if (!active) { return; }
            if (e.relatedTarget && active.contains(e.relatedTarget)) { return; }
            hide();
        });
        document.addEventListener('focusin', function (e) {
            var el = targetOf(e);
            if (!el) { return; }
            hide();
            show(el);
        });
        document.addEventListener('focusout', hide);
        document.addEventListener('scroll', hide, true);
        document.addEventListener('mousedown', hide, true);
        window.addEventListener('resize', hide);
    })();
})();