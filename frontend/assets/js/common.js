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

    window.WPCommon = {
        fmtSize: fmtSize,
        fmtTime: fmtTime,
        fmtAgo: fmtAgo,
        escapeHtml: escapeHtml,
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