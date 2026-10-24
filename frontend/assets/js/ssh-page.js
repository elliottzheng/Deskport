// /ssh 页装配 — SSH 工作台
// 会话核心与 WS 协议: term-client.js; 侧栏/搜索/日志/分屏: term-features.js
// 会话由 ssh-ui.js 的主机列表经 window.createSshSession 发起 (features.init 中导出)
(function () {
    'use strict';

    const ui = TermFeatures.createUI();
    const manager = TermClient.createManager({
        setStatus: ui.setStatus,
        onChange: ui.onChange,
        decorateSession: ui.decorateSession,
        onAuthPrompt: ui.onAuthPrompt,
        onHostKeyChanged: ui.onHostKeyChanged,
        restoreEndpoint: '/api/ssh/sessions',
        restoreItem: function (build, it) {
            return build('', it.id, { kind: 'ssh', hostId: it.hostId, label: it.label });
        }
    });

    // /ssh 页会话由主机列表发起; 侧栏"+"= 打开新建主机表单 (本页无本地终端入口)
    const addBtn = document.getElementById('sidebar-add');
    if (addBtn) {
        addBtn.addEventListener('click', function () {
            if (window.SSHUI) { window.SSHUI.openHostForm(); }
        });
    }

    ui.init(manager);
    manager.restoreSessions();

    // App 壳“最近”面板：切到指定 SSH 会话
    window.addEventListener('message', function (e) {
        var d = e.data || {};
        if (d.source !== WP_MSG.SRC.SHELL || d.type !== WP_MSG.SHELL.ACTIVATE_SESSION) { return; }
        var target = (manager.sessions || []).find(function (s) { return s.sessionID === d.id; });
        if (target) { manager.switchSession(target); }
        else { location.reload(); }   // 页面重载后按 activeSessionId 自动接管
    });
})();
