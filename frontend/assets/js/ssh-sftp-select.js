// SFTP 选择交互: 桌面鼠标框选 (FM-17) + 移动端长按。
// 只负责"怎么选中", 选完之后交给注入的菜单回调。
window.SftpSelect = (function () {
    'use strict';
    let menu = null;
    let marquee = null;
    let pressTimer = null;
    let pressXY = null;

    function init(deps) {
        menu = deps.menu;
    }

    function sel() { return window.SftpState.state.sel; }
    function setSel(v) { window.SftpState.state.sel = v; }
    function entries() { return window.SftpState.state.entries; }

    // ---- 桌面框选: 空白处拖拽圈选 (触屏跳过, 走长按多选) ----
    function start(e) {
        if (e.button !== 0) { return; }
        if (e.target.closest('.sftp-row')) { return; }
        if (window.matchMedia('(hover: none) and (pointer: coarse)').matches) { return; }
        const list = e.currentTarget;
        marquee = {
            x1: e.clientX, y1: e.clientY,
            listEl: list, listRect: list.getBoundingClientRect(),
            additive: e.ctrlKey || e.metaKey,
            base: Object.assign({}, sel()),
            moved: false,
        };
        const box = document.createElement('div');
        box.className = 'sftp-marquee';
        list.appendChild(box);
        marquee.box = box;
        window.addEventListener('mousemove', move);
        window.addEventListener('mouseup', end);
        e.preventDefault();
    }

    function move(e) {
        const m = marquee;
        if (!m) { return; }
        const left = Math.min(m.x1, e.clientX), top = Math.min(m.y1, e.clientY);
        const width = Math.abs(e.clientX - m.x1), height = Math.abs(e.clientY - m.y1);
        m.box.style.left = (left - m.listRect.left) + 'px';
        m.box.style.top = (top - m.listRect.top) + 'px';
        m.box.style.width = width + 'px';
        m.box.style.height = height + 'px';
        if (width + height > 6) { m.moved = true; }
        const boxRect = { l: left, t: top, r: left + width, b: top + height };
        const next = m.additive ? Object.assign({}, m.base) : {};
        m.listEl.querySelectorAll('.sftp-row').forEach(row => {
            if (row.dataset.parent) { return; }   // 上级行不参与框选
            const r = row.getBoundingClientRect();
            if (r.left < boxRect.r && r.right > boxRect.l
                    && r.top < boxRect.b && r.bottom > boxRect.t) {
                const en = entries().find(x => x.path === row.dataset.path);
                if (en) { next[row.dataset.path] = en; }
            }
        });
        setSel(next);
        window.SftpState.renderSelection();
    }

    function end() {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', end);
        if (marquee && marquee.box) { marquee.box.remove(); }
        // 空白处单击(未拖动) = 清空选择 (Explorer 语义)
        if (marquee && !marquee.moved) { setSel({}); window.SftpState.renderSelection(); }
        marquee = null;
    }

    // ---- 移动端长按: 行 = 文件菜单, 空白 = 目录菜单 ----
    function pressStart(e) {
        if (e.touches.length !== 1) { return; }
        const t = e.touches[0];
        pressXY = [t.clientX, t.clientY];
        const originTarget = e.target;
        pressTimer = setTimeout(function () {
            pressTimer = null;
            const row = originTarget.closest ? originTarget.closest('.sftp-row') : null;
            if (row && row.dataset.path) {
                const entry = entries().find(x => x.path === row.dataset.path);
                if (entry) {
                    setSel({ [entry.path]: entry });
                    window.SftpState.renderSelection();
                    menu.showRow({ clientX: pressXY[0], clientY: pressXY[1] }, entry);
                    return;
                }
            }
            menu.showBlank(pressXY[0], pressXY[1]);
        }, 500);
    }

    function pressCancel() {
        if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
        pressXY = null;
    }

    function pressMove(e) {
        if (!pressTimer || !pressXY || !e.touches.length) { return; }
        const t = e.touches[0];
        if (Math.abs(t.clientX - pressXY[0]) > 10
                || Math.abs(t.clientY - pressXY[1]) > 10) {
            pressCancel();
        }
    }

    function bind(listEl) {
        listEl.addEventListener('mousedown', start);
        listEl.addEventListener('contextmenu', function (ev) {
            if (ev.target.closest('.sftp-row')) { return; }   // 行内菜单由 sftpRow 处理
            ev.preventDefault();
            menu.showBlank(ev.clientX, ev.clientY);
        });
        listEl.addEventListener('touchstart', pressStart, { passive: true });
        listEl.addEventListener('touchmove', pressMove, { passive: true });
        listEl.addEventListener('touchend', pressCancel);
        listEl.addEventListener('touchcancel', pressCancel);
    }

    return { init: init, bind: bind };
})();
