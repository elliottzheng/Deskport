// SFTP 面板共享状态与纯路径工具 — 被 ui / menu / select / transfer 四个模块共用。
// 拆自 ssh-sftp-ui.js (2026-11-05): 原 618 行把状态、渲染、框选、菜单、
// 传输、文本编辑混在一个闭包里, 任何一个变化理由都要动整个文件。
window.SftpState = (function () {
    'use strict';

    const state = {
        hostId: null,
        path: '/',
        homePath: '',
        entries: [],      // 当前目录条目
        sel: {},          // path -> entry (FM-17 多选)
    };

    function selectedEntries() {
        return Object.values(state.sel).filter(x => !x.isParent);
    }

    function selectedPath() {
        const keys = Object.keys(state.sel);
        return keys.length ? keys[0] : null;
    }

    function renderSelection() {
        document.querySelectorAll('#sftp-list .sftp-row').forEach(r => {
            r.classList.toggle('selected', !!state.sel[r.dataset.path]);
        });
    }

    // ---- 纯路径工具 (posix 语义: 远程永远是 /) ----
    function posixDirname(p) {
        const idx = p.lastIndexOf('/');
        if (idx <= 0) { return '/'; }
        return p.slice(0, idx);
    }

    function parentPath(p) {
        return posixDirname(p) || '/';
    }

    function joinRemote(dir, name) {
        return (dir === '/' ? '' : dir) + '/' + name;
    }

    function displayPath(p) {
        if (p === state.homePath || p === '.') { return '登录目录'; }
        const parts = p.split('/').filter(Boolean);
        return parts.length ? parts[parts.length - 1] : p;
    }

    return {
        state: state,
        selectedEntries: selectedEntries,
        selectedPath: selectedPath,
        renderSelection: renderSelection,
        posixDirname: posixDirname,
        parentPath: parentPath,
        joinRemote: joinRemote,
        displayPath: displayPath,
    };
})();
