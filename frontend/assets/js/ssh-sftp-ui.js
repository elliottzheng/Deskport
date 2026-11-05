// SSH SFTP 面板编排: 状态迁移、列表渲染、导航、快速访问、CRUD、文本编辑、绑定。
// 拆自原 618 行的单体闭包 (2026-11-05):
//   ssh-sftp-state.js     共享状态 + 纯路径工具 + 选中渲染
//   ssh-sftp-select.js    桌面框选 + 移动端长按
//   ssh-sftp-menu.js      行菜单 / 空白处菜单 (动作由本文件注入)
//   ssh-sftp-transfer.js  下载 / 转存本机 / 上传
window.SSHSftpUI = (function (ctx) {
    'use strict';
    const $ = ctx.$;
    const api = ctx.api;
    const toastMsg = ctx.toastMsg;
    const escapeHtml = WPCommon.escapeHtml;
    const fmtSize = WPCommon.fmtSize;
    const S = window.SftpState;

    let editingFile = null;

    // ---- 主机下拉 ----
    function hostLabel(hostId) {
        const h = ctx.getHosts().find(x => x.id === hostId);
        return h ? h.name : (hostId || '');
    }

    function renderHostOptions() {
        const sel = $('sftp-host-select');
        if (!sel) { return; }
        sel.innerHTML = '';
        ctx.getHosts().forEach(h => {
            const opt = document.createElement('option');
            opt.value = h.id;
            opt.textContent = h.name;
            if (h.id === S.state.hostId) { opt.selected = true; }
            sel.appendChild(opt);
        });
        sel.onchange = function () {
            S.state.hostId = this.value;
            S.state.path = SftpMemory.getLast(S.state.hostId) || '.';
            $('sftp-host-label').textContent = hostLabel(S.state.hostId);
            renderQuick();
            navigate(S.state.path);
        };
    }

    function togglePanel() {
        const panel = $('sftp-panel');
        const opening = panel.style.display !== 'flex';
        panel.style.display = opening ? 'flex' : 'none';
        if (!opening) { return; }
        // 默认目标: 当前活跃 SSH 会话的主机; 否则第一台主机
        if (!S.state.hostId) {
            const active = window.termManager && window.termManager.getActive();
            if (active && active.kind === 'ssh' && active.hostId) {
                S.state.hostId = active.hostId;
            } else if (ctx.getHosts().length) {
                S.state.hostId = ctx.getHosts()[0].id;
            }
        }
        $('sftp-host-label').textContent = hostLabel(S.state.hostId);
        if (!S.state.path || S.state.path === '/') {
            S.state.path = SftpMemory.getLast(S.state.hostId) || '.';
        }
        renderQuick();
        navigate(S.state.path);
    }

    // ---- 快速访问 (当前路径可固定、可取消) ----
    function renderQuick() {
        const box = $('sftp-quick');
        if (!box) { return; }
        const pins = SftpMemory.getPins(S.state.hostId);
        box.innerHTML = '';
        if (!pins.length) {
            box.style.display = 'none';
            updatePinStar();
            return;
        }
        box.style.display = 'flex';
        pins.forEach(function (p) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'sftp-quick-chip' + (p === S.state.path ? ' active' : '');
            btn.textContent = S.displayPath(p);
            btn.title = p;
            btn.addEventListener('click', function () { navigate(p); });
            btn.addEventListener('contextmenu', function (ev) {
                ev.preventDefault();
                WPCommon.confirm('取消固定 ' + p + ' ？', { okText: '取消固定', danger: true })
                    .then(function (ok) {
                        if (!ok) { return; }
                        SftpMemory.unpin(S.state.hostId, p);
                        renderQuick();
                    });
            });
            box.appendChild(btn);
        });
        updatePinStar();
    }

    function updatePinStar() {
        const btn = $('sftp-pin');
        if (!btn) { return; }
        const pinned = SftpMemory.isPinned(S.state.hostId, S.state.path);
        btn.textContent = pinned ? '★' : '☆';
        btn.title = pinned ? '取消固定当前目录' : '固定当前目录到快速访问';
    }

    function togglePin() {
        if (!S.state.hostId || !S.state.path) { return; }
        if (SftpMemory.isPinned(S.state.hostId, S.state.path)) {
            SftpMemory.unpin(S.state.hostId, S.state.path);
        } else {
            SftpMemory.pin(S.state.hostId, S.state.path);
        }
        renderQuick();
    }

    // ---- 列表渲染与导航 ----
    async function navigate(path) {
        S.state.path = path || '.';
        $('sftp-path').textContent = S.state.path;
        renderQuick();
        const box = $('sftp-list');
        box.innerHTML = '<div class="empty-tip">加载中…</div>';
        try {
            const data = await api('/api/ssh/sftp/list', {
                method: 'POST',
                body: JSON.stringify({ hostId: S.state.hostId, path: S.state.path }),
            });
            S.state.entries = data.entries || [];
            S.state.sel = {};
            S.state.path = data.currentPath || S.state.path;
            S.state.homePath = data.homePath || S.state.homePath;
            if (S.state.homePath) { SftpMemory.setHome(S.state.hostId, S.state.homePath); }
            $('sftp-path').textContent = S.state.path;
            SftpMemory.setLast(S.state.hostId, S.state.path);
            renderQuick();
            box.innerHTML = '';
            if (S.state.path !== '/' && data.parentPath !== null) {
                box.appendChild(row({ name: '..', path: data.parentPath,
                                      type: 'folder', size: 0 }, true));
            }
            S.state.entries.forEach(e => { box.appendChild(row(e, false)); });
            if (!S.state.entries.length) {
                box.innerHTML = '<div class="empty-tip">空目录</div>';
            }
        } catch (err) {
            box.innerHTML = '<div class="empty-tip">' + err.message + '</div>';
        }
    }

    function row(e, isParent) {
        const el = document.createElement('div');
        el.className = 'sftp-row' + (S.state.sel[e.path] ? ' selected' : '');
        el.dataset.path = e.path;
        if (isParent) {
            e.isParent = true;
            el.dataset.parent = '1';
        }
        el.innerHTML =
            Icons.svg(e.type === 'folder' ? 'folder' : 'file') +
            '<span class="sftp-row-name">' + escapeHtml(e.name) + '</span>' +
            '<span class="sftp-row-meta">' + (e.type === 'file' ? fmtSize(e.size) : '') + '</span>';
        el.addEventListener('click', (ev) => {
            if (isParent || e.type === 'folder') { navigate(e.path); return; }
            // 单击选中; Ctrl 多选 (FM-17)
            if (ev.ctrlKey) {
                if (S.state.sel[e.path]) { delete S.state.sel[e.path]; }
                else { S.state.sel[e.path] = e; }
            } else {
                S.state.sel = { [e.path]: e };
            }
            S.renderSelection();
        });
        el.addEventListener('dblclick', () => {
            if (e.type === 'file') { editSelected(); }
        });
        el.addEventListener('contextmenu', (ev) => {
            ev.preventDefault();
            if (isParent) { SftpMenu.showBlank(ev.clientX, ev.clientY); return; }
            // 文件与文件夹右键都自动选中 (文件夹之前不选中, 直接下载会提示"请先选中")
            if (!S.state.sel[e.path]) {
                S.state.sel = { [e.path]: e };
                S.renderSelection();
            }
            SftpMenu.showRow(ev, e);
        });
        return el;
    }

    // ---- CRUD 动作 (由 SftpMenu 注入) ----
    function renameSelected() {
        const p = S.selectedPath();
        if (!p) { toastMsg('请先选中'); return; }
        const name = window.prompt('重命名为:', p.split('/').pop());
        if (!name || name === p.split('/').pop()) { return; }
        api('/api/ssh/sftp/rename', {
            method: 'POST',
            body: JSON.stringify({ hostId: S.state.hostId, path: p, newName: name }),
        })
            .then(() => { toastMsg('已重命名'); S.state.sel = {}; navigate(S.state.path || '.'); })
            .catch(err => toastMsg('重命名失败: ' + err.message));
    }

    async function chmodSelected() {
        const p = S.selectedPath();
        if (!p) { toastMsg('请先选中'); return; }
        const mode = window.prompt('chmod 权限 (如 755):');
        if (!mode) { return; }
        try {
            await api('/api/ssh/sftp/chmod', {
                method: 'POST',
                body: JSON.stringify({ hostId: S.state.hostId, path: p, mode: mode }),
            });
            toastMsg('已修改权限');
            await navigate(S.state.path);
        } catch (err) { toastMsg('chmod 失败: ' + err.message); }
    }

    async function deleteSelected() {
        const paths = Object.keys(S.state.sel);
        if (!paths.length) { toastMsg('请先选中'); return; }
        if (!await WPCommon.confirm('删除选中的 ' + paths.length + ' 项？(不可恢复)',
                                    { okText: '删除', danger: true })) { return; }
        for (const p of paths) {
            try {
                await api('/api/ssh/sftp/delete', {
                    method: 'POST',
                    body: JSON.stringify({ hostId: S.state.hostId, path: p }),
                });
            } catch (err) { toastMsg('删除失败: ' + err.message); }
        }
        S.state.sel = {};
        await navigate(S.state.path || '.');
    }

    function mkdirPrompt() {
        const name = window.prompt('新建文件夹名称:');
        if (!name) { return; }
        api('/api/ssh/sftp/mkdir', {
            method: 'POST',
            body: JSON.stringify({ hostId: S.state.hostId,
                                   path: S.joinRemote(S.state.path, name) }),
        })
            .then(() => { toastMsg('已创建 ' + name); return navigate(S.state.path); })
            .catch(err => toastMsg('新建失败: ' + err.message));
    }

    // ---- 文本编辑 ----
    async function editSelected() {
        const p = S.selectedPath();
        if (!p) { alert('请先选中文件'); return; }
        try {
            const data = await api('/api/ssh/sftp/read', {
                method: 'POST',
                body: JSON.stringify({ hostId: S.state.hostId, path: p }),
            });
            openEditModal(p, data.content, data.mtime, data.encoding);
        } catch (err) { alert('读取失败: ' + err.message); }
    }

    function openEditModal(path, content, mtime, encoding) {
        editingFile = { hostId: S.state.hostId, path: path, baseMtime: mtime,
                        encoding: encoding || 'utf-8' };
        let modal = document.getElementById('sftp-edit-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'sftp-edit-modal';
            modal.className = 'modal-mask';
            modal.innerHTML =
                '<div class="modal"><div class="modal-title" id="sftp-edit-title"></div>' +
                '<textarea id="sftp-edit-content" spellcheck="false"></textarea>' +
                '<p class="load-error" id="sftp-edit-error" style="display:none;margin-top:8px"></p>' +
                '<div class="modal-actions" style="margin-top:12px">' +
                '<button class="btn btn-primary" id="sftp-edit-save">保存</button>' +
                '<button class="btn btn-ghost" id="sftp-edit-cancel">取消</button></div></div>';
            document.body.appendChild(modal);
            modal.querySelector('#sftp-edit-cancel').addEventListener('click', () => {
                modal.style.display = 'none';
            });
            modal.querySelector('#sftp-edit-save').addEventListener('click', saveEdit);
        }
        modal.querySelector('#sftp-edit-title').textContent = '编辑 — ' + path;
        modal.querySelector('#sftp-edit-content').value = content;
        modal.querySelector('#sftp-edit-error').style.display = 'none';
        modal.style.display = 'flex';
    }

    async function saveEdit() {
        if (!editingFile) { return; }
        const modal = document.getElementById('sftp-edit-modal');
        const content = modal.querySelector('#sftp-edit-content').value;
        try {
            await api('/api/ssh/sftp/write', {
                method: 'POST',
                body: JSON.stringify({
                    hostId: editingFile.hostId, path: editingFile.path,
                    content: content, baseMtime: editingFile.baseMtime,
                    encoding: editingFile.encoding,
                }),
            });
            modal.style.display = 'none';
            toastMsg('已保存 (SSH)');
        } catch (err) {
            modal.querySelector('#sftp-edit-error').textContent = err.message;
            modal.querySelector('#sftp-edit-error').style.display = '';
        }
    }

    // ---- 绑定 ----
    function bind() {
        SftpMenu.init({
            toastMsg: toastMsg,
            actions: {
                download: () => SftpTransfer.downloadSelected(),
                edit: editSelected,
                rename: renameSelected,
                chmod: chmodSelected,
                del: deleteSelected,
                mkdir: mkdirPrompt,
                uploadFile: () => $('sftp-file-input').click(),
                uploadFolder: () => { const i = $('sftp-folder-input'); if (i) { i.click(); } },
            },
        });
        SftpTransfer.init({
            api: api, toastMsg: toastMsg, alert: alert, navigate: navigate,
        });
        SftpSelect.init({ menu: SftpMenu });

        SSHHostStore.subscribe(function () { renderHostOptions(); });
        $('sftp-close-btn').addEventListener('click', togglePanel);
        $('sftp-toggle').addEventListener('click', togglePanel);
        $('sftp-up').addEventListener('click', () => navigate(S.parentPath(S.state.path)));
        const sftpPathEl = $('sftp-path');
        sftpPathEl.title = '点击复制路径';
        sftpPathEl.style.cursor = 'pointer';
        sftpPathEl.addEventListener('click', function () {
            if (!S.state.path || S.state.path === '.') { return; }
            navigator.clipboard.writeText(S.state.path)
                .then(() => toastMsg('路径已复制'))
                .catch(() => toastMsg('复制失败'));
        });
        const pinBtn = $('sftp-pin');
        if (pinBtn) { pinBtn.addEventListener('click', togglePin); }
        SftpSelect.bind($('sftp-list'));
        $('sftp-upload-btn').addEventListener('click', () => $('sftp-file-input').click());
        $('sftp-file-input').addEventListener('change', SftpTransfer.uploadFiles);
        const filesBtn = $('sftp-upload-files');
        if (filesBtn) { filesBtn.addEventListener('click', () => $('sftp-file-input').click()); }
        const folderBtn = $('sftp-upload-folder');
        if (folderBtn) { folderBtn.addEventListener('click', () => $('sftp-folder-input').click()); }
        const folderInput = $('sftp-folder-input');
        if (folderInput) { folderInput.addEventListener('change', SftpTransfer.uploadFiles); }
        $('sftp-mkdir').addEventListener('click', mkdirPrompt);
        $('sftp-edit').addEventListener('click', editSelected);
        $('sftp-chmod').addEventListener('click', chmodSelected);
        $('sftp-download').addEventListener('click', () => SftpTransfer.downloadSelected());
        $('sftp-to-local').addEventListener('click', () => SftpTransfer.toLocalSelected());
    }

    return { bind: bind, renderHostOptions: renderHostOptions };
})(window.SSHUiCtx);
