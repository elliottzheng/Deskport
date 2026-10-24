// SSH 页面共享上下文：DOM 查询、JSON API、主机列表引用、轻量提示。
window.SSHUiCtx = (function () {
    'use strict';
    async function api(url, opts) {
        const r = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
        return data;
    }
    return {
        $: function (id) { return document.getElementById(id); },
        api: api,
        getHosts: function () { return []; },
        toastMsg: function (text) {
            const el = document.getElementById('status');
            if (el) { el.textContent = text; }
        },
    };
})();
