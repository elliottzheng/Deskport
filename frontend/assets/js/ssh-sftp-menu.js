// SFTP 右键菜单 (行菜单 + 空白处菜单)。动作通过 init 注入, 避免反向依赖 ui 模块。
window.SftpMenu = (function () {
    'use strict';
    const state = () => window.SftpState.state;
    let actions = {};
    let menuEl = null;
    let toastMsg = function () {};

    function init(deps) {
        actions = deps.actions || {};
        toastMsg = deps.toastMsg || toastMsg;
    }

    function close() {
        if (menuEl) { menuEl.remove(); menuEl = null; }
    }

    function build(x, y, maxH) {
        close();
        const menu = document.createElement('div');
        menu.className = 'ctx-menu';
        menu.style.left = Math.min(x, innerWidth - 170) + 'px';
        menu.style.top = Math.min(y, innerHeight - maxH) + 'px';
        const item = (label, fn) => {
            const b = document.createElement('button');
            b.className = 'ctx-item';
            b.textContent = label;
            b.addEventListener('click', () => { close(); fn(); });
            menu.appendChild(b);
        };
        document.body.appendChild(menu);
        menuEl = menu;
        setTimeout(() => document.addEventListener('click', close, { once: true }), 0);
        return item;
    }

    function copyPaths(paths) {
        navigator.clipboard.writeText(paths.join('\n'))
            .then(() => toastMsg('路径已复制')).catch(() => {});
    }

    // ev: 鼠标事件或 {clientX, clientY} (长按合成)
    function showRow(ev, entry) {
        const sel = state().sel;
        const batch = Object.keys(sel).length > 1 && sel[entry.path]
            ? Object.values(sel) : [entry];
        const single = batch.length === 1 ? batch[0] : null;
        const item = build(ev.clientX, ev.clientY, 260);
        if (single && single.type === 'file') {
            item('下载', actions.download);
            item('预览 / 编辑', actions.edit);
            item('重命名', actions.rename);
            item('chmod', actions.chmod);
        } else {
            item('下载 (打包 zip' + (batch.length > 1 ? '，' + batch.length + ' 项' : '') + ')',
                 actions.download);
        }
        item('删除 (' + batch.length + ' 项)', actions.del);
        item('复制路径', () => copyPaths(single ? [single.path] : batch.map(x => x.path)));
    }

    // 空白处右键 / 长按: 新建与上传入口 (移动端没有右键)
    function showBlank(x, y) {
        const item = build(x, y, 160);
        item('新建文件夹', actions.mkdir);
        item('上传文件', actions.uploadFile);
        item('上传文件夹', actions.uploadFolder);
    }

    return { init: init, showRow: showRow, showBlank: showBlank, close: close };
})();
