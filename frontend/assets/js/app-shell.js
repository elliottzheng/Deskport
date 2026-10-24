// 共享页面外壳 — 手机底部 4 项 + 更多弹层；平板/桌面左侧导航栏
// 页面只需放 <div id="app-shell"></div> 并引入 icons.js + app-shell.js
// 导航规则见 docs/ui-rules.md (R1/R2)。
window.AppShell = (function () {
    var BRAND = '远控平台';
    var PRIMARY = [
        { href: '/', icon: 'grid', label: '控制台' },
        { href: '/files', icon: 'folder', label: '文件' },
        { href: '/terminal', icon: 'terminal', label: '会话', match: ['/terminal', '/ssh'] }
    ];
    var sessionHref = '/terminal';

    function hrefOf(item) {
        return item.href === '/terminal' ? sessionHref : item.href;
    }

    var MORE = [
        { href: '/screen', icon: 'screen', label: '屏幕' },
        { href: '/command', icon: 'play', label: '命令' },
        { href: '/settings', icon: 'settings', label: '设置' }
    ];

    // 被 App 壳嵌入时：不渲染旧导航，并把顶层链接交给壳处理
    var EMBED = new URLSearchParams(location.search).get('embed') === '1';
    if (EMBED) {
        document.documentElement.classList.add('embed');
        document.body.classList.add('embed');
        var NAV_MAP = {
            '/': 'home', '/files': 'files', '/terminal': 'terminal',
            '/ssh': 'ssh', '/screen': 'screen', '/command': 'command',
            '/settings': 'settings', '/logout': 'logout',
        };
        document.addEventListener('click', function (e) {
            var a = e.target.closest ? e.target.closest('a[href]') : null;
            if (!a) { return; }
            var href = a.getAttribute('href') || '';
            if (!href || href.indexOf('/api/') === 0 || href.indexOf('/assets/') === 0
                || href.indexOf('http') === 0 || href.charAt(0) === '#'
                || a.hasAttribute('download') || a.target === '_blank') { return; }
            var path = href.split('?')[0];
            if (!NAV_MAP[path]) { return; }
            e.preventDefault();
            parent.postMessage({ source: WP_MSG.SRC.EMBED, type: WP_MSG.EMBED.NAV, href: path }, '*');
        }, true);
        return { embed: true, PRIMARY: [], MORE: [] };
    }

    function currentPath() {
        var p = location.pathname;
        if (p.length > 1) p = p.replace(/\/+$/, '');
        return p;
    }

    function isActive(item, path) {
        var matches = item.match || [item.href];
        return matches.some(function (href) {
            return href === '/' ? path === '/' : path.indexOf(href) === 0;
        });
    }

    function link(item, path, extraClass) {
        return '<a class="sidenav-link' + (isActive(item, path) ? ' is-active' : '') + '"'
            + ' href="' + hrefOf(item) + '" title="' + item.label + '">'
            + Icons.svg(item.icon) + '<span class="label">' + item.label + '</span></a>';
    }

    function render() {
        var mount = document.getElementById('app-shell');
        if (!mount) return;
        var path = currentPath();
        var lastKind = localStorage.getItem('wp.lastSessionKind');
        sessionHref = lastKind === 'ssh' ? '/ssh' : '/terminal';
        if (path === '/terminal') { localStorage.setItem('wp.lastSessionKind', 'local'); }
        if (path === '/ssh') { localStorage.setItem('wp.lastSessionKind', 'ssh'); }
        if (path !== '/') {
            try { localStorage.setItem('wp.lastView', JSON.stringify({ path: path, at: Date.now() })); } catch (err) { }
        }
        var moreActive = MORE.some(function (m) { return isActive(m, path); });

        var sidenav = '<aside class="sidenav">'
            + '<a class="sidenav-brand" href="/" title="' + BRAND + '">'
            + Icons.svg('grid') + '<span class="label">' + BRAND + '</span></a>'
            + '<nav class="sidenav-nav">'
            + PRIMARY.map(function (n) { return link(n, path); }).join('')
            + '<div class="sidenav-sep"></div>'
            + MORE.map(function (n) { return link(n, path); }).join('')
            + '</nav>'
            + '<div class="sidenav-foot">'
            + '<a class="sidenav-link" href="/logout" title="退出">'
            + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">'
            + '<path d="M9 4.5H5.5A1.5 1.5 0 0 0 4 6v12a1.5 1.5 0 0 0 1.5 1.5H9"/>'
            + '<path d="M15 8l4 4-4 4M19 12H9"/></svg><span class="label">退出</span></a>'
            + '</div>'
            + '</aside>';

        var topbar = '<header class="topbar">'
            + '<a class="brand" href="/">' + BRAND + '</a>'
            + '<span class="topbar-actions">'
            + '<a class="logout" href="/logout">退出</a>'
            + '</span>'
            + '</header>';

        var tabs = PRIMARY.map(function (n) {
            return '<a class="tab' + (isActive(n, path) ? ' is-active' : '') + '" href="' + hrefOf(n) + '">'
                + Icons.svg(n.icon) + '<span>' + n.label + '</span></a>';
        }).join('');
        tabs += '<button class="tab' + (moreActive ? ' is-active' : '') + '" id="more-tab" type="button">'
            + Icons.svg('more') + '<span>更多</span></button>';

        var moreSheet = '<div class="more-mask" id="more-mask" hidden>'
            + '<div class="more-sheet" role="dialog" aria-label="更多">'
            + '<div class="more-handle"></div>'
            + '<div class="more-grid">'
            + MORE.map(function (n) {
                return '<a class="more-item' + (isActive(n, path) ? ' is-active' : '') + '" href="' + n.href + '">'
                    + Icons.svg(n.icon) + '<span>' + n.label + '</span></a>';
            }).join('')
            + '</div>'
            + '<button class="btn btn-ghost more-close" id="more-close" type="button">取消</button>'
            + '</div></div>';

        mount.innerHTML = sidenav + topbar + '<nav class="tabbar">' + tabs + '</nav>' + moreSheet;

        var mask = document.getElementById('more-mask');
        var moreTab = document.getElementById('more-tab');
        var close = document.getElementById('more-close');
        if (moreTab && mask) {
            moreTab.addEventListener('click', function () { mask.hidden = false; });
        }
        if (mask) {
            mask.addEventListener('click', function (e) {
                if (e.target === mask) { mask.hidden = true; }
            });
        }
        if (close && mask) {
            close.addEventListener('click', function () { mask.hidden = true; });
        }
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && mask) { mask.hidden = true; }
        });
    }

    render();

    return { PRIMARY: PRIMARY, MORE: MORE };
})();