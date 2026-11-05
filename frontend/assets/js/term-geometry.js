// 终端几何与元信息 — fit/resize/滚动跟随/控制键/bfcache 重连/会话元数据轮询。
// 拆自 term-client.js (2026-11-05): 原 517 行把会话生命周期、WS 帧协议与
// 尺寸同步混在一个闭包里; 这里只留"布局与输入管道"这一件事。
//
// 依赖通过 create(deps) 注入, 不反向 import term-client:
//   getActive()      当前前台会话
//   getSessions()    全部会话 (bfcache 断开/元信息轮询用)
//   connectWs(s)     补建连 (pageshow 恢复用)
//   onChange()       元信息变化时通知 UI 层
//   restoreEndpoint  会话列表接口 (元信息来源)
window.TermGeometry = (function () {
    'use strict';

    function create(deps) {
        function getActive() { return deps.getActive(); }
        function getSessions() { return deps.getSessions(); }

        // fit 后校正: xterm 的 screen 有时会比 viewport 可视宽度多出滚动条宽度,
        // 把原生滚动条盖住(鼠标点不到、拖不动)。按实际重叠减列, 让出滚动条。
        // xterm 内部 viewportY/baseY 才是可靠的"在不在底部"; DOM scrollTop 会滞后
        function atBottom(session) {
            var b = session && session.term && session.term.buffer
                    && session.term.buffer.active;
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
                session.ws.send(JSON.stringify({
                    type: 'resize', cols: session.term.cols, rows: session.term.rows }));
            }
        }

        function sendResize() {
            var s = getActive();
            if (s) { sendResizeFor(s); }
        }

        function sendControlKey(seq) {
            var s = getActive();
            if (s && s.ws && s.ws.readyState === 1) { s.ws.send(seq); }
        }

        // 定时刷新会话元数据 (cwd 短名 / 前台进程), 侧栏标题据此更新
        function refreshMeta() {
            if (!deps.restoreEndpoint) { return; }
            var all = getSessions();
            if (!all.length) { return; }   // 空闲时不轮询
            fetch(deps.restoreEndpoint)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    var byId = {};
                    (data.sessions || []).forEach(function (it) { byId[it.id] = it; });
                    var changed = false;
                    all.forEach(function (s) {
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
                    if (changed) { deps.onChange(); }
                })
                .catch(function () { /* 服务未就绪时忽略 */ });
        }

        function scrollActiveToBottom() {
            var s = getActive();
            if (!s) { return; }
            try { s.term.scrollToBottom(); } catch (err) { /* ignore */ }
            s.pinnedToBottom = true;
            clearTimeout(s.pinTimer);
            // scrollToBottom 触发的 onScroll 可能带着旧的 DOM 位置, 再确认一次
            s.pinTimer = setTimeout(function () {
                s.pinTimer = null;
                s.pinnedToBottom = true;
                try { s.term.scrollToBottom(); } catch (err) { /* ignore */ }
            }, 80);
        }

        function isActiveAtBottom() {
            var s = getActive();
            return !s || atBottom(s);
        }

        // 页面进入 bfcache 时 WS 不会自动关闭, 服务端会误以为仍被附着。
        // pagehide 主动断开 (会话本身保活), pageshow(persisted) 再重连。
        window.addEventListener('pagehide', function () {
            getSessions().forEach(function (s) {
                if (s.ws && s.ws.readyState <= 1) {
                    s.manualClose = true;
                    try { s.ws.close(1000, 'pagehide'); } catch (err) { /* ignore */ }
                    s.ws = null;
                }
            });
        });
        window.addEventListener('pageshow', function (e) {
            if (!e.persisted) { return; }
            getSessions().forEach(function (s) {
                if (s.state !== 'ended' && !s.ws) {
                    s.manualClose = false;
                    deps.connectWs(s);
                }
            });
        });

        return {
            atBottom: atBottom,
            pinIfNeeded: pinIfNeeded,
            fitSession: fitSession,
            sendResizeFor: sendResizeFor,
            sendResize: sendResize,
            sendControlKey: sendControlKey,
            refreshMeta: refreshMeta,
            scrollActiveToBottom: scrollActiveToBottom,
            isActiveAtBottom: isActiveAtBottom,
        };
    }

    return { create: create };
})();
