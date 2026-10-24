// /terminal 页装配 — 本地终端
// 会话核心与 WS 协议: term-client.js; 侧栏/搜索/日志/分屏: term-features.js
(function () {
    'use strict';

    const ui = TermFeatures.createUI();
    const manager = TermClient.createManager({
        setStatus: ui.setStatus,
        onChange: ui.onChange,
        decorateSession: ui.decorateSession,
        onAuthPrompt: ui.onAuthPrompt,
        onHostKeyChanged: ui.onHostKeyChanged,
        restoreEndpoint: '/api/terminal/sessions',
        restoreItem: function (build, it) { return build(it.cmd, it.id); }
    });

    function createSessionFromInput() {
        const input = document.getElementById('cmd');
        manager.createSession(input ? input.value : '');
    }

    document.getElementById('connect').addEventListener('click', createSessionFromInput);
    document.getElementById('cmd').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { createSessionFromInput(); }
    });
    // 侧栏"+"与预设终端按钮 (旧内联 onclick 已迁移到显式绑定)
    // 移动端(≤640): "+" 弹出程序选择对话框 (TER-01); 桌面保持默认 pwsh7 直建
    const picker = document.getElementById('prog-picker');
    document.getElementById('sidebar-add').addEventListener('click', function () {
        if (window.matchMedia('(hover: none) and (pointer: coarse)').matches && picker) {
            picker.style.display = 'flex';
            document.getElementById('picker-custom-row').style.display = 'none';
            return;
        }
        createSessionFromInput();
    });
    if (picker) {
        picker.addEventListener('click', function (e) {
            const item = e.target.closest('.picker-item');
            if (item) {
                const cmd = item.getAttribute('data-cmd');
                if (cmd === '__custom__') {
                    document.getElementById('picker-custom-row').style.display = 'flex';
                    document.getElementById('picker-custom-cmd').focus();
                    return;
                }
                manager.createSession(cmd);
                picker.style.display = 'none';
                return;
            }
            if (e.target.id === 'picker-custom-go') {
                const v = document.getElementById('picker-custom-cmd').value.trim();
                if (v) {
                    manager.createSession(v);
                    document.getElementById('picker-custom-cmd').value = '';
                    picker.style.display = 'none';
                }
                return;
            }
            if (e.target.id === 'picker-close' || e.target === picker) { picker.style.display = 'none'; }
        });
    }
    document.querySelectorAll('.preset-command[data-cmd]').forEach(function (btn) {
        btn.addEventListener('click', function () { manager.createSession(btn.getAttribute('data-cmd')); });
    });

    ui.init(manager);

    // FM-15: 文件页"在终端中打开"跨页交接 (sessionStorage 一次性消费)
    try {
        const pending = JSON.parse(sessionStorage.getItem('terminal-open') || 'null');
        if (pending && pending.cwd) {
            sessionStorage.removeItem('terminal-open');
            manager.createSession(pending.cmd || '', pending.cwd, { kick: pending.kick || null });
        }
    } catch (err) { /* 忽略脏数据 */ }

    manager.restoreSessions();

    // App 壳内：文件页“在终端中打开”通过 postMessage 触发起会话
    window.addEventListener('message', function (e) {
        var d = e.data || {};
        if (d.source !== WP_MSG.SRC.SHELL) { return; }
        if (d.type === WP_MSG.SHELL.ACTIVATE_SESSION) {
            var target = (manager.sessions || []).find(function (s) { return s.sessionID === d.id; });
            if (target) { manager.switchSession(target); }
            else { location.reload(); }   // 页面重载后按 activeSessionId 自动接管
            return;
        }
        if (d.type !== WP_MSG.SHELL.OPEN_TERMINAL) { return; }
        try {
            const p = JSON.parse(sessionStorage.getItem('terminal-open') || 'null');
            if (p && p.cwd) {
                sessionStorage.removeItem('terminal-open');
                manager.createSession(p.cmd || '', p.cwd, { kick: p.kick || null });
            }
        } catch (err) { /* 忽略 */ }
    });
})();
