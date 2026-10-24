// SVG 图标注册表 — 全站图标唯一来源
// HTML: <i data-icon="grid"></i>  (脚本在 body 末尾同步注入, 先于页面 JS/Vue 挂载)
// JS:   Icons.svg('grid')  /  Icons.injectAll(container)
// 统一规格: viewBox 24x24, stroke 1.7, currentColor; 尺寸由 CSS 上下文选择器控制
window.Icons = (function () {
    var PATHS = {
        grid: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
        task: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2.8h6V4M8.5 9h7M8.5 13h7M8.5 17h4"/>',
        terminal: '<rect x="2.5" y="4" width="19" height="16" rx="2.5"/><path d="M6.5 9l3.5 3-3.5 3M12.5 15h5"/>',
        screen: '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8.5 20.5h7M12 17v3.5"/>',
        inbox: '<path d="M3 12.5l2.2-6.6A1.5 1.5 0 0 1 6.6 5h10.8a1.5 1.5 0 0 1 1.4 1l2.2 6.5V18a1.5 1.5 0 0 1-1.5 1.5H4.5A1.5 1.5 0 0 1 3 18v-5.5z"/><path d="M3 12.5h5l1.5 2h5l1.5-2h5"/>',
        recent: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
        more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
        settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 8.96 4.6H9a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z"/>',
        folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.6l2 2.5h8.4A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-12z"/>',
        play: '<circle cx="12" cy="12" r="9"/><path d="M10 8.5l5 3.5-5 3.5z"/>',
        plug: '<path d="M17.5 3.5c2 2 2 5 0 7L14 14l-2-2 3.5-3.5c1-1 1-2.5 0-3.5z"/><path d="M14 14l-4-4"/>',
        star: '<path d="M12 3.8l2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"/>',
        home: '<path d="M3 11.5l9-7.5 9 7.5M5.5 9.5V20h13V9.5"/>',
        drives: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
        folderNew: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.6l2 2.5h8.4A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-12z"/><path d="M12 10v5M9.5 12.5h5"/>',
        extract: '<path d="M4 4.5h6l2 2.5h8A1.5 1.5 0 0 1 21.5 8.5V18A1.5 1.5 0 0 1 20 19.5H4A1.5 1.5 0 0 1 2.5 18V6A1.5 1.5 0 0 1 4 4.5z"/><path d="M9.5 12.5h5M12 10v5"/>',
        rename: '<path d="M4 20h16M14.5 4.5l5 5L9 20H4v-5z"/>',
        trashFull: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5M14 11v5"/>',
        trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
        download: '<path d="M12 4v12M7.5 11.5L12 16l4.5-4.5M4.5 19h15"/>',
        upload: '<path d="M12 16V4M7.5 8.5L12 4l4.5 4.5M4.5 16v2.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V16"/>',
        menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
        chevronLeft: '<path d="M15 5l-7 7 7 7"/>',
        chevronRight: '<path d="M9 5l7 7-7 7"/>',
        refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6M20 3.5V8h-4.5"/>',
        pencil: '<path d="M14.5 4.5l5 5L9 20H4v-5z"/>',
        file: '<path d="M6 3h8l4.5 4.5V19.5A1.5 1.5 0 0 1 17 21H6a1.5 1.5 0 0 1-1.5-1.5v-15A1.5 1.5 0 0 1 6 3z"/><path d="M13.5 3v5h5"/>',
        sftp: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.6l2 2.5h8.4A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-12z"/><path d="M13.5 12.5h5M16 10l2.5 2.5L16 15"/>',
        search: '<circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/>',
        fileText: '<path d="M6 3h9l4.5 4.5V21H6z"/><path d="M14 3v5h5.5M9 12h6M9 16h6"/>',
        split: '<rect x="2.5" y="4" width="19" height="16" rx="2"/><path d="M12 4v16"/>',
        archive: '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M12 4v6M10 6.5h4M10 9h4M12 14v3.5M10.5 17.5h3"/>',
        disc: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.5"/>',
        table: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17M3.5 14.5h17M9.2 9.5V19.5M14.8 9.5V19.5"/>',
        image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="9.5" r="1.8"/><path d="M4.5 16.5l4.5-4 3 3 3.5-3.5 4 4"/>',
        music: '<path d="M9 18.5V6l10-2v12.5"/><circle cx="6.5" cy="18.5" r="2.5"/><circle cx="16.5" cy="16.5" r="2.5"/>',
        video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10.5l5-3v9l-5-3"/>',
        exe: '<rect x="3.5" y="4" width="17" height="16" rx="2"/><path d="M3.5 8.5h17M7.5 12l3.5 3-3.5 3M13 15h4"/>'
    };

    function svg(name) {
        var body = PATHS[name];
        if (!body) return '';
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">' + body + '</svg>';
    }

    function injectAll(root) {
        (root || document).querySelectorAll('i[data-icon]').forEach(function (el) {
            el.innerHTML = svg(el.getAttribute('data-icon'));
        });
        // <template> 内容存放在惰性 DocumentFragment, 不在文档树中,
        // 上一行 querySelectorAll 够不到; 递归处理以覆盖 v-if 模板 (Vue 挂载前注入)
        (root || document).querySelectorAll('template').forEach(function (tpl) {
            injectAll(tpl.content);
        });
    }

    // 页面脚本置于 body 末尾: 静态 DOM 已解析完成, 此刻注入早于页面 JS 与 Vue 挂载,
    // 因此 data-icon 上的 v-if/v-else 等指令随宿主元素一起被 Vue 正常编译
    injectAll(document);

    return { svg: svg, injectAll: injectAll };
})();
