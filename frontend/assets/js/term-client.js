// 终端会话核心 — xterm 封装 + WS 帧协议 + 重连/回放/尺寸同步
// 本地终端 (/terminal) 与 SSH (/ssh) 共用; 协议逻辑全站唯一来源
// 页面通过 createManager(options) 装配, UI 层见 term-features.js
//
// WS 帧协议 (与服务端 termcore.py/ssh.py 对齐, 改动需两端同步):
//   C→S 文本=终端输入 | JSON {type: ping|close|resize} | {type: ssh-auth-answer}
//   S→C 文本=终端输出 | JSON {type: hello|exit|error|ssh-auth|ssh-hostkey-changed|preempted|replay-end}
//   ssh-auth/ssh-hostkey-changed/preempted 经钩子/标记交页面处理
//   (preempted = 被其他连接抢占, 客户端收到后停止自动重连, 防两客户端互踢死循环)
window.TermClient = (function () {
    'use strict';

    var escapeHtml = window.WPCommon.escapeHtml;
    var PING_INTERVAL = TermEnv.PING_INTERVAL;
    var ACTIVE_SESSION_KEY = TermEnv.ACTIVE_SESSION_KEY;

    // options: setStatus/onChange/onAuthPrompt/onHostKeyChanged/restoreEndpoint/restoreItem/decorateSession
    function createManager(options) {
        var sessions = [];
        var activeSession = null;
        var uidCounter = 0;
        var setStatus = options.setStatus || function () {};
        var onChange = options.onChange || function () {};
        function buildSessionUI(cmd, existingSessionID, opts) {
            return TermSessionUI.build({
                nextUid: function () { return 's' + (++uidCounter); },
                getActive: function () { return activeSession; },
                sendResizeFor: sendResizeFor,
                atBottom: atBottom,
                onChange: onChange,
                options: options,
            }, cmd, existingSessionID, opts);
        }

        function controlContext() {
            return {
                getActive: function () { return activeSession; },
                ACTIVE_SESSION_KEY: ACTIVE_SESSION_KEY,
                setStatus: setStatus,
                fitSession: fitSession,
                sendResizeFor: sendResizeFor,
                pinIfNeeded: pinIfNeeded,
                clearTimers: clearTimers,
                onChange: onChange,
                options: options,
                connectWs: connectWs,
                RECONNECT_BASE: TermEnv.RECONNECT_BASE,
                RECONNECT_MAX: TermEnv.RECONNECT_MAX,
            };
        }

        function createSession(cmd, cwd, extra) {
            const opts = Object.assign({ cwd: cwd }, extra || {});
            const session = buildSessionUI(cmd, null, opts);
            sessions.push(session);
            switchSession(session);
        }

        // SSH 会话: hostId 对应 /api/ssh/ws; label 为主机名 (由 ssh-ui.js 调用)
        function createSshSession(hostId, label) {
            const session = buildSessionUI('', null, { kind: 'ssh', hostId: hostId, label: label });
            sessions.push(session);
            switchSession(session);
        }

        function switchSession(session) {
            if (!session) { return; }
            const prev = activeSession;
            if (prev && prev !== session) {
                clearTimers(prev);
                prev.manualClose = true;
                if (prev.ws && prev.ws.readyState <= 1) {
                    try { prev.ws.close(); } catch (err) { /* ignore */ }
                }
                prev.ws = null;
                prev.state = 'detached';
            }
            activeSession = session;
            if (session.sessionID) {
                localStorage.setItem(ACTIVE_SESSION_KEY, session.sessionID);
            }
            sessions.forEach(function (s) { s.el.hidden = (s !== session); });
            session.manualClose = false;
            onChange();
            if (session.state === 'ended') {
                setStatus('已退出: ' + session.cmd, 'err');
                return;
            }
            try { fitSession(session); } catch (err) { /* ignore */ }
            pinIfNeeded(session);
            connectWs(session);
        }

        function closeSession(session) {
            if (session.ws && session.ws.readyState === 1) {
                try { session.ws.send(JSON.stringify({ type: 'close' })); } catch (err) { /* ignore */ }
            } else if (session.sessionID) {
                // 未附着的会话(恢复后未连接/被抢占)没有可发 close 帧的通道,
                // 必须走 REST 终结, 否则进程与会话在服务端残留并在下次刷新复活
                if (session.kind === 'ssh') {
                    fetch('/api/ssh/disconnect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ hostId: session.hostId }) }).catch(function () {});
                } else {
                    fetch('/api/terminal/sessions/' + encodeURIComponent(session.sessionID), { method: 'DELETE' }).catch(function () {});
                }
            }
            clearTimers(session);
            session.manualClose = true;
            if (session.ws) {
                try { session.ws.close(); } catch (err) { /* ignore */ }
                session.ws = null;
            }
            try { session.term.dispose(); } catch (err) { /* ignore */ }
            if (session.el && session.el.parentNode) {
                session.el.parentNode.removeChild(session.el);
            }
            const idx = sessions.indexOf(session);
            if (idx >= 0) { sessions.splice(idx, 1); }
            if (activeSession === session) {
                activeSession = sessions.length ? sessions[sessions.length - 1] : null;
                if (activeSession) {
                    switchSession(activeSession);
                } else {
                    setStatus('未连接');
                }
            }
            onChange();
        }

        // 页面加载: 拉取服务端存活会话, 按页面提供的 restoreItem 重建列表
        function restoreSessions() {
            fetch(options.restoreEndpoint)
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    const items = (data && data.sessions) || [];
                    if (!items.length) { return; }
                    // 本地刚建立的会话 (如文件页"在终端中打开"交接过来的) 已在列表里,
                    // 服务端返回同一 id 时不能重复建 UI
                    const known = new Set(sessions.map(function (s) { return s.sessionID; }));
                    items.forEach(function (it) {
                        if (known.has(it.id)) { return; }
                        const s = options.restoreItem(buildSessionUI, it);
                        if (!s) { return; }
                        s.needReplay = true;
                        s.cwd = it.cwd || s.cwd;
                        s.cwdLabel = it.cwdLabel || s.cwdLabel;
                        s.foreground = it.foreground || '';
                        s.foregroundCwd = it.foregroundCwd || '';
                        s.foregroundCwdLabel = it.foregroundCwdLabel || '';
                        s.state = 'detached';   // 恢复列表里未附着的会话不该显示"连接中"
                        if (it.attached) {
                            s.attachedElsewhere = true;   // 正被其他窗口/设备使用 (侧栏标"使用中")
                        }
                        sessions.push(s);
                    });
                    // 调用方已创建并切到某个会话 (文件页交接) 时, 恢复列表只补侧栏,
                    // 不得抢占当前焦点 —— 否则新会话被隐藏, 用户看到的是旧终端
                    if (activeSession) { onChange(); return; }
                    // 单用户: 上次前台会话优先接管。attached 可能来自 bfcache 里的旧页面,
                    // 不应当成"其他设备"而拒绝恢复; 新附着会向旧连接发 preempted。
                    const rememberedId = localStorage.getItem(ACTIVE_SESSION_KEY);
                    const remembered = items.find(function (it) { return it.id === rememberedId; });
                    const first = remembered || items.find(function (it) { return !it.attached; });
                    if (!first) {
                        setStatus('检测到会话正在其他窗口使用, 点击侧栏会话可接管');
                        onChange();
                        return;
                    }
                    const target = sessions.find(function (s) { return s.sessionID === first.id; });
                    if (target) {
                        if (first.attached) {
                            setStatus('正在接管上次会话…');
                        }
                        switchSession(target);
                    }
                    onChange();
                })
                .catch(function () { /* 服务未就绪时静默, 用户可手动新建 */ });
        }

        // ------------------------------------------------------------------
        // WebSocket 连接 / 重连
        // ------------------------------------------------------------------

        function clearTimers(session) {
            if (session.pingTimer) { clearInterval(session.pingTimer); session.pingTimer = null; }
            if (session.retryTimer) { clearTimeout(session.retryTimer); session.retryTimer = null; }
            if (session.replayTimer) { clearTimeout(session.replayTimer); session.replayTimer = null; }
            if (session.pinTimer) { clearTimeout(session.pinTimer); session.pinTimer = null; }
        }

        function connectWs(session) {
            // 防抢占: 同一会话已有存活/半关闭连接时禁止重复建连
            if (session.ws && session.ws.readyState <= 2) { return; }
            clearTimers(session);
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            let url;
            if (session.kind === 'ssh') {
                url = proto + '://' + location.host + '/api/ssh/ws?hostId=' + encodeURIComponent(session.hostId);
            } else {
                url = proto + '://' + location.host + '/api/terminal/ws?cmd=' + encodeURIComponent(session.cmd);
                if (session.cwd) { url += '&cwd=' + encodeURIComponent(session.cwd); }
            }
            if (session.sessionID) { url += '&session=' + encodeURIComponent(session.sessionID); }
            if (session.needReplay) {
                url += '&replay=1';
                session.replayHold = true;
                session.el.classList.add('is-replaying');
                clearTimeout(session.replayTimer);
                session.replayTimer = setTimeout(function () {
                    if (!session.replayHold) { return; }
                    // 服务端回放帧丢失时的兜底: 解锁输入并显示已有内容
                    session.replayHold = false;
                    var pending = session.replayBuffer.join('');
                    session.replayBuffer = [];
                    if (pending) { session.term.write(pending); }
                    session.el.classList.remove('is-replaying');
                    fitSession(session);
                }, 10000);
            } else {
                session.replayHold = false;
                clearTimeout(session.replayTimer);
                session.el.classList.remove('is-replaying');
            }
            if (session.state !== 'reconnecting') { session.state = 'connecting'; }
            setStatus(session.state === 'reconnecting' ? '重连中: ' + session.cmd : '连接中: ' + session.cmd);

            const socket = new WebSocket(url);
            session.ws = socket;

            socket.onopen = function () {
                session.retryCount = 0;
                session.pingTimer = setInterval(function () {
                    if (socket.readyState === 1) { socket.send(JSON.stringify({ type: 'ping' })); }
                }, PING_INTERVAL);
            };

            socket.onmessage = function (e) {
                let obj = null;
                try { obj = JSON.parse(e.data); } catch (err) { obj = null; }
                if (obj && obj.type === 'ssh-auth') {
                    // keyboard-interactive / 2FA 提示 → 页面 UI 钩子
                    if (options.onAuthPrompt) { options.onAuthPrompt(session, obj); }
                    return;
                }
                if (obj && obj.type === 'replay-end') {
                    session.replayHold = false;
                    clearTimeout(session.replayTimer);
                    var replayData = session.replayBuffer.join('');
                    session.replayBuffer = [];
                    var done = false;
                    var reveal = function () {
                        if (done) { return; }
                        done = true;
                        session.pinnedToBottom = true;
                        try { session.term.scrollToBottom(); } catch (err) { /* ignore */ }
                        session.el.classList.remove('is-replaying');
                        fitSession(session);
                    };
                    if (replayData) {
                        session.term.write(replayData, reveal);
                        // 部分 xterm 版本在超大写入时不回调, 兜底 150ms 后显示
                        setTimeout(reveal, 150);
                    } else {
                        reveal();
                    }
                    return;
                }
                if (obj && obj.type === 'preempted') {
                    // 被其他窗口/设备抢占: 标记后 onclose 不再自动重连 (避免两客户端互踢死循环)
                    session.preempted = true;
                    return;
                }
                if (obj && obj.type) {
                    TermControls.handle(controlContext(), session, socket, obj);
                } else if (session.replayHold) {
                    session.replayBuffer.push(e.data);
                } else {
                    session.term.write(e.data);
                    pinIfNeeded(session);
                }
            };

            socket.onclose = function (e) {
                if (session.ws !== socket) { return; }
                session.ws = null;
                clearTimers(session);
                if (session.state === 'ended') {
                    onChange();
                    return;
                }
                if (session.manualClose) {
                    onChange();
                    return;
                }
                if (session.preempted) {
                    // 被其他窗口/设备接管: 停止自动重连, 手动点击侧栏会话即可重新接管
                    session.state = 'detached';
                    session.manualClose = true;
                    setStatus('会话已在其他窗口打开, 点击会话可重新接管', 'err');
                    onChange();
                    return;
                }
                // code 1006 = 握手失败/异常断开 (含未认证 401): 探测登录态, 掉线则转跳登录页
                if (e.code === 1006) { probeAuthAndRedirect(); }
                setStatus('连接断开 (code ' + e.code + ')', 'err');
                TermControls.schedule(controlContext(), session);
            };

            socket.onerror = function () {
                if (session.ws === socket) { setStatus('连接出错', 'err'); }
            };
        }

        // 会话过期/未登录时服务端会拒绝 WS, 探测登录态并转跳登录页
        // 守卫对未登录返回 401 {"error":"unauthorized"} (无 authenticated 字段), 故 !r.ok 同样视为掉线
        function probeAuthAndRedirect() {
            fetch('/api/auth/status', { credentials: 'same-origin' })
                .then(function (r) {
                    if (!r.ok) { throw new Error('unauthenticated'); }
                    return r.json();
                })
                .then(function (d) {
                    if (d && d.authenticated === false) {
                        redirectLogin();
                    }
                })
                .catch(function (err) {
                    if (err && err.message === 'unauthenticated') { redirectLogin(); }
                    /* 网络异常时保持重连逻辑 */
                });
        }

        function redirectLogin() {
            window.location.href = '/login?next=' +
                encodeURIComponent(location.pathname + location.search);
        }

        // ------------------------------------------------------------------
        // 尺寸同步 / 控制键
        // ------------------------------------------------------------------

        // fit 后校正: xterm 的 screen 有时会比 viewport 可视宽度多出滚动条宽度,
        // 把原生滚动条盖住(鼠标点不到、拖不动)。按实际重叠减列, 让出滚动条。
        // xterm 内部 viewportY/baseY 才是可靠的"在不在底部"; DOM scrollTop 会滞后
        function atBottom(session) {
            var b = session && session.term && session.term.buffer && session.term.buffer.active;
            if (!b) { return true; }
            return b.viewportY >= b.baseY;
        }

        // 用户停在底部时, 写入后强制回到底部; 防止 fit/resize 后视口漂移,
        // 出现"AI 一输出就停在上面"的错觉。用户主动上滚则不会被打断。
        function pinIfNeeded(session) {
            if (!session || !session.pinnedToBottom) { return; }
            clearTimeout(session.pinTimer);
            session.pinTimer = setTimeout(function () {
                session.pinTimer = null;
                if (!session.pinnedToBottom) { return; }
                // 排队期间用户可能已经上滚: 以 xterm 实际位置为准, 不能把人拽回底部
                if (!atBottom(session)) { session.pinnedToBottom = false; return; }
                try { session.term.scrollToBottom(); } catch (err) { /* ignore */ }
            }, 30);
        }

        function fitSession(session) {
            if (!session || !session.fit || !session.term) { return; }
            session.fit.fit();
            var screen = session.el.querySelector('.xterm-screen');
            var viewport = session.el.querySelector('.xterm-viewport');
            if (!screen || !viewport) { return; }
            var overlap = screen.offsetWidth - viewport.clientWidth;
            if (overlap <= 0) { return; }
            var cell = screen.offsetWidth / Math.max(1, session.term.cols);
            if (!cell) { return; }
            var cols = Math.max(2, session.term.cols - Math.ceil(overlap / cell));
            if (cols < session.term.cols) {
                session.term.resize(cols, session.term.rows);
            }
            if (session.pinnedToBottom) {
                try { session.term.scrollToBottom(); } catch (err) { /* ignore */ }
            }
        }

        function sendResizeFor(session) {
            // 容器隐藏/初次布局时 fit 可能算出 0 或负值, 不发; 服务端还会再兜一层下限
            if (session && session.ws && session.ws.readyState === 1
                && session.term.cols >= 2 && session.term.rows >= 1) {
                session.ws.send(JSON.stringify({ type: 'resize', cols: session.term.cols, rows: session.term.rows }));
            }
        }

        function sendResize() {
            if (activeSession) { sendResizeFor(activeSession); }
        }

        // 定时刷新会话元数据 (cwd 短名 / 前台进程), 侧栏标题据此更新
        function refreshMeta() {
            if (!options.restoreEndpoint) { return; }
            if (!sessions.length) { return; }   // 空闲时不轮询
            fetch(options.restoreEndpoint)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    var byId = {};
                    (data.sessions || []).forEach(function (it) { byId[it.id] = it; });
                    var changed = false;
                    sessions.forEach(function (s) {
                        var it = s.sessionID && byId[s.sessionID];
                        if (!it) { return; }
                        var fg = it.foreground || '';
                        var cwdLabel = it.cwdLabel || '';
                        var fgCwd = it.foregroundCwd || '';
                        var fgLabel = it.foregroundCwdLabel || '';
                        if (fg !== s.foreground || cwdLabel !== s.cwdLabel
                            || fgCwd !== s.foregroundCwd || fgLabel !== s.foregroundCwdLabel) {
                            s.foreground = fg;
                            s.cwdLabel = cwdLabel;
                            s.foregroundCwd = fgCwd;
                            s.foregroundCwdLabel = fgLabel;
                            changed = true;
                        }
                    });
                    if (changed) { onChange(); }
                })
                .catch(function () { /* 服务未就绪时忽略 */ });
        }

        function sendControlKey(seq) {
            if (activeSession && activeSession.ws && activeSession.ws.readyState === 1) {
                activeSession.ws.send(seq);
            }
        }

        // 页面进入 bfcache 时 WS 不会自动关闭, 服务端会误以为仍被附着。
        // pagehide 主动断开 (会话本身保活), pageshow(persisted) 再重连。
        window.addEventListener('pagehide', function () {
            sessions.forEach(function (s) {
                if (s.ws && s.ws.readyState <= 1) {
                    s.manualClose = true;
                    try { s.ws.close(1000, 'pagehide'); } catch (err) { /* ignore */ }
                    s.ws = null;
                }
            });
        });
        window.addEventListener('pageshow', function (e) {
            if (!e.persisted) { return; }
            sessions.forEach(function (s) {
                if (s.state !== 'ended' && !s.ws) {
                    s.manualClose = false;
                    connectWs(s);
                }
            });
        });

        return {
            sessions: sessions,
            getActive: function () { return activeSession; },
            createSession: createSession,
            createSshSession: createSshSession,
            switchSession: switchSession,
            closeSession: closeSession,
            restoreSessions: restoreSessions,
            clearTimers: clearTimers,
            connectWs: connectWs,
            ensureConnected: function (session) {
                // 分屏右栏等"非前台也要活"的场景: 无通道且未终结时补建连
                if (!session.ws && session.state !== 'ended' && !session.manualClose) {
                    connectWs(session);
                }
            },
            sendResize: sendResize,
            sendResizeFor: sendResizeFor,
            refreshMeta: refreshMeta,
            fitSession: fitSession,
            sendControlKey: sendControlKey,
            scrollActiveToBottom: function () {
                if (!activeSession) { return; }
                var s = activeSession;
                try { s.term.scrollToBottom(); } catch (err) { /* ignore */ }
                s.pinnedToBottom = true;
                clearTimeout(s.pinTimer);
                // scrollToBottom 触发的 onScroll 可能带着旧的 DOM 位置, 再确认一次
                s.pinTimer = setTimeout(function () {
                    s.pinTimer = null;
                    s.pinnedToBottom = true;
                    try { s.term.scrollToBottom(); } catch (err) { /* ignore */ }
                }, 80);
            },
            isActiveAtBottom: function () {
                return !activeSession || atBottom(activeSession);
            }
        };
    }

    return {
        createManager: createManager,
        escapeHtml: escapeHtml,
        stateText: TermEnv.stateText
    };
})();