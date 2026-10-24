// 终端控制帧与重连策略；由 term-client 注入上下文。
window.TermControls = (function () {
    'use strict';

        function handleControl(ctx, session, socket, obj) {
            if (obj.type === 'hello') {
                session.sessionID = obj.session;
                session.preempted = false;   // 新附着成功, 清除抢占标记
                if (session === ctx.getActive()) {
                    localStorage.setItem(ctx.ACTIVE_SESSION_KEY, obj.session);
                }
                if (obj.resumed === false) {
                    session.term.reset();
                }
                session.cwd = obj.cwd || session.cwd;
                session.cwdLabel = obj.cwdLabel || session.cwdLabel;
                session.foreground = obj.foreground || '';
                session.foregroundCwd = obj.foregroundCwd || '';
                session.foregroundCwdLabel = obj.foregroundCwdLabel || '';
                session.state = 'connected';
                ctx.setStatus('已连接: ' + (obj.label || obj.cmd || session.cmd), 'ok');
                try { ctx.fitSession(session); } catch (err) { /* ignore */ }
                ctx.sendResizeFor(session);
                ctx.pinIfNeeded(session);
                ctx.onChange();
                // 一次性首条消息 (交给 AI: 让 CLI 先读 任务.txt); 重连不再重发
                if (session.kick) {
                    const kickText = session.kick;
                    session.kick = null;
                    setTimeout(function () {
                        if (socket && socket.readyState === 1) { socket.send(kickText + '\r'); }
                    }, 600);
                }
                // replayHold 由服务端 replay-end 帧精确解除 (回放经 frp 时可能远超 1.5s)
                return;
            }
            if (obj.type === 'exit') {
                session.term.write('\r\n\x1b[90m[进程已退出 code=' + (obj.code === null ? '?' : obj.code) + ']\x1b[0m\r\n');
                session.state = 'ended';
                ctx.clearTimers(session);
                ctx.setStatus('已退出: ' + session.cmd, 'err');
                try { socket.close(); } catch (err) { /* ignore */ }
                ctx.onChange();
                return;
            }
            if (obj.type === 'error') {
                session.term.write('\r\n\x1b[31m[错误] ' + obj.msg + '\x1b[0m\r\n');
                session.state = 'ended';
                ctx.clearTimers(session);
                ctx.setStatus('错误: ' + obj.msg, 'err');
                try { socket.close(); } catch (err) { /* ignore */ }
                ctx.onChange();
                return;
            }
            if (obj.type === 'ssh-hostkey-changed') {
                // 主机密钥与 known_hosts 不符 (疑似中间人): 服务端发完即断。
                // 禁止自动重连, 交页面 UI 告警并让用户显式决定是否重新信任
                session.term.write('\r\n\x1b[31m[安全告警] ' + obj.msg + '\x1b[0m\r\n');
                session.state = 'ended';
                session.manualClose = true;
                ctx.clearTimers(session);
                ctx.setStatus('主机密钥变更, 已停止重连', 'err');
                if (ctx.options.onHostKeyChanged) { ctx.options.onHostKeyChanged(session, obj); }
                try { socket.close(); } catch (err) { /* ignore */ }
                ctx.onChange();
                return;
            }
            // pong: 忽略
        }

        function scheduleReconnect(ctx, session) {
            if (session !== ctx.getActive()) { return; }
            session.retryCount += 1;
            session.state = 'reconnecting';
            const delay = Math.min(ctx.RECONNECT_BASE * Math.pow(2, session.retryCount - 1), ctx.RECONNECT_MAX);
            ctx.setStatus('重连中 (第 ' + session.retryCount + ' 次)', 'err');
            ctx.onChange();
            session.retryTimer = setTimeout(function () {
                session.retryTimer = null;
                ctx.connectWs(session);
            }, delay);
        }

    return { handle: handleControl, schedule: scheduleReconnect };
})();
