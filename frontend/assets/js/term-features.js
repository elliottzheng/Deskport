// 终端页面 UI 层 — 状态栏/会话侧栏/控制键/搜索/日志/分屏/multi-exec
// /terminal 与 /ssh 共用; 会话与协议逻辑在 term-client.js
// 装配顺序: createUI() → TermClient.createManager(hooks) → init(manager)
window.TermFeatures = (function () {
    'use strict';

    var KEYS_PREF_KEY = 'showControlKeysV2';   // v2: 触屏默认显示(覆盖旧 key 里遗留的隐藏记忆)
    var SIDEBAR_PREF_KEY = 'sidebarOpen';

    function createUI() {
        var statusEl = document.getElementById('status');

        var searchAddons = {};
        var logState = {};            // uid: {enabled, chunks[]}
        var splitMode = false;
        var multiExecSet = new Set(); // multi-exec 勾选的 ssh 会话 uid
        var manager = null;           // init(manager) 时注入

        // 控制键按钮 data-key → 发送序列 (两页 #control-keys 共用)
        var KEY_SEQS = {
            esc: '\x1b', 'ctrl-c': '\x03', 'ctrl-s': '\x13',
            up: '\x1b[A', down: '\x1b[B', left: '\x1b[D', right: '\x1b[C',
            enter: '\r',
            home: '\x1b[H', end: '\x1b[F',
            pgup: '\x1b[5~', pgdn: '\x1b[6~'
        };

        var baseTitle = document.title;
        var pageKind = location.pathname === '/ssh' ? 'ssh' : 'local';

        function setStatus(text, cls) {
            statusEl.textContent = text;
            statusEl.className = cls || '';
        }

        var CUSTOM_NAMES_KEY = 'wp.sessionNames';
        var customNames = {};
        try { customNames = JSON.parse(localStorage.getItem(CUSTOM_NAMES_KEY) || '{}') || {}; }
        catch (err) { customNames = {}; }

        function nameKey(s) { return s.sessionID || s.uid; }
        function getCustomName(s) { return s ? (customNames[nameKey(s)] || '') : ''; }
        function saveCustomNames() {
            try { localStorage.setItem(CUSTOM_NAMES_KEY, JSON.stringify(customNames)); }
            catch (err) { /* ignore */ }
        }
        function renameSession(s) {
            if (!s) { return; }
            var current = getCustomName(s) || displayName(s);
            var name = window.prompt('会话名称（留空恢复自动标题）', current);
            if (name === null) { return; }
            name = name.trim();
            if (name) { customNames[nameKey(s)] = name; }
            else { delete customNames[nameKey(s)]; }
            saveCustomNames();
            onChange();
        }

        // 会话显示名: 自定义名 > 项目+应用标题/进程 > 应用标题 > 目录·shell
        function displayName(s) {
            if (!s) { return ''; }
            var custom = getCustomName(s);
            if (custom) { return custom; }
            var fg = (s.foreground || '').replace(/\.exe$/i, '');
            var shellLike = /^(pwsh|powershell|cmd|conhost|openconsole)$/i.test(fg);
            if (fg && !shellLike) {
                // AI/业务进程: 项目目录 + 应用自己设置的标题(如 kimi-code), 没有标题就用进程名
                var appLabel = s.title || fg;
                var where = s.foregroundCwdLabel || s.cwdLabel;
                return where ? (where + ' · ' + appLabel) : appLabel;
            }
            if (s.title) { return s.title; }
            var label = s.label || s.cmd || '';
            var lower = label.toLowerCase();
            var shell = label;
            if (s.kind === 'local') {
                if (lower.indexOf('pwsh') >= 0) { shell = 'PowerShell 7'; }
                else if (lower.indexOf('powershell') >= 0) { shell = 'Windows PowerShell'; }
                else if (lower.indexOf('cmd') >= 0) { shell = '命令提示符'; }
            }
            var parts = [];
            if (s.cwdLabel) { parts.push(s.cwdLabel); }
            parts.push(shell);
            return parts.join(' · ');
        }

        // 当前会话名同步到浏览器标签页 / 壳顶栏标题
        function notifyTitle() {
            var active = manager ? manager.getActive() : null;
            var label = displayName(active);
            document.title = label ? (label + ' — 远控平台') : baseTitle;
            if (window.parent && window.parent !== window) {
                try {
                    window.parent.postMessage({ source: WP_MSG.SRC.EMBED, type: WP_MSG.EMBED.TITLE, text: label,
                                             kind: active ? (active.kind || pageKind) : pageKind }, '*');
                } catch (err) { /* ignore */ }
            }
        }

        function onChange() {
            renderSidebar();
            syncSplit();
            syncEmpty();
            notifyTitle();
        }

        // 空工作区引导: 无可见会话时显示
        function syncEmpty() {
            const empty = document.getElementById('term-empty');
            if (!empty) { return; }
            const visible = manager.sessions.some(function (s) { return !s.el.hidden; });
            empty.hidden = visible;
            // 会话存在但都不可见 = 全在别处使用 → 显示接管提示; 一个都没有 → 显示新建引导
            const elsewhere = manager.sessions.length > 0;
            const newText = empty.querySelector('.empty-new');
            const takeText = empty.querySelector('.empty-takeover');
            if (newText) { newText.style.display = elsewhere ? 'none' : ''; }
            if (takeText) { takeText.style.display = elsewhere && !visible ? '' : 'none'; }
        }

        // 分屏: 左栏=前台会话, 右栏=第一个可用的其他会话
        function syncSplit() {
            const host = document.getElementById('terminal');
            if (!host) { return; }
            const active = manager ? manager.getActive() : null;
            if (!splitMode) {
                host.classList.remove('split');
                manager.sessions.forEach(function (s) {
                    s.el.classList.remove('split-right');
                    if (s !== active) { s.el.hidden = true; }
                });
                return;
            }
            host.classList.add('split');
            const right = manager.sessions.find(function (s) { return s !== active && s.state !== 'ended'; });
            if (right) {
                right.manualClose = false;
                right.el.hidden = false;
                right.el.classList.add('split-right');
                // 右栏会话也要有自己的 WS 通道, 否则永远空白
                manager.ensureConnected(right);
                try { manager.fitSession(right); } catch (err) { /* ignore */ }
            }
            if (active) { try { manager.fitSession(active); } catch (err) { /* ignore */ } }
        }

        // 日志捕获: 包装 term.write, 会话创建时经 decorateSession 注入
        function decorateSession(session) {
            const origWrite = session.term.write.bind(session.term);
            session.term.write = function (data) {
                const st = logState[session.uid];
                if (st && st.enabled) {
                    st.chunks.push(data instanceof Uint8Array ? new TextDecoder().decode(data) : String(data));
                    if (st.chunks.length > 20000) { st.chunks.splice(0, 2000); }
                }
                return origWrite(data);
            };
        }

        // ------------------------------------------------------------------
        // 侧边栏渲染
        // ------------------------------------------------------------------

        function renderSidebar() {
            const list = document.getElementById('session-list');
            if (!list || !manager) { return; }
            list.innerHTML = '';
            manager.sessions.forEach(function (s) {
                const li = document.createElement('li');
                li.className = 'session-item' + (s === manager.getActive() ? ' active' : '');
                li.innerHTML =
                    (s.kind === 'ssh'
                        ? '<label class="me-check" title="multi-exec 勾选"><input type="checkbox"' +
                          (multiExecSet.has(s.uid) ? ' checked' : '') + '></label>'
                        : '') +
                    '<span class="session-badge ' + s.kind + '">' + (s.kind === 'ssh' ? 'SSH' : '本地') + '</span>' +
                    '<span class="session-name">' + TermClient.escapeHtml(displayName(s)) + '</span>' +
                    '<span class="session-state">' + TermClient.stateText(s) + '</span>' +
                    '<button class="session-close" title="关闭终端">&times;</button>';
                const meBox = li.querySelector('.me-check input');
                if (meBox) {
                    meBox.addEventListener('click', function (e) {
                        e.stopPropagation();
                        toggleMultiExec(s.uid);
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
                        renameSession(s);
                    });
                }
                li.querySelector('.session-close').addEventListener('click', function (e) {
                    e.stopPropagation();
                    manager.closeSession(s);
                });
                list.appendChild(li);
            });
        }

        function toggleMultiExec(uid) {
            if (multiExecSet.has(uid)) { multiExecSet.delete(uid); }
            else { multiExecSet.add(uid); }
            renderSidebar();
        }

        // ------------------------------------------------------------------
        // L2: 搜索 / 日志 / 分屏 / multi-exec
        // ------------------------------------------------------------------

        function ensureSearchAddon(session) {
            if (!searchAddons[session.uid]) {
                // decorations: 全部匹配同时高亮 (默认只标当前匹配)
                const addon = new SearchAddon.SearchAddon({
                    decorations: {
                        matchBackground: '#3b4b66',
                        activeMatchBackground: '#1f6feb',
                        matchOverviewRuler: '#8b949e',
                        activeMatchColorOverviewRuler: '#1f6feb'
                    }
                });
                session.term.loadAddon(addon);
                searchAddons[session.uid] = addon;
            }
            return searchAddons[session.uid];
        }

        function init(m) {
            manager = m;
            var pageActive = true;
            // 前台进程 / 目录短名定时刷新（仅当前视图可见时）
            setInterval(function () {
                if (pageActive && document.visibilityState === 'visible') {
                    manager.refreshMeta();
                }
            }, 5000);
            window.addEventListener('message', function (e) {
                var d = e.data || {};
                if (d.source !== WP_MSG.SRC.SHELL || d.type !== WP_MSG.SHELL.VIEW_ACTIVE) { return; }
                pageActive = !!d.active;
                if (pageActive) { manager.refreshMeta(); }
            });
            const controlKeys = document.getElementById('control-keys');
            const sidebar = document.getElementById('sidebar');
            const sidebarToggle = document.getElementById('sidebar-toggle');

            // 侧边栏展开/隐藏 (localStorage 记忆; 三处入口统一走 setSidebar)
            // 壳内默认收起: 避免 220px 抽屉挡住整个会话视图
            var embedded = new URLSearchParams(location.search).get('embed') === '1';
            if (!embedded && localStorage.getItem(SIDEBAR_PREF_KEY) === '1') {
                sidebar.classList.add('open');
            }
            if (embedded) { sidebar.classList.remove('open'); }
            function setSidebar(open) {
                sidebar.classList.toggle('open', open);
                localStorage.setItem(SIDEBAR_PREF_KEY, open ? '1' : '0');
            }
            sidebarToggle.addEventListener('click', function () {
                setSidebar(!sidebar.classList.contains('open'));
            });
            document.getElementById('sidebar-close').addEventListener('click', function () {
                setSidebar(false);
            });
            document.getElementById('sidebar-backdrop').addEventListener('click', function () {
                setSidebar(false);
            });

            // 控制键区默认可见性: 移动端默认显示(触控需要), 桌面默认隐藏(键盘已够用)
            // 状态栏点击切换并记忆; 未记忆时跟随视口档位
            // 虚拟键默认可见性: 触屏(粗指针)或"触屏能力+窄视口"(手机桌面模式) → 显示; 键鼠宽屏 → 隐藏
            const touchQuery = window.matchMedia('(hover: none) and (pointer: coarse)');
            function isTouchDevice() {
                return touchQuery.matches
                    || ((navigator.maxTouchPoints || 0) > 0 && window.innerWidth < 1024);
            }
            function applyKeysDefault() {
                if (localStorage.getItem(KEYS_PREF_KEY) !== null) {
                    controlKeys.classList.toggle('hidden', localStorage.getItem(KEYS_PREF_KEY) === '0');
                } else {
                    controlKeys.classList.toggle('hidden', !isTouchDevice());
                }
            }
            function syncVkeysLabel() {
                const btn = document.getElementById('vkeys-toggle');
                if (btn) { btn.textContent = controlKeys.classList.contains('hidden') ? '虚拟键:关' : '虚拟键:开'; }
            }
            const _applyKeysDefault = applyKeysDefault;
            applyKeysDefault = function () { _applyKeysDefault(); syncVkeysLabel(); };
            applyKeysDefault();
            window.addEventListener('resize', applyKeysDefault);   // 无记忆时跟随视口/设备
            statusEl.title = '单击 显示/隐藏控制键';
            statusEl.addEventListener('click', function () {
                controlKeys.classList.toggle('hidden');
                localStorage.setItem(KEYS_PREF_KEY, controlKeys.classList.contains('hidden') ? '0' : '1');
                const active = manager.getActive();
                if (active) {
                    try { manager.fitSession(active); } catch (err) { /* ignore */ }
                    manager.sendResize();
                }
            });

            // 窗口变化 → 前台 fit 重算 → onResize 自动发 resize 帧
            window.addEventListener('resize', function () {
                const active = manager.getActive();
                if (active) {
                    try { manager.fitSession(active); } catch (err) { /* ignore */ }
                    manager.sendResize();
                }
            });

            // 搜索栏显隐 + 查找
            const searchBar = document.getElementById('search-bar');
            const searchInput = document.getElementById('search-input');
            const doSearch = function (backwards) {
                const active = manager.getActive();
                if (!active) { return; }
                const addon = ensureSearchAddon(active);
                if (backwards) { addon.findPrevious(searchInput.value); }
                else { addon.findNext(searchInput.value); }
            };
            document.getElementById('search-toggle').addEventListener('click', function () {
                searchBar.style.display = searchBar.style.display === 'flex' ? 'none' : 'flex';
                const active = manager.getActive();
                if (!active) { return; }
                ensureSearchAddon(active);
                if (searchBar.style.display === 'flex') { searchInput.focus(); }
            });
            // 输入即查 + 回车跳下一个, Shift+回车上一个
            searchInput.addEventListener('input', function () { doSearch(false); });
            searchInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { doSearch(e.shiftKey); }
                if (e.key === 'Escape') {
                    searchBar.style.display = 'none';
                    const active = manager.getActive();
                    if (active) { ensureSearchAddon(active).clearDecorations(); }
                }
            });
            document.getElementById('search-prev').addEventListener('click', function () { doSearch(true); });
            document.getElementById('search-next').addEventListener('click', function () { doSearch(false); });
            document.getElementById('search-close').addEventListener('click', function () {
                searchBar.style.display = 'none';
                const active = manager.getActive();
                if (active) { ensureSearchAddon(active).clearDecorations(); }
            });

            // 日志: 当前前台会话记录开关; 再点下载 blob
            document.getElementById('log-toggle').addEventListener('click', function () {
                const active = manager.getActive();
                if (!active) { return; }
                const uid = active.uid;
                if (logState[uid] && logState[uid].enabled) {
                    // 下载并停止
                    const blob = new Blob(logState[uid].chunks, { type: 'text/plain' });
                    const a = document.createElement('a');
                    a.href = URL.createObjectURL(blob);
                    a.download = 'term-' + uid + '.log';
                    a.click();
                    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
                    logState[uid].enabled = false;
                    setStatus('日志已下载并停止记录');
                } else {
                    logState[uid] = { enabled: true, chunks: [] };
                    setStatus('日志记录中 (再次点击下载并停止)', 'ok');
                }
            });

            // 控制键: 事件委托 (两页 #control-keys 结构一致, 按钮带 data-key)
            // mousedown preventDefault: 不让按钮抢走终端焦点, 点完可直接继续打字
            document.getElementById('control-keys').addEventListener('mousedown', function (e) {
                if (e.target.closest('.control-key')) { e.preventDefault(); }
            });
            document.getElementById('control-keys').addEventListener('click', function (e) {
                const btn = e.target.closest('.control-key');
                if (btn) { manager.sendControlKey(KEY_SEQS[btn.getAttribute('data-key')] || ''); }
            });

            // 回到底部浮钮: 当前会话滚离底部时出现, 点击回到底部并恢复跟随
            const scrollBottomBtn = document.getElementById('scroll-bottom');
            if (scrollBottomBtn) {
                scrollBottomBtn.addEventListener('click', function () {
                    manager.scrollActiveToBottom();
                });
                setInterval(function () {
                    const show = !!manager.getActive() && !manager.isActiveAtBottom();
                    if (scrollBottomBtn.hidden !== !show) { scrollBottomBtn.hidden = !show; }
                }, 500);
            }

            // 分屏: 左栏=前台会话, 右栏=第一个可用的其他会话
            document.getElementById('split-toggle').addEventListener('click', function () {
                splitMode = !splitMode;
                syncSplit();
            });

            // multi-exec: 广播输入发送到所有勾选的 SSH 会话
            const meInput = document.getElementById('multi-exec-input');
            if (meInput) {
                meInput.addEventListener('keydown', function (e) {
                    if (e.key !== 'Enter') { return; }
                    const text = meInput.value;
                    if (!text) { return; }
                    let sent = 0;
                    multiExecSet.forEach(function (uid) {
                        const s = manager.sessions.find(x => x.uid === uid);
                        if (s && s.ws && s.ws.readyState === 1) { s.ws.send(text + '\r'); sent += 1; }
                    });
                    meInput.value = '';
                    setStatus('multi-exec 已发送到 ' + sent + ' 个会话');
                });
            }

            syncEmpty();
            // 侧栏脚部工具 (移动端单行顶栏方案): 触发既有桌面控件
            document.getElementById('sidebar').addEventListener('click', function (e) {
                const t = e.target.closest('[data-act]');
                if (!t) { return; }
                const act = t.getAttribute('data-act');
                if (act === 'search') { const b = document.getElementById('search-toggle'); if (b) { b.click(); } }
                if (act === 'log') { const b = document.getElementById('log-toggle'); if (b) { b.click(); } }
                if (act === 'vkeys') {
                    const ck = document.getElementById('control-keys');
                    ck.classList.toggle('hidden');
                    localStorage.setItem(KEYS_PREF_KEY, ck.classList.contains('hidden') ? '0' : '1');
                    syncVkeysLabel();
                }
            });

            // 跨模块显式契约 (取代旧的隐式 window 全局):
            // termManager — ssh-ui 取活跃会话; termUI — ssh-ui 复用侧栏开合
            window.termManager = manager;
            window.termUI = { setSidebar: setSidebar };
            // ssh-ui.js 经全局入口在任一终端页发起 SSH 会话
            window.createSshSession = manager.createSshSession;
            window.toggleMultiExec = toggleMultiExec;
        }

        // SSH 认证/密钥告警钩子 (manager options 用; 页面装配时传入 createManager)
        function onAuthPrompt(session, obj) {
            if (window.SSHUI) { window.SSHUI.showAuthPrompt(session, obj); }
        }

        function onHostKeyChanged(session, obj) {
            if (window.SSHUI) { window.SSHUI.showHostKeyChanged(session, obj); }
        }

        return {
            setStatus: setStatus,
            onChange: onChange,
            decorateSession: decorateSession,
            onAuthPrompt: onAuthPrompt,
            onHostKeyChanged: onHostKeyChanged,
            init: init
        };
    }

    return { createUI: createUI };
})();
