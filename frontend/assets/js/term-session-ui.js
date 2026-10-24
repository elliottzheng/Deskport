// 会话 UI 工厂：xterm 容器、输入/标题/滚动事件；由 term-client 注入上下文。
window.TermSessionUI = (function () {
    'use strict';
    function build(ctx, cmd, existingSessionID, opts) {
            opts = opts || {};
            const kind = opts.kind || 'local';
            // 空 cmd 回退 pwsh7 (用户指定, 与服务端 DEFAULT_TERMINAL_CMD 一致);
            // 没装 pwsh 的机器由服务端回落 powershell.exe
            const command = kind === 'ssh' ? (opts.label || 'SSH')
                : ((cmd || '').trim() || 'pwsh.exe');
            const termHost = document.getElementById('terminal');
            const container = document.createElement('div');
            container.className = 'session-terminal';
            termHost.appendChild(container);

            const term = new Terminal({
                cursorBlink: true,
                fontFamily: 'Consolas, "Courier New", monospace',
                fontSize: 14,
                theme: {
                    background: '#0d1117',
                    foreground: '#c9d1d9',
                    cursor: '#58a6ff',
                    selectionBackground: '#264f78'
                }
            });
            const fit = new FitAddon.FitAddon();
            term.loadAddon(fit);
            term.open(container);

            const session = {
                uid: ctx.nextUid(),
                sessionID: existingSessionID || null,
                kind: kind,
                hostId: opts.hostId || null,
                cmd: command,
                cwd: opts.cwd || null,
                cwdLabel: opts.cwdLabel || '',
                foreground: '',
                foregroundCwd: '',
                foregroundCwdLabel: '',
                kick: opts.kick || null,
                label: opts.label || command,
                title: '',          // 应用通过 OSC 0/2 设置的终端标题; 空则回退 label
                term: term,
                fit: fit,
                el: container,
                ws: null,
                state: 'connecting',
                retryCount: 0,
                retryTimer: null,
                pingTimer: null,
                manualClose: false,
                needReplay: false,
                replayHold: false,  // 回放期间丢弃 xterm 的 DA 内建应答
                replayBuffer: [],   // 回放文本缓冲: 收齐后一次性写入, 避免可见的逐段滚动
                replayTimer: null,
                pinnedToBottom: true,  // 用户停在底部时, 新输出继续跟随
                pinTimer: null,
            };

            // 用户手动向上滚后不再被新输出拽回底部; 回到底部(或点浮钮)后恢复跟随
            term.onScroll(function () {
                session.pinnedToBottom = ctx.atBottom(session);
            });
            container.addEventListener('wheel', function (e) {
                if (e.deltaY < 0) { session.pinnedToBottom = false; }
            }, { passive: true });
            container.addEventListener('touchmove', function () {
                if (!ctx.atBottom(session)) { session.pinnedToBottom = false; }
            }, { passive: true });
            container.addEventListener('scroll', function (e) {
                if (e.target && e.target.classList && e.target.classList.contains('xterm-viewport')) {
                    if (!ctx.atBottom(session)) { session.pinnedToBottom = false; }
                }
            }, true);

            term.onData(function (d) {
                // 回放期间 ConPTY 会发终端能力查询(OSC 颜色/DA/XTGETTCAP), xterm 的
                // 自动应答若上行会落进 shell 输入行变成乱码 —— replay-end 前丢弃全部输入
                if (session.replayHold) { return; }
                // 非回放期同样会漏: ConPTY 把 DA/OSC 应答当键盘输入转给 shell
                // (TERM-02), 这些应答形态固定, 一律不上行
                if (TermEnv.DEVICE_REPLY.test(d)) { return; }
                if (session === ctx.getActive() && session.ws && session.ws.readyState === 1) {
                    session.ws.send(d);
                }
            });
            term.onResize(function () {
                ctx.sendResizeFor(session);
            });
            // 终端标题 (OSC 0/2): bash 的 user@host:cwd、程序自定义标题等
            term.onTitleChange(function (title) {
                var next = (title || '').trim();
                if (!next || next === session.title) { return; }
                session.title = next;
                ctx.onChange();
            });
            if (ctx.options.decorateSession) { ctx.options.decorateSession(session); }
            return session;

    }
    return { build: build };
})();
