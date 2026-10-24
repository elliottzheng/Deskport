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
})();