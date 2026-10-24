// SSH SFTP 面板：状态、渲染、框选、上传下载、菜单与文本编辑。
window.SSHSftpUI = (function (ctx) {
    'use strict';
    const $ = ctx.$;
    const api = ctx.api;
    const toastMsg = ctx.toastMsg;
    const escapeHtml = WPCommon.escapeHtml;
    const fmtSize = WPCommon.fmtSize;

    let sftpHostId = null;
    let sftpPath = '/';
    let sftpHomePath = '';
    let sftpEntries = [];
    let sftpSel = {};   // path → entry (FM-17 多选)

    function renderSftpSelection() {
        document.querySelectorAll('#sftp-list .sftp-row').forEach(r => {
            r.classList.toggle('selected', !!sftpSel[r.dataset.path]);
        });
    }
    // FM-17 桌面框选: 空白处拖拽圈选 (触屏跳过, 走长按多选)
    let sftpMarquee = null;
    function sftpMarqueeStart(e) {
        if (e.button !== 0) { return; }
        if (e.target.closest('.sftp-row')) { return; }
        if (window.matchMedia('(hover: none) and (pointer: coarse)').matches) { return; }
        const list = e.currentTarget;
        sftpMarquee = {
            x1: e.clientX, y1: e.clientY,
            listEl: list, listRect: list.getBoundingClientRect(),
            additive: e.ctrlKey || e.metaKey,
            base: Object.assign({}, sftpSel),
            moved: false,
        };
        const box = document.createElement('div');
        box.className = 'sftp-marquee';
        list.appendChild(box);
        sftpMarquee.box = box;
        window.addEventListener('mousemove', sftpMarqueeMove);
        window.addEventListener('mouseup', sftpMarqueeEnd);
        e.preventDefault();
    }
    function sftpMarqueeMove(e) {
        const m = sftpMarquee;
        if (!m) { return; }
        const left = Math.min(m.x1, e.clientX), top = Math.min(m.y1, e.clientY);
        const width = Math.abs(e.clientX - m.x1), height = Math.abs(e.clientY - m.y1);
        m.box.style.left = (left - m.listRect.left) + 'px';
        m.box.style.top = (top - m.listRect.top) + 'px';
        m.box.style.width = width + 'px';
        m.box.style.height = height + 'px';
        if (width + height > 6) { m.moved = true; }
        const boxRect = { l: left, t: top, r: left + width, b: top + height };
        const sel = m.additive ? Object.assign({}, m.base) : {};
        m.listEl.querySelectorAll('.sftp-row').forEach(row => {
            const r = row.getBoundingClientRect();
            if (r.left < boxRect.r && r.right > boxRect.l && r.top < boxRect.b && r.bottom > boxRect.t) {
                const p = row.dataset.path;
                const en = sftpEntries.find(x => x.path === p);
                if (en) { sel[p] = en; }
            }
        });
        sftpSel = sel;
        renderSftpSelection();
    }
    function sftpMarqueeEnd() {
        window.removeEventListener('mousemove', sftpMarqueeMove);
        window.removeEventListener('mouseup', sftpMarqueeEnd);
        if (sftpMarquee && sftpMarquee.box) { sftpMarquee.box.remove(); }
        // 空白处单击(未拖动) = 清空选择 (Explorer 语义)
        if (sftpMarquee && !sftpMarquee.moved) { sftpSel = {}; renderSftpSelection(); }
        sftpMarquee = null;
    }
    function selectedSftpPath() {
        const keys = Object.keys(sftpSel);
        return keys.length ? keys[0] : null;
    }

    let editingFile = null;

    function renderSftpHostOptions() {
        const sel = $('sftp-host-select');
        if (!sel) { return; }
        sel.innerHTML = '';
        ctx.getHosts().forEach(h => {
            const opt = document.createElement('option');
            opt.value = h.id;
            opt.textContent = h.name;
            if (h.id === sftpHostId) { opt.selected = true; }
            sel.appendChild(opt);
        });
        sel.onchange = function () {
            sftpHostId = this.value;
            sftpPath = SftpMemory.getLast(sftpHostId) || '.';
            $('sftp-host-label').textContent = hostLabel(sftpHostId);
            renderSftpQuick();
            sftpNavigate(sftpPath);
        };
    }

    function toggleSftpPanel() {
        const panel = $('sftp-panel');
        const opening = panel.style.display !== 'flex';
        panel.style.display = opening ? 'flex' : 'none';
        const foot = $('sftp-foot-toggle');
        if (foot) { foot.textContent = opening ? 'SFTP 文件:开' : 'SFTP 文件:关'; }
        if (!opening) { return; }
        // 默认目标: 当前活跃 SSH 会话的主机; 否则第一台主机
        if (!sftpHostId) {
            const active = window.termManager && window.termManager.getActive();
            if (active && active.kind === 'ssh' && active.hostId) {
                sftpHostId = active.hostId;
            } else if (ctx.getHosts().length) {
                sftpHostId = ctx.getHosts()[0].id;
            }
        }
        $('sftp-host-label').textContent = hostLabel(sftpHostId);
        if (!sftpPath || sftpPath === '/') {
            sftpPath = SftpMemory.getLast(sftpHostId) || '.';
        }
        renderSftpQuick();
        sftpNavigate(sftpPath);
    }

    function hostLabel(hostId) {
        const h = ctx.getHosts().find(x => x.id === hostId);
        return h ? h.name : (hostId || '');
    }

    // ---- SFTP 快速访问（当前路径可固定、可取消） ----
    function sftpDisplayPath(p) {
        if (p === sftpHomePath || p === '.') { return '登录目录'; }
        const parts = p.split('/').filter(Boolean);
        return parts.length ? parts[parts.length - 1] : p;
    }

    function renderSftpQuick() {
        const box = $('sftp-quick');
        if (!box) { return; }
        const pins = SftpMemory.getPins(sftpHostId);
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
            btn.className = 'sftp-quick-chip' + (p === sftpPath ? ' active' : '');
            btn.textContent = sftpDisplayPath(p);
            btn.title = p;
            btn.addEventListener('click', function () { sftpNavigate(p); });
            btn.addEventListener('contextmenu', function (ev) {
                ev.preventDefault();
                if (confirm('取消固定 ' + p + ' ？')) {
                    SftpMemory.unpin(sftpHostId, p);
                    renderSftpQuick();
                }
            });
            box.appendChild(btn);
        });
        updatePinStar();
    }

    function updatePinStar() {
        const btn = $('sftp-pin');
        if (!btn) { return; }
        const pinned = SftpMemory.isPinned(sftpHostId, sftpPath);
        btn.textContent = pinned ? '★' : '☆';
        btn.title = pinned ? '取消固定当前目录' : '固定当前目录到快速访问';
    }

    function toggleSftpPin() {
        if (!sftpHostId || !sftpPath) { return; }
        if (SftpMemory.isPinned(sftpHostId, sftpPath)) {
            SftpMemory.unpin(sftpHostId, sftpPath);
        } else {
            SftpMemory.pin(sftpHostId, sftpPath);
        }
        renderSftpQuick();
    }

    function parentPath(p) {
        const parent = posixDirname(p);
        return parent || '/';
    }

    function posixDirname(p) {
        const idx = p.lastIndexOf('/');
        if (idx <= 0) { return '/'; }
        return p.slice(0, idx);
    }

    async function sftpNavigate(path) {
        sftpPath = path || '.';
        $('sftp-path').textContent = sftpPath;
        renderSftpQuick();
        const box = $('sftp-list');
        box.innerHTML = '<div class="empty-tip">加载中…</div>';
        try {
            const data = await api('/api/ssh/sftp/list', {
                method: 'POST',
                body: JSON.stringify({ hostId: sftpHostId, path: sftpPath }),
            });
            sftpEntries = data.entries || [];
            sftpSel = {};
            sftpPath = data.currentPath || sftpPath;
            sftpHomePath = data.homePath || sftpHomePath;
            if (sftpHomePath) { SftpMemory.setHome(sftpHostId, sftpHomePath); }
            $('sftp-path').textContent = sftpPath;
            SftpMemory.setLast(sftpHostId, sftpPath);
            renderSftpQuick();
            box.innerHTML = '';
            if (sftpPath !== '/' && data.parentPath !== null) {
                box.appendChild(sftpRow({ name: '..', path: data.parentPath, type: 'folder', size: 0 }, true));
            }
            sftpEntries.forEach(e => { box.appendChild(sftpRow(e, false)); });
            if (!sftpEntries.length) {
                box.innerHTML = '<div class="empty-tip">空目录</div>';
            }
        } catch (err) {
            box.innerHTML = '<div class="empty-tip">' + err.message + '</div>';
        }
    }

    function sftpRow(e, isParent) {
        const row = document.createElement('div');
        row.className = 'sftp-row' + (sftpSel[e.path] ? ' selected' : '');
        row.dataset.path = e.path;
        row.innerHTML =
            Icons.svg(e.type === 'folder' ? 'folder' : 'file') +
            '<span class="sftp-row-name">' + escapeHtml(e.name) + '</span>' +
            '<span class="sftp-row-meta">' + (e.type === 'file' ? fmtSize(e.size) : '') + '</span>';
        row.addEventListener('click', (ev) => {
            if (isParent) { sftpNavigate(e.path); return; }
            if (e.type === 'folder') { sftpNavigate(e.path); return; }
            // 单击选中; Ctrl 多选 (FM-17)
            if (ev.ctrlKey) {
                if (sftpSel[e.path]) { delete sftpSel[e.path]; } else { sftpSel[e.path] = e; }
            } else {
                sftpSel = { [e.path]: e };
            }
            renderSftpSelection();
        });
        row.addEventListener('dblclick', () => {
            if (e.type === 'file') { sftpEditSelected(); }
        });
        row.addEventListener('contextmenu', (ev) => {
            ev.preventDefault();
            if (e.type === 'file' && !sftpSel[e.path]) { sftpSel = { [e.path]: e }; renderSftpSelection(); }
            showSftpMenu(ev, e);
        });
        return row;
    }

    async function sftpDownloadSelected() {
        // 多选: 逐个经 a[download] 触发 (不开新标签); 未选中: 当前目录
        const paths = Object.keys(sftpSel);
        if (!paths.length) { paths.push(sftpPath !== '/' ? sftpPath : null); }
        if (!paths[0]) { alert('请先选中文件'); return; }
        for (const p of paths) {
            const a = document.createElement('a');
            a.href = '/api/ssh/sftp/download?hostId=' + encodeURIComponent(sftpHostId)
                + '&path=' + encodeURIComponent(p);
            a.download = '';
            document.body.appendChild(a);
            a.click();
            a.remove();
        }
    }
    async function sftpToLocalSelected() {
        const p = selectedSftpPath() || (sftpPath !== '/' ? sftpPath : null);
        if (!p) { alert('请先选中文件'); return; }
        try {
            const data = await api('/api/ssh/sftp/to-local', {
                method: 'POST',
                body: JSON.stringify({ hostId: sftpHostId, remotePath: p }),
            });
            alert('已存到本机: ' + data.localPath);
        } catch (err) { alert('转存失败: ' + err.message); }
    }
    async function sftpUploadFiles(ev) {
        const files = Array.from(ev.target.files || []);
        ev.target.value = '';
        if (!files.length) { return; }
        for (const file of files) {
            const fd = new FormData();
            fd.append('hostId', sftpHostId);
            fd.append('remoteDir', sftpPath);
            fd.append('file', file);
            const r = await fetch('/api/ssh/sftp/upload', { method: 'POST', body: fd });
            if (!r.ok) { alert('上传失败: ' + file.filename); }
        }
        await sftpNavigate(sftpPath);
    }

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------
    let sftpMenuEl = null;
    function showSftpMenu(ev, entry) {
        closeSftpMenu();
        const batch = Object.keys(sftpSel).length > 1 && sftpSel[entry.path] ? Object.values(sftpSel) : [entry];
        const single = batch.length === 1 ? batch[0] : null;
        const menu = document.createElement('div');
        menu.className = 'ctx-menu';
        menu.style.left = Math.min(ev.clientX, innerWidth - 170) + 'px';
        menu.style.top = Math.min(ev.clientY, innerHeight - 260) + 'px';
        const item = (label, fn) => {
            const b = document.createElement('button');
            b.className = 'ctx-item';
            b.textContent = label;
            b.addEventListener('click', () => { closeSftpMenu(); fn(); });
            menu.appendChild(b);
        };
        if (single && single.type === 'file') {
            item('下载', sftpDownloadSelected);
            item('预览 / 编辑', sftpEditSelected);
            item('重命名', sftpRenameSelected);
            item('chmod', sftpChmodSelected);
        } else {
            item('下载 (' + batch.length + ' 项)', sftpDownloadSelected);
        }
        item('删除 (' + batch.length + ' 项)', sftpDeleteSelected);
        item('复制路径', () => {
            navigator.clipboard.writeText(single ? single.path : batch.map(x => x.path).join('\n'))
                .then(() => toastMsg('路径已复制')).catch(() => {});
        });
        document.body.appendChild(menu);
        sftpMenuEl = menu;
        setTimeout(() => document.addEventListener('click', closeSftpMenu, { once: true }), 0);
    }
    function closeSftpMenu() {
        if (sftpMenuEl) { sftpMenuEl.remove(); sftpMenuEl = null; }
    }
    function sftpRenameSelected() {
        const p = selectedSftpPath();
        if (!p) { toastMsg('请先选中'); return; }
        const name = window.prompt('重命名为:', p.split('/').pop());
        if (!name || name === p.split('/').pop()) { return; }
        api('/api/ssh/sftp/rename', { method: 'POST', body: JSON.stringify({ hostId: sftpHostId, path: p, newName: name }) })
            .then(() => { toastMsg('已重命名'); sftpSel = {}; sftpNavigate(sftpPath || '.'); })
            .catch(err => toastMsg('重命名失败: ' + err.message));
    }
    function sftpChmodSelected() {
        const p = selectedSftpPath();
        if (!p) { toastMsg('请先选中'); return; }
        const mode = window.prompt('chmod 权限 (如 755):');
        if (!mode) { return; }
        api('/api/ssh/sftp/chmod', { method: 'POST', body: JSON.stringify({ hostId: sftpHostId, path: p, mode: mode }) })
            .then(() => { toastMsg('已修改权限'); sftpNavigate(sftpPath || '.'); })
            .catch(err => toastMsg('chmod 失败: ' + err.message));
    }
    function sftpDeleteSelected() {
        const paths = Object.keys(sftpSel);
        if (!paths.length) { toastMsg('请先选中'); return; }
        if (!window.confirm('删除选中的 ' + paths.length + ' 项？(不可恢复)')) { return; }
        (async () => {
            for (const p of paths) {
                try { await api('/api/ssh/sftp/delete', { method: 'POST', body: JSON.stringify({ hostId: sftpHostId, path: p }) }); }
                catch (err) { toastMsg('删除失败: ' + err.message); }
            }
            sftpSel = {};
            sftpNavigate(sftpPath || '.');
        })();
    }

    async function sftpMkdirPrompt() {
        const name = window.prompt('新建文件夹名称:');
        if (!name) { return; }
        try {
            await api('/api/ssh/sftp/mkdir', {
                method: 'POST',
                body: JSON.stringify({ hostId: sftpHostId, path: joinRemote(sftpPath, name) }),
            });
            await sftpNavigate(sftpPath);
        } catch (err) { alert('新建失败: ' + err.message); }
    }

    async function sftpChmodPrompt() {
        const p = selectedSftpPath();
        if (!p) { alert('请先选中文件或文件夹'); return; }
        const mode = window.prompt('权限 (八进制, 如 644 / 755):');
        if (!mode) { return; }
        try {
            await api('/api/ssh/sftp/chmod', {
                method: 'POST',
                body: JSON.stringify({ hostId: sftpHostId, path: p, mode: mode }),
            });
            await sftpNavigate(sftpPath);
        } catch (err) { alert('chmod 失败: ' + err.message); }
    }

    async function sftpEditSelected() {
        const p = selectedSftpPath();
        if (!p) { alert('请先选中文件'); return; }
        try {
            const data = await api('/api/ssh/sftp/read', {
                method: 'POST',
                body: JSON.stringify({ hostId: sftpHostId, path: p }),
            });
            openEditModal(p, data.content, data.mtime, data.encoding);
        } catch (err) { alert('读取失败: ' + err.message); }
    }

    function openEditModal(path, content, mtime, encoding) {
        editingFile = { hostId: sftpHostId, path: path, baseMtime: mtime,
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
            if (typeof toastMsg === 'function') { toastMsg('已保存 (SSH)'); }
        } catch (err) {
            modal.querySelector('#sftp-edit-error').textContent = err.message;
            modal.querySelector('#sftp-edit-error').style.display = '';
        }
    }

    function joinRemote(dir, name) {
        return (dir === '/' ? '' : dir) + '/' + name;
    }

    function bind() {
        SSHHostStore.subscribe(function () { renderSftpHostOptions(); });
        $('sftp-close-btn').addEventListener('click', toggleSftpPanel);
        $('sftp-toggle').addEventListener('click', toggleSftpPanel);
        // 移动端工具条只留菜单+状态, SFTP 入口放到侧栏脚部
        const sftpFoot = $('sftp-foot-toggle');
        if (sftpFoot) { sftpFoot.addEventListener('click', toggleSftpPanel); }
        $('sftp-up').addEventListener('click', () => sftpNavigate(parentPath(sftpPath)));
        const pinBtn = $('sftp-pin');
        if (pinBtn) { pinBtn.addEventListener('click', toggleSftpPin); }
        $('sftp-list').addEventListener('mousedown', sftpMarqueeStart);   // FM-17 桌面框选
        $('sftp-upload-btn').addEventListener('click', () => $('sftp-file-input').click());
        $('sftp-file-input').addEventListener('change', sftpUploadFiles);
        $('sftp-mkdir').addEventListener('click', sftpMkdirPrompt);
        $('sftp-edit').addEventListener('click', sftpEditSelected);
        $('sftp-chmod').addEventListener('click', sftpChmodPrompt);
        $('sftp-download').addEventListener('click', sftpDownloadSelected);
        $('sftp-to-local').addEventListener('click', sftpToLocalSelected);
    }

    return { bind: bind, renderHostOptions: renderSftpHostOptions };
})(window.SSHUiCtx);
