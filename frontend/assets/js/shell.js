// App 壳：单文档、多视图（iframe 常驻），导航不重载
window.AppShellV2 = (function () {
    var VIEWS = {
        home: { href: '/', label: '控制台', icon: 'grid' },
        tasks: { href: '/tasks', label: '任务', icon: 'task' },
        files: { href: '/files', label: '文件', icon: 'folder' },
        session: { label: '会话', icon: 'terminal' },
        screen: { href: '/screen', label: '屏幕', icon: 'screen' },
        command: { href: '/command', label: '命令', icon: 'play' },
        settings: { href: '/settings', label: '设置', icon: 'settings' },
    };
    var TABS = [
        { view: 'home', label: '控制台', icon: 'grid' },
        { view: 'files', label: '文件', icon: 'folder' },
        { view: 'session', label: '会话', icon: 'terminal' },
        { view: 'more', label: '更多', icon: 'more' },
    ];
    var NAV = ['home', 'tasks', 'files', 'session', 'screen', 'command'];
    var frames = {};
    var currentView = 'home';
    var sessionKind = localStorage.getItem('wp.shell.kind') === 'ssh' ? 'ssh' : 'local';
    var filesLoc = new URLSearchParams(location.search).get('loc') || '';
    var sessionTitles = { local: '', ssh: '' };

    function svg(name) { return window.Icons ? Icons.svg(name) : ''; }

    function sessionFrameKey() { return sessionKind === 'ssh' ? 'ssh' : 'local'; }

    function frameSrc(key) {
        if (key === 'home') { return '/'; }
        if (key === 'local') { return '/terminal'; }
        if (key === 'ssh') { return '/ssh'; }
        if (key === 'files') {
            var p = new URLSearchParams(location.search);
            var loc = filesLoc || p.get('loc') || '';
            var path = p.get('path') || '';
            var q = [];
            if (loc) { q.push('loc=' + encodeURIComponent(loc)); }
            else if (path) { q.push('path=' + encodeURIComponent(path)); }
            return '/files' + (q.length ? '?' + q.join('&') : '');
        }
        return VIEWS[key] ? VIEWS[key].href : '/';
    }

    // 深链 /app?view=files&loc=inbox：loc 变化时重载文件 iframe
    function syncFilesLoc() {
        var loc = new URLSearchParams(location.search).get('loc') || '';
        if (loc === filesLoc) { return; }
        filesLoc = loc;
        var frame = frames.files;
        if (frame) {
            frame.dataset.loaded = '';
            frame.src = frameSrc('files') + '?embed=1';
        }
    }

    function ensureFrame(key) {
        if (frames[key]) { return frames[key]; }
        var frame = document.createElement('iframe');
        frame.className = 'shell-frame';
        frame.setAttribute('title', key);
        var src = frameSrc(key);
        frame.src = src + (src.indexOf('?') >= 0 ? '&' : '?') + 'embed=1';
        frame.addEventListener('load', function () {
            frame.dataset.loaded = '1';
            dispatchResize(frame);
            postActive(key);
        });
        document.getElementById('shell-frames').appendChild(frame);
        frames[key] = frame;
        return frame;
    }

    function dispatchResize(frame) {
        try {
            frame.contentWindow.dispatchEvent(new Event('resize'));
        } catch (err) { /* iframe 未就绪 */ }
    }

    // 告诉 iframe 自己是否在当前视图: 隐藏视图据此暂停 MJPEG/轮询
    function postActive(key) {
        var frame = frames[key];
        if (!frame) { return; }
        var active = currentView === 'session' ? key === sessionFrameKey() : key === currentView;
        try {
            frame.contentWindow.postMessage({ source: WP_MSG.SRC.SHELL, type: WP_MSG.SHELL.VIEW_ACTIVE, active: active }, '*');
        } catch (err) { /* iframe 未就绪 */ }
    }

    function frameFor(view) {
        return ensureFrame(view === 'session' ? sessionFrameKey() : view);
    }

    function updateNav() {
        document.querySelectorAll('[data-view]').forEach(function (el) {
            var v = el.getAttribute('data-view');
            var active = v === currentView
                || (v === 'more' && ['tasks', 'screen', 'command', 'settings'].indexOf(currentView) >= 0);
            el.classList.toggle('is-active', active);
        });
        document.querySelectorAll('#session-switch button, #session-inline button').forEach(function (btn) {
            btn.classList.toggle('is-active', btn.getAttribute('data-kind') === sessionKind);
        });
        var inline = document.getElementById('session-inline');
        if (inline) { inline.hidden = (currentView !== 'session'); }
    }

    function urlFor(view) {
        var url = '/app?view=' + view;
        if (view === 'session') { url += '&kind=' + sessionKind; }
        if (view === 'files' && filesLoc) { url += '&loc=' + encodeURIComponent(filesLoc); }
        return url;
    }

    function syncTitle() {
        if (currentView === 'session') {
            var label = sessionTitles[sessionKind];
            document.title = (label ? label + ' — ' : '会话 — ') + 'Deskport';
            return;
        }
        var view = VIEWS[currentView];
        document.title = (view ? view.label : 'Deskport') + ' — Deskport';
    }

    function show(view, opts) {
        opts = opts || {};
        if (!VIEWS[view]) { view = 'home'; }
        currentView = view;
        Object.keys(frames).forEach(function (key) {
            frames[key].classList.remove('is-active');
        });
        var frame = frameFor(view);
        frame.classList.add('is-active');
        document.getElementById('session-head').hidden = (view !== 'session');
        updateNav();
        var url = urlFor(view);
        if (opts.replace) { history.replaceState({ view: view }, '', url); }
        else if (!opts.silent) { history.pushState({ view: view }, '', url); }
        localStorage.setItem('wp.shell.view', view);
        localStorage.setItem('wp.shell.kind', sessionKind);
        rememberView(view);
        syncTitle();
        Object.keys(frames).forEach(postActive);
        setTimeout(function () { dispatchResize(frame); }, 60);
    }

    // 视图访问足迹（最近切换器的“视图”分组）
    function rememberView(view) {
        var items = readRecentViews().filter(function (it) { return it.view !== view; });
        items.unshift({ view: view, at: Date.now() });
        try { localStorage.setItem('wp.shell.recent', JSON.stringify(items.slice(0, 8))); } catch (err) { /* 配额 */ }
    }

    function readRecentViews() {
        try {
            var raw = JSON.parse(localStorage.getItem('wp.shell.recent') || '[]');
            return Array.isArray(raw) ? raw.filter(function (it) { return it && VIEWS[it.view]; }) : [];
        } catch (err) { return []; }
    }

    function setSessionKind(kind, opts) {
        sessionKind = kind === 'ssh' ? 'ssh' : 'local';
        localStorage.setItem('wp.shell.kind', sessionKind);
        show('session', opts || { replace: true });
    }

    function closeMore() { document.getElementById('shell-more').hidden = true; }

    function openMore() { document.getElementById('shell-more').hidden = false; }

    // ---- 最近：会话（本地/SSH）与视图的统一入口 ----
    function openRecents() {
        var sheet = document.getElementById('recent-sheet');
        var list = document.getElementById('recent-list');
        sheet.hidden = false;
        list.innerHTML = '<div class="recent-empty">加载中…</div>';
        Promise.all([
            fetchJson('/api/terminal/sessions'),
            fetchJson('/api/ssh/sessions')
        ]).then(function (res) {
            var sessions = (res[0].sessions || []).concat(res[1].sessions || []);
            list.innerHTML = renderRecents(sessions);
        }).catch(function () {
            list.innerHTML = renderRecents([]);
        });
    }

    function fetchJson(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (r) {
            return r.ok ? r.json() : { sessions: [] };
        }).catch(function () { return { sessions: [] }; });
    }

    function closeRecents() { document.getElementById('recent-sheet').hidden = true; }

    function renderRecents(sessions) {
        var html = '';
        var views = readRecentViews().filter(function (it) { return it.view !== 'session'; });
        if (sessions.length) {
            html += '<div class="shell-recent-group">会话</div>';
            html += sessions.map(function (it) {
                var kind = it.kind === 'ssh' ? 'ssh' : 'local';
                var title = it.label || it.cmd || (kind === 'ssh' ? 'SSH 会话' : '本地终端');
                var attached = it.attached ? '<span class="recent-card-state is-attached">已连接</span>'
                    : '<span class="recent-card-state">离线</span>';
                return '<button type="button" class="recent-card" data-recent-kind="' + kind + '" data-recent-session="' + escapeAttr(it.id) + '">'
                    + svg('terminal')
                    + '<span class="recent-card-main"><span class="recent-card-title">' + escapeHtml(title) + '</span>'
                    + '<span class="recent-card-sub">' + (kind === 'ssh' ? 'SSH' : '本机') + ' · ' + escapeHtml(it.id || '') + '</span></span>'
                    + attached + '</button>';
            }).join('');
        }
        if (views.length) {
            html += '<div class="shell-recent-group">视图</div>';
            html += views.map(function (it) {
                var v = VIEWS[it.view];
                return '<button type="button" class="recent-card" data-recent-view="' + it.view + '">'
                    + svg(v.icon) + '<span class="recent-card-main"><span class="recent-card-title">' + v.label + '</span></span></button>';
            }).join('');
        }
        return html || '<div class="recent-empty">还没有最近记录。<br>打开终端或 SSH 后会出现在这里。</div>';
    }

    function openRecentSession(kind, id) {
        if (!id) { return; }
        localStorage.setItem('activeSessionId', id);
        sessionKind = kind === 'ssh' ? 'ssh' : 'local';
        localStorage.setItem('wp.shell.kind', sessionKind);
        show('session', { replace: true });
        var frame = ensureFrame(sessionKind);
        var send = function () {
            try {
                frame.contentWindow.postMessage({ source: WP_MSG.SRC.SHELL, type: WP_MSG.SHELL.ACTIVATE_SESSION, id: id }, '*');
            } catch (err) { /* iframe 未就绪 */ }
        };
        if (frame.dataset.loaded) { setTimeout(send, 80); }
        else { frame.addEventListener('load', function () { setTimeout(send, 150); }, { once: true }); }
    }

    var escapeHtml = window.WPCommon.escapeHtml;

    function escapeAttr(s) { return escapeHtml(s); }

    function build() {
        document.getElementById('shell-nav').innerHTML = NAV.map(function (view) {
            var v = VIEWS[view];
            return '<a href="' + (view === 'session' ? '/app?view=session' : '/app?view=' + view) + '" data-view="' + view + '">'
                + svg(v.icon) + '<span class="nav-label">' + v.label + '</span></a>';
        }).join('');
        document.getElementById('shell-utils').innerHTML =
            '<button type="button" class="shell-recent-btn" id="recent-open" title="最近使用">'
            + svg('recent') + '<span>最近</span></button>'
            + '<span class="session-switch shell-session-inline" id="session-inline" hidden>'
            + '<button type="button" data-kind="local">本地</button>'
            + '<button type="button" data-kind="ssh">SSH</button></span>'
            + '<a href="/app?view=settings" data-nav="/settings">设置</a><a href="/logout">退出</a>';
        document.getElementById('shell-tabs').innerHTML = TABS.map(function (t) {
            return '<a href="' + (t.view === 'more' ? '#' : '/app?view=' + t.view) + '" data-view="' + t.view + '">'
                + svg(t.icon) + '<span>' + t.label + '</span></a>';
        }).join('');
        document.getElementById('more-grid').innerHTML =
            '<button type="button" class="more-item" data-more-recent="1">'
            + svg('recent') + '<span>最近</span></button>'
            + ['tasks', 'screen', 'command', 'settings'].map(function (view) {
                var v = VIEWS[view];
                return '<button type="button" class="more-item" data-more-view="' + view + '">'
                    + svg(v.icon) + '<span>' + v.label + '</span></button>';
            }).join('');
        document.getElementById('shell-more').hidden = true;

        document.getElementById('shell-nav').addEventListener('click', function (e) {
            var a = e.target.closest('a[data-view]');
            if (!a) { return; }
            e.preventDefault();
            show(a.getAttribute('data-view'));
        });
        document.getElementById('shell-tabs').addEventListener('click', function (e) {
            var a = e.target.closest('a[data-view]');
            if (!a) { return; }
            e.preventDefault();
            var view = a.getAttribute('data-view');
            if (view === 'more') { openMore(); return; }
            show(view);
        });
        document.addEventListener('click', function (e) {
            var btn = e.target.closest('button[data-kind]');
            if (btn) { setSessionKind(btn.getAttribute('data-kind')); }
        });
        document.getElementById('shell-utils').addEventListener('click', function (e) {
            var a = e.target.closest('a[data-nav]');
            if (!a) { return; }
            e.preventDefault();
            var href = a.getAttribute('data-nav');
            if (href === '/logout') { location.href = '/logout'; return; }
            var map = { '/settings': 'settings' };
            show(map[href] || 'home');
        });
        document.getElementById('more-grid').addEventListener('click', function (e) {
            var recentBtn = e.target.closest('[data-more-recent]');
            if (recentBtn) { closeMore(); openRecents(); return; }
            var btn = e.target.closest('button[data-more-view]');
            if (!btn) { return; }
            closeMore();
            show(btn.getAttribute('data-more-view'));
        });
        document.getElementById('more-close').addEventListener('click', closeMore);
        document.getElementById('shell-more').addEventListener('click', function (e) {
            if (e.target.id === 'shell-more') { closeMore(); }
        });

        document.getElementById('recent-open').addEventListener('click', openRecents);
        document.getElementById('recent-close').addEventListener('click', closeRecents);
        document.getElementById('recent-sheet').addEventListener('click', function (e) {
            if (e.target.id === 'recent-sheet') { closeRecents(); }
        });
        document.getElementById('recent-list').addEventListener('click', function (e) {
            var card = e.target.closest('[data-recent-view]');
            if (card) {
                closeRecents();
                show(card.getAttribute('data-recent-view'));
                return;
            }
            card = e.target.closest('[data-recent-session]');
            if (!card) { return; }
            closeRecents();
            openRecentSession(card.getAttribute('data-recent-kind'), card.getAttribute('data-recent-session'));
        });

        window.addEventListener('popstate', function () {
            var params = new URLSearchParams(location.search);
            var view = params.get('view') || 'home';
            var kind = params.get('kind');
            if (kind === 'ssh' || kind === 'local') { sessionKind = kind; }
            syncFilesLoc();
            show(view, { silent: true });
        });
        window.addEventListener('resize', function () {
            var frame = frames[frameForKey()];
            if (frame) { dispatchResize(frame); }
        });

        // 被嵌入页面通过 postMessage 请求壳导航
        window.addEventListener('message', function (e) {
            var data = e.data || {};
            if (data.source !== WP_MSG.SRC.EMBED) { return; }
            if (data.type === WP_MSG.EMBED.TITLE) {
                if (data.kind === 'local' || data.kind === 'ssh') {
                    sessionTitles[data.kind] = data.text || '';
                }
                if (currentView === 'session') { syncTitle(); }
                return;
            }
            if (data.type === WP_MSG.EMBED.FILES_PATH) {
                if (currentView !== 'files') { return; }
                filesLoc = data.loc || '';
                var qs = new URLSearchParams(location.search);
                qs.set('view', 'files');
                if (data.loc === 'inbox') {
                    qs.set('loc', 'inbox');
                    qs.delete('path');
                } else {
                    qs.delete('loc');
                    if (data.path && data.path !== '/') { qs.set('path', data.path); }
                    else { qs.delete('path'); }
                }
                history.replaceState(null, '', '/app?' + qs.toString());
                return;
            }
            if (data.type === WP_MSG.EMBED.NAV) {
                var href = data.href || '';
                if (href === '/logout') { location.href = '/logout'; return; }
                if (href === '/ssh') { setSessionKind('ssh'); return; }
                if (href === '/terminal') { setSessionKind('local'); return; }
                var map = {
                    '/': 'home', '/tasks': 'tasks', '/files': 'files', '/screen': 'screen',
                    '/command': 'command', '/settings': 'settings',
                };
                if (map[href]) { show(map[href]); }
            } else if (data.type === WP_MSG.SHELL.OPEN_TERMINAL) {
                setSessionKind('local');
                var frame = ensureFrame('local');
                var ping = function () {
                    try {
                        frame.contentWindow.postMessage({ source: WP_MSG.SRC.SHELL, type: WP_MSG.SHELL.OPEN_TERMINAL }, '*');
                    } catch (err) { /* ignore */ }
                };
                if (frame.dataset.loaded) { setTimeout(ping, 80); }
                else { frame.addEventListener('load', function () { setTimeout(ping, 150); }, { once: true }); }
            }
        });
    }

    function frameForKey() {
        return currentView === 'session' ? sessionFrameKey() : currentView;
    }

    function boot() {
        build();
        var params = new URLSearchParams(location.search);
        var view = params.get('view') || localStorage.getItem('wp.shell.view') || 'home';
        var kind = params.get('kind');
        if (kind === 'ssh' || kind === 'local') { sessionKind = kind; }
        if (!VIEWS[view]) { view = 'home'; }
        show(view, { replace: true });
    }

    boot();
    return { show: show, setSessionKind: setSessionKind, current: function () { return currentView; } };
})();