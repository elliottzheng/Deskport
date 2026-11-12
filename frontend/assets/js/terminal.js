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

    // 联邦: 远端机器列表填充到"新建"旁的选择器与移动端 picker
    function currentMachine() {
        const picker = document.getElementById('prog-picker');
        const pickerOpen = picker && picker.style.display === 'flex';
        const chip = pickerOpen
            ? document.querySelector('#picker-machines .machine-chip.is-active') : null;
        if (chip) {
            return { id: chip.getAttribute('data-machine') || '', name: chip.textContent || '本机' };
        }
        const sel = document.getElementById('new-session-machine');
        const opt = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
        return { id: sel ? (sel.value || '') : '', name: opt ? opt.textContent : '本机' };
    }

    async function loadMachines() {
        try {
            const r = await fetch('/api/settings/federation');
            const data = await r.json().catch(() => ({}));
            const machines = data.machines || [{ id: '', name: '本机' }];
            const sel = document.getElementById('new-session-machine');
            const box = document.getElementById('picker-machines');
            machines.forEach(function (m, i) {
                if (i === 0) { return; }
                if (sel) {
                    const opt = document.createElement('option');
                    opt.value = m.id;
                    opt.textContent = m.name;
                    sel.appendChild(opt);
                }
                if (box) {
                    const chip = document.createElement('button');
                    chip.type = 'button';
                    chip.className = 'machine-chip';
                    chip.setAttribute('data-machine', m.id);
                    chip.textContent = m.name;
                    box.appendChild(chip);
                }
            });
            if (box) {
                box.addEventListener('click', function (e) {
                    const chip = e.target.closest('.machine-chip');
                    if (!chip) { return; }
                    box.querySelectorAll('.machine-chip').forEach(function (c) {
                        c.classList.remove('is-active');
                    });
                    chip.classList.add('is-active');
                });
            }
        } catch (err) { /* 联邦不可用时仅本机 */ }
    }

    function createSessionFromInput() {
        const input = document.getElementById('cmd');
        const m = currentMachine();
        manager.createSession(input ? input.value : '', null, { machine: m.id, machineName: m.name });
    }

    document.getElementById('connect').addEventListener('click', createSessionFromInput);
    document.getElementById('cmd').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { createSessionFromInput(); }
    });
    // 侧栏"+"只负责"在当前选中的机器上建默认终端", 与顶栏自定义输入框解耦
    // (原先读 #cmd 的值, 同一个按钮会随顶栏输入内容变行为); 移动端弹程序选择框
    const picker = document.getElementById('prog-picker');
    document.getElementById('sidebar-add').addEventListener('click', function () {
        const touch = window.matchMedia('(hover: none) and (pointer: coarse)').matches;
        if (touch && picker) {
            picker.style.display = 'flex';
            document.getElementById('picker-custom-row').style.display = 'none';
            return;
        }
        const m = currentMachine();
        manager.createSession('', null, { machine: m.id, machineName: m.name });
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
                const m = currentMachine();
                manager.createSession(cmd, null, { machine: m.id, machineName: m.name });
                picker.style.display = 'none';
                return;
            }
            if (e.target.id === 'picker-custom-go') {
                const v = document.getElementById('picker-custom-cmd').value.trim();
                if (v) {
                    const m = currentMachine();
                    manager.createSession(v, null, { machine: m.id, machineName: m.name });
                    document.getElementById('picker-custom-cmd').value = '';
                    picker.style.display = 'none';
                }
                return;
            }
            if (e.target.id === 'picker-close' || e.target === picker) { picker.style.display = 'none'; }
        });
    }
    document.querySelectorAll('.preset-command[data-cmd]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            const m = currentMachine();
            manager.createSession(btn.getAttribute('data-cmd'),
                                  null, { machine: m.id, machineName: m.name });
        });
    });

    ui.init(manager);

    // FM-15: 文件页"在终端中打开"跨页交接 (sessionStorage 一次性消费)
    try {
        const pending = JSON.parse(sessionStorage.getItem('terminal-open') || 'null');
        if (pending && pending.cwd) {
            sessionStorage.removeItem('terminal-open');
            manager.createSession(pending.cmd || '', pending.cwd,
                { kick: pending.kick || null, machine: pending.machine || '' });
        }
    } catch (err) { /* 忽略脏数据 */ }

    loadMachines();
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
                manager.createSession(p.cmd || '', p.cwd,
                    { kick: p.kick || null, machine: p.machine || '' });
            }
        } catch (err) { /* 忽略 */ }
    });
})();
