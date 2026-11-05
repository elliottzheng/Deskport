// 终端会话侧栏 — 会话列表渲染、徽标、双击重命名、multi-exec 勾选。
// 拆自 term-features.js (2026-11-05): 原 467 行把状态栏、侧栏渲染、搜索、
// 日志、分屏、控制键全塞在一个闭包里, 任何一个变化理由都要动整个文件。
window.TermSidebar = (function () {
    'use strict';

    const selected = new Set();   // multi-exec 勾选的 ssh 会话 uid
    let deps = {};

    function init(d) {
        deps = d || {};
    }

    function toggle(uid) {
        if (selected.has(uid)) { selected.delete(uid); }
        else { selected.add(uid); }
        render();
    }

    function uids() { return Array.from(selected); }

    function clear() { selected.clear(); }

    function render() {
        const list = document.getElementById('session-list');
        const manager = deps.manager;
        if (!list || !manager) { return; }
        list.innerHTML = '';
        manager.sessions.forEach(function (s) {
            const li = document.createElement('li');
            li.className = 'session-item' + (s === manager.getActive() ? ' active' : '');
            li.innerHTML =
                (s.kind === 'ssh'
                    ? '<label class="me-check" title="勾选多个 SSH 会话后，可在侧栏广播框批量发送命令"><input type="checkbox"' +
                      (selected.has(s.uid) ? ' checked' : '') + '></label>'
                    : '') +
                '<span class="session-badge ' + s.kind + (s.remote ? ' remote' : '') + '">' +
                    (s.remote ? '远端' : (s.kind === 'ssh' ? 'SSH' : '本地')) + '</span>' +
                '<span class="session-name">' + TermClient.escapeHtml(deps.displayName(s)) + '</span>' +
                '<span class="session-state">' + TermClient.stateText(s) + '</span>' +
                '<button class="session-close" title="关闭终端">&times;</button>';
            const meBox = li.querySelector('.me-check input');
            if (meBox) {
                meBox.addEventListener('click', function (e) {
                    e.stopPropagation();
                    toggle(s.uid);
                });
            }
            li.addEventListener('click', function () { manager.switchSession(s); });
            const nameEl = li.querySelector('.session-name');
            if (nameEl) {
                var tip = [];
                if (s.foreground) { tip.push(s.foreground); }
                if (s.foregroundCwd) { tip.push(s.foregroundCwd); }
                else if (s.cwd) { tip.push(s.cwd); }
                nameEl.title = (tip.join(' · ') + '（双击重命名）').trim();
                nameEl.addEventListener('dblclick', function (e) {
                    e.stopPropagation();
                    deps.renameSession(s);
                });
            }
            li.querySelector('.session-close').addEventListener('click', function (e) {
                e.stopPropagation();
                manager.closeSession(s);
            });
            list.appendChild(li);
        });
    }

    return { init: init, render: render, toggle: toggle, uids: uids, clear: clear };
})();
