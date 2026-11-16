// 会话 UI 工厂：xterm 容器、输入/标题/滚动事件；由 term-client 注入上下文。
// 另载两页共用的「快捷 CD 栏」(反馈 dad400ea / d9377f0f)：最近访问候选、
// 目录→CD、文件→既有下载通道、不存在→报错。纯函数经 module.exports 供
// Node 单测（tests/js/term_cwd.test.mjs）；页面侧挂 window.TermSessionUI。
var TermSessionUI = (function () {
    'use strict';

    // ---- 快捷 CD 栏: 最近访问存储 ----
    // 数据源 = 文件页「在终端打开」的 terminal-open 交接 (terminal.js 消费处
    // 调 rememberHandoff 落地), 持久化键随项目 wp.<域>.<名> 惯例
    // (同 wp.shell.recent / wp.taskPathRecent); 不另起第二份记录。
    var RECENT_KEY = 'wp.terminal.recentCwds';
    var RECENT_CAP = 10;

    // 裁剪: newest-first 列表按 path 去重(留最前)并封顶; 容错坏条目与裸字符串
    function trimRecent(list, cap) {
        var top = cap || RECENT_CAP;
        var out = [];
        var seen = {};
        var items = Array.isArray(list) ? list : [];
        for (var i = 0; i < items.length && out.length < top; i++) {
            var it = items[i];
            var path = it && typeof it === 'object' ? it.path : it;
            if (typeof path !== 'string') { continue; }
            path = path.trim();
            if (!path || seen[path]) { continue; }
            seen[path] = true;
            var machine = it && typeof it === 'object' && it.machine ? String(it.machine) : '';
            out.push({ path: path, machine: machine });
        }
        return out;
    }

    function readRecent() {
        try { return trimRecent(JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'), RECENT_CAP); }
        catch (err) { return []; }
    }

    function writeRecent(list) {
        try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (err) { /* 配额 */ }
    }

    function rememberRecent(path, machine) {
        var p = (path || '').trim();
        if (!p) { return; }
        var next = [{ path: p, machine: machine || '' }].concat(
            readRecent().filter(function (it) { return it.path !== p; }));
        writeRecent(trimRecent(next, RECENT_CAP));
    }

    // 文件页「在终端打开」交接落地时记一条 (同一数据源, 不另起记录)
    function rememberHandoff(pending) {
        if (pending && typeof pending.cwd === 'string' && pending.cwd.trim()) {
            rememberRecent(pending.cwd, pending.machine || '');
        }
    }

    // cd 命令: 引号包裹(路径可含空格/中文); 去掉尾部分隔符——cmd.exe 下
    // cd "E:\dir\" 的反斜杠会吃掉引号; 剥离后为空(根路径)则放弃
    function cdCommand(path) {
        var p = (path || '').trim().replace(/[\\/]+$/, '');
        return p ? 'cd "' + p + '"' : '';
    }

    function baseName(path) {
        var p = String(path || '').replace(/[\\/]+$/, '');
        var idx = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
        return idx >= 0 ? p.slice(idx + 1) : p;
    }

    // 探测判据: 目录探测 200=目录; 否则文件探测 200=文件; 都没有=不存在
    function classifyProbes(dirStatus, fileStatus) {
        if (dirStatus === 200) { return 'dir'; }
        if (fileStatus === 200) { return 'file'; }
        return 'missing';
    }
    function build(ctx, cmd, existingSessionID, opts) {
            opts = opts || {};
            const kind = opts.kind || 'local';
            // 空 cmd 回退 pwsh7 (用户指定, 与服务端 DEFAULT_TERMINAL_CMD 一致);
            // 没装 pwsh 的机器由服务端回落 powershell.exe
            const command = kind === 'ssh' ? (opts.label || 'SSH')
                : ((cmd || '').trim() || 'pwsh.exe');
            // 联邦远端新建: 标签带 [机器名], 侧栏徽标显示"远端"
            const machine = opts.machine || '';
            const machineName = opts.machineName || machine;
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
                label: opts.label || (machine ? ('[' + machineName + '] ' + command) : command),
                machine: opts.machine || '',        // 联邦远端机器 id; 空 = 本机
                remote: !!opts.machine,
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
    // /terminal 与 /ssh 顶栏下挂一行「快捷 CD」: 输入目录→CD, 输入文件→下载,
    // 不存在→报错; 本地终端页另出最近访问候选(≤10, 无历史不显示)。
    // DOM 由 JS 建 (两页 #bar 结构一致), 不动 HTML。
    function mountCwdBar(deps) {
        var manager = deps.manager;
        var setStatus = deps.setStatus || function () {};
        var pageKind = deps.pageKind || 'local';
        var bar = document.getElementById('bar');
        if (!bar || !bar.parentNode) { return; }

        var wrap = document.createElement('div');
        wrap.id = 'cwd-bar';
        wrap.className = 'cwd-bar';
        var label = document.createElement('span');
        label.className = 'cwd-label';
        label.textContent = 'CD';
        label.title = '快捷切换目录：输目录回车 CD；输文件路径直接下载';
        var input = document.createElement('input');
        input.id = 'cwd-input';
        input.className = 'cwd-input';
        input.placeholder = '目录路径回车 CD；文件路径直接下载';
        input.spellcheck = false;
        input.setAttribute('autocomplete', 'off');
        var go = document.createElement('button');
        go.id = 'cwd-go';
        go.type = 'button';
        go.className = 'cwd-go';
        go.textContent = '转到';
        go.title = '切换目录或下载文件';
        var recent = document.createElement('div');
        recent.id = 'cwd-recent';
        recent.className = 'cwd-recent';
        wrap.appendChild(label);
        wrap.appendChild(input);
        wrap.appendChild(go);
        wrap.appendChild(recent);
        bar.parentNode.insertBefore(wrap, bar.nextSibling);

        // 候选只在本地终端页出 (数据源是 terminal-open 交接, 归本地终端)
        function refreshRecent() {
            recent.innerHTML = '';
            var items = pageKind === 'local' ? readRecent() : [];
            if (!items.length) { return false; }
            items.forEach(function (it) {
                var chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'cwd-recent-chip';
                chip.textContent = baseName(it.path) || it.path;
                chip.title = it.path + (it.machine ? '（成员 ' + it.machine + '）' : '');
                chip.addEventListener('click', function () {
                    input.value = it.path;
                    hideRecent();
                    submit(it.path, it.machine);
                });
                recent.appendChild(chip);
            });
            return true;
        }

        function showRecent() {
            if (refreshRecent()) { recent.classList.add('open'); }
            else { recent.classList.remove('open'); }
        }

        function hideRecent() { recent.classList.remove('open'); }

        function machineNameOf(id) {
            if (!id) { return '本机'; }
            var opt = document.querySelector('#new-session-machine option[value="' + id + '"]');
            if (opt) { return opt.textContent; }
            var chip = document.querySelector('#picker-machines .machine-chip[data-machine="' + id + '"]');
            return chip ? chip.textContent : id;
        }

        function liveSession() {
            var s = manager.getActive ? manager.getActive() : null;
            return (s && s.ws && s.ws.readyState === 1) ? s : null;
        }

        // 目录/文件判定: 本地与成员会话走 /api/files (目录列表 + 下载 URL HEAD 探),
        // SSH 会话走 /api/ssh/sftp/list + /api/ssh/sftp/download——全是既有通道
        function classify(path, live) {
            var isSsh = !!(live && live.kind === 'ssh');
            var machine = (live && live.machine) || '';
            var q = 'path=' + encodeURIComponent(path)
                + (machine ? '&machine=' + encodeURIComponent(machine) : '');
            var dirProbe;
            var fileUrl;
            if (isSsh) {
                dirProbe = fetch('/api/ssh/sftp/list', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ hostId: live.hostId, path: path }),
                });
                fileUrl = '/api/ssh/sftp/download?hostId=' + encodeURIComponent(live.hostId) + '&' + q;
            } else {
                dirProbe = fetch('/api/files?' + q);
                fileUrl = '/api/files/download?' + q;
            }
            return dirProbe.then(function (r) {
                if (r.ok) { return { kind: 'dir' }; }
                return fetch(fileUrl, { method: 'HEAD' }).then(function (r2) {
                    return { kind: classifyProbes(0, r2.status), downloadUrl: fileUrl };
                }, function () { return { kind: 'missing' }; });
            }, function () { return { kind: 'missing' }; });
        }

        function triggerDownload(url) {
            var a = document.createElement('a');
            a.href = url;
            a.download = '';
            document.body.appendChild(a);
            a.click();
            a.remove();
        }

        function submit(raw, hintMachine) {
            var path = (raw || '').trim();
            if (!path) { return; }
            var wantMachine = hintMachine || '';
            var live = liveSession();
            // 候选带着别的成员机: 不往当前会话里 CD, 换到那台机器新开
            if (live && wantMachine && live.machine !== wantMachine) { live = null; }
            if (!live && pageKind !== 'local') {
                setStatus('请先连接一个 SSH 会话', 'err');
                return;
            }
            setStatus('正在识别路径…');
            classify(path, live).then(function (res) {
                if (res.kind === 'file') {
                    triggerDownload(res.downloadUrl);
                    setStatus('开始下载: ' + baseName(path), 'ok');
                    return;
                }
                if (res.kind === 'dir') {
                    var machine = live ? (live.machine || '') : wantMachine;
                    if (live) {
                        live.ws.send(cdCommand(path) + '\r');
                    } else {
                        manager.createSession('', path,
                            { machine: machine, machineName: machineNameOf(machine) });
                    }
                    rememberRecent(path, machine);
                    setStatus('已切换目录: ' + path, 'ok');
                    return;
                }
                setStatus('路径不存在或无法访问: ' + path, 'err');
            });
        }

        input.addEventListener('focus', showRecent);
        input.addEventListener('blur', function () { setTimeout(hideRecent, 150); });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { hideRecent(); submit(input.value); }
            if (e.key === 'Escape') { hideRecent(); }
        });
        go.addEventListener('click', function () { hideRecent(); submit(input.value); });
    }

    return {
        build: build,
        mountCwdBar: mountCwdBar,
        rememberHandoff: rememberHandoff,
        readRecent: readRecent,
        rememberRecent: rememberRecent,
        trimRecent: trimRecent,
        cdCommand: cdCommand,
        classifyProbes: classifyProbes,
        baseName: baseName,
        RECENT_KEY: RECENT_KEY,
        RECENT_CAP: RECENT_CAP,
    };
})();
if (typeof window !== 'undefined') { window.TermSessionUI = TermSessionUI; }
if (typeof module !== 'undefined' && module.exports) { module.exports = TermSessionUI; }
