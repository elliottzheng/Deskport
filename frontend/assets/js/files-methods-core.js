// 自动拆分自 files.js：FilesMethodsCore — 导航、地址栏、排序、重命名/新建。
// 选中与手势已拆到 files-methods-select.js (2026-11-05)。
// 自动拆分自 files.js：FilesMethodsCore
window.FilesMethodsCore = {
        toastMsg(text) {
            this.toast = text;
            clearTimeout(this.toastTimer);
            this.toastTimer = setTimeout(() => { this.toast = ''; }, 2600);
        },
        fmtSize(bytes) {
            return WPCommon.fmtSize(bytes);
        },
        fmtTime(ts) {
            return WPCommon.fmtTime(ts);
        },
        typeText(e) {
            if (e.type === 'drive') { return '磁盘'; }
            if (e.type === 'folder') { return '文件夹'; }
            const dot = e.name.lastIndexOf('.');
            const ext = dot > 0 ? e.name.slice(dot + 1).toUpperCase() : '';
            return ext ? ext + ' 文件' : '文件';
        },
        attrText(e) {
            const parts = [];
            if (e.readonly) { parts.push('只读'); }
            if (e.hidden) { parts.push('隐藏'); }
            if (e.system) { parts.push('系统'); }
            return parts.length ? parts.join('、') : '常规';
        },

        // ---- 联邦成员浏览 ----
        // 给 /api/files* 的请求 URL 追加 machine 参数 (成员模式); 本机原样返回
        fmUrl(base) {
            if (!this.machine) { return base; }
            return base + (base.includes('?') ? '&' : '?')
                + 'machine=' + encodeURIComponent(this.machine);
        },
        async loadMachines() {
            try {
                const r = await fetch('/api/settings/federation');
                const data = await r.json().catch(() => ({}));
                this.machines = (data.fileMachines || []).filter(m => m.id);
                if (this.machine && !this.machines.some(m => m.id === this.machine)) {
                    this.machine = '';   // 策略被关或成员已删: 回本机
                    this.toastMsg('该成员未开放文件浏览，已切回本机');
                    await this.refresh();
                }
            } catch (err) { /* 无联邦配置时仅本机 */ }
        },
        async onMachineChange() {
            // 路径是各机自己的命名空间: 切机器一律回到盘符根重新导航
            this.loc = '';
            this.currentPath = '/';
            this.parentPath = null;
            this.backStack = [];
            this.fwdStack = [];
            this.searchActive = false;
            this.searchQuery = '';
            this.clipboard = null;   // 剪贴板路径不跨机
            this.machineOffline = false;
            this.clearSelection();
            await this.refresh();
        },

        // ---- 导航 ----
        async navigate(path) {
            if (path === this.currentPath && this.loc !== 'inbox') { return; }
            this.loc = '';
            this.backStack.push(this.currentPath || '');
            this.fwdStack = [];
            this.currentPath = path;
            this.searchActive = false;
            this.clearSelection();
            await this.refresh();
        },
        async openInbox() {
            this.loc = 'inbox';
            this.inboxSource = 'all';
            this.inboxKind = 'all';
            this.parentPath = null;
            this.searchActive = false;
            this.currentPath = '';
            this.clearSelection();
            await this.refreshInbox();
        },
        rememberLocation() {
            try {
                if (this.machine) { return; }   // 成员路径不入本机的 localStorage
                if (this.loc === 'inbox') {
                    localStorage.setItem(FM_LOC_KEY, 'inbox');
                } else {
                    localStorage.setItem(FM_LOC_KEY, '');
                    localStorage.setItem(FM_PATH_KEY, this.currentPath || '/');
                }
            } catch (err) { /* ignore */ }
            this.syncUrl();
        },
        syncUrl() {
            try {
                const q = new URLSearchParams(location.search);
                q.set('embed', '1');
                if (this.loc === 'inbox') {
                    q.set('loc', 'inbox');
                    q.delete('path');
                } else {
                    q.delete('loc');
                    if (this.currentPath && this.currentPath !== '/') { q.set('path', this.currentPath); }
                    else { q.delete('path'); }
                }
                if (this.machine) { q.set('machine', this.machine); }
                else { q.delete('machine'); }
                history.replaceState(null, '', location.pathname + '?' + q.toString());
            } catch (err) { /* ignore */ }
            try {
                parent.postMessage({ source: WP_MSG.SRC.EMBED, type: WP_MSG.EMBED.FILES_PATH,
                                     loc: this.loc, path: this.currentPath }, '*');
            } catch (err) { /* ignore */ }
        },
        setInboxFilter(which, value) {
            if (which === 'source') { this.inboxSource = value; }
            else { this.inboxKind = value; }
            this.clearSelection();
        },
        async refresh() {
            if (this.loc === 'inbox') { return this.refreshInbox(); }
            this.loading = true;
            this.error = '';
            try {
                const r = await fetch(this.fmUrl('/api/files?path='
                    + encodeURIComponent(this.currentPath) + '&hidden=1'));
                const data = await r.json();
                if (!r.ok) {
                    if (this.machine) { this.machineOffline = r.status === 502; }
                    // 恢复的路径已失效（删除/改名）: 回退到根目录, 避免一直报错
                    if (this.currentPath && this.currentPath !== '/' && !this._fallbackTried) {
                        this._fallbackTried = true;
                        this.currentPath = '/';
                        this.loading = false;
                        return this.refresh();
                    }
                    this.error = data.error || ('HTTP ' + r.status);
                    this.entries = data.entries || [];
                    this.parentPath = data.parentPath || null;
                    return;
                }
                this._fallbackTried = false;
                this.machineOffline = false;
                this.currentPath = data.currentPath;
                this.parentPath = data.parentPath;
                this.entries = data.entries;
                if (data.currentPath === '/') { this.drivesCache = data.entries; }
                this.clearSelection();
                this.addressInput = data.currentPath;
                this.rememberLocation();
            } catch (err) {
                this.error = '加载失败: ' + err.message;
            } finally {
                this.loading = false;
            }
        },
        async refreshInbox() {
            this.loading = true;
            this.error = '';
            try {
                const r = await fetch('/api/inbox/recent?limit=300');
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.entries = (data.items || []).map(it => ({
                    name: it.name,
                    path: it.path,
                    type: 'file',
                    size: it.size,
                    mtime: it.mtime,
                    hidden: false,
                    system: false,
                    readonly: false,
                    source: it.source,
                    dupes: it.dupes || [],
                    thumb: it.thumb,
                    ext: it.ext,
                    kind: it.kind,
                }));
                this.inboxTotal = data.total || this.entries.length;
                this.inboxEnabled = data.enabled !== false;
                this.sortKey = 'mtime';
                this.sortDir = -1;
                this.parentPath = null;
                this.clearSelection();
                this.addressInput = '收件箱';
                this.rememberLocation();
            } catch (err) {
                this.error = '加载收件箱失败: ' + err.message;
            } finally {
                this.loading = false;
            }
        },
        async goUp() {
            if (this.loc === 'inbox') { return; }
            if (this.parentPath) { await this.navigate(this.parentPath); }
        },
        goBack() {
            if (this.loc === 'inbox' || !this.backStack.length) { return; }
            this.fwdStack.push(this.currentPath);
            this.currentPath = this.backStack.pop();
            this.clearSelection();
            this.refresh();
        },
        goForward() {
            if (this.loc === 'inbox' || !this.fwdStack.length) { return; }
            this.backStack.push(this.currentPath);
            this.currentPath = this.fwdStack.pop();
            this.clearSelection();
            this.refresh();
        },
        editAddress() {
            this.addressInput = this.loc === 'inbox' ? '' : this.currentPath;
            this.addressEditing = true;
            this.$nextTick(() => {
                const el = this.$refs.addressInput;
                if (el) { el.focus(); el.select(); }
            });
        },
        async commitAddress() {
            let p = this.addressInput.trim();
            // 正斜杠统一为反斜杠: 后端按 Win32 路径处理, 混用会导致面包屑不分级
            if (/^[a-zA-Z]:\//.test(p) || p.startsWith('/')) { p = p.replace(/\//g, '\\'); }
            this.addressEditing = false;
            if (p && p !== this.currentPath) { await this.navigate(p); }
        },

        // ---- 地址栏: 空白处单击进入编辑 (Windows 资源管理器语义), 长按复制 ----
        crumbBarClick(e) {
            if (this.addressLongPress) { this.addressLongPress = false; return; }
            if (e.target.closest('.crumb-seg') || e.target.closest('input')) { return; }
            this.editAddress();
        },
        addressSwallowClick(e) {
            // 长按后的合成 click 会先落到子按钮上, 在捕获阶段吞掉
            if (this.addressLongPress) {
                e.preventDefault();
                e.stopPropagation();
                this.addressLongPress = false;
            }
        },
        copyAddress() {
            if (this.loc === 'inbox' || !this.currentPath) {
                this.toastMsg('收件箱没有可复制的路径');
                return;
            }
            this.copyToClipboard(this.currentPath, '路径已复制');
        },
        addressPressStart(e) {
            if (this.loc === 'inbox' || !e.touches.length) { return; }
            const t = e.touches[0];
            this.addressPressXY = [t.clientX, t.clientY];
            this.addressPressTimer = setTimeout(() => {
                this.addressPressTimer = null;
                this.addressLongPress = true;
                this.copyAddress();
            }, 500);
        },
        addressPressMove(e) {
            if (!this.addressPressTimer || !this.addressPressXY || !e.touches.length) { return; }
            const t = e.touches[0];
            if (Math.abs(t.clientX - this.addressPressXY[0]) > 10
                || Math.abs(t.clientY - this.addressPressXY[1]) > 10) {
                clearTimeout(this.addressPressTimer);
                this.addressPressTimer = null;
            }
        },
        addressPressEnd() {
            if (this.addressPressTimer) { clearTimeout(this.addressPressTimer); this.addressPressTimer = null; }
        },
        toggleMobileNav() {   // MOB-04: 移动端快速访问抽屉
            this.mobileNav = !this.mobileNav;
        },
        exitSelMode() {
            this.selMode = false;
            this.clearSelection();
        },
        rowClick(e, idx, ev) {
            if (this.suppressClick) { this.suppressClick = false; return; }   // 长按后的合成 click
            if (this.isMobile && !this.selMode) {
                this.openEntry(e);          // 手机单击直接打开
                return;
            }
            if (ev.shiftKey && this.anchorIdx !== null) {
                const list = this.visibleEntries;
                const lo = Math.min(this.anchorIdx, idx);
                const hi = Math.max(this.anchorIdx, idx);
                const next = { ...this.selected };
                for (let i = lo; i <= hi; i += 1) { next[list[i].path] = list[i]; }
                this.selected = next;
                return;
            }
            if (ev.ctrlKey || ev.metaKey) {
                this.toggleSelect(e, idx);
                return;
            }
            if (this.isMobile && this.selMode) {
                this.toggleSelect(e, idx);
                return;
            }
            this.selected = { [e.path]: e };
            this.anchorIdx = idx;
        },
        rowDblClick(e) {
            if (this.isMobile) { return; }
            if (e.type === 'folder' || e.type === 'drive') { this.navigate(e.path); return; }
            // 双击默认预览 (FM-07); 不可预览类型维持下载
            if (this.isPreviewable(e)) { this.openPreview(e); return; }
            this.doDownloadOne(e.path);
        },
        openEntry(e) {
            if (e.type === 'folder' || e.type === 'drive') { this.navigate(e.path); }
            else { this.actionTarget = e; }
        },

        // ---- 排序 ----
        sortBy(key) {
            if (this.sortKey === key) { this.sortDir *= -1; }
            else { this.sortKey = key; this.sortDir = 1; }
        },
        toggleView() {
            this.viewMode = this.viewMode === 'details' ? 'icons' : 'details';
            localStorage.setItem(FM_VIEW_KEY, this.viewMode);
        },

        // ---- 重命名 / 新建 ----
        startNewFolder() {
            if (this.currentPath === '/') { this.toastMsg('请先进入某个磁盘'); return; }
            this.nameMode = 'new-folder';
            this.nameTarget = this.currentPath;
            this.nameValue = '新建文件夹';
            this.nameError = '';
            this.nameModal = true;
            this.$nextTick(() => { const el = this.$refs.nameInput; if (el) { el.select(); } });
        },
        startRename() {
            const sel = this.selectedPaths;
            if (sel.length !== 1) { this.toastMsg('请选中一个项目'); return; }
            this.nameMode = 'rename';
            this.nameTarget = this.selected[sel[0]].path;
            this.nameValue = this.selected[sel[0]].name;
            this.nameError = '';
            this.nameModal = true;
            this.$nextTick(() => {
                const el = this.$refs.nameInput;
                if (el) { el.focus(); const dot = el.value.lastIndexOf('.'); el.setSelectionRange(0, dot > 0 ? dot : el.value.length); }
            });
        },
        startRenameFrom(entry) {
            this.actionTarget = null;
            this.nameMode = 'rename';
            this.nameTarget = entry.path;
            this.nameValue = entry.name;
            this.nameError = '';
            this.nameModal = true;
        },
        async submitName() {
            const name = this.nameValue.trim();
            if (!name) { this.nameError = '名称不能为空'; return; }
            if (name.split('').some(c => ILLEGAL.includes(c))) {
                this.nameError = '名称含非法字符 \\ / : * ? " < > |';
                return;
            }
            try {
                if (this.nameMode === 'rename') {
                    const r = await fetch('/api/files/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: this.nameTarget, newName: name }),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.toastMsg('已重命名');
                } else if (this.nameMode === 'compress') {
                    const r = await fetch('/api/files/compress', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ paths: this.compressTargets || this.selectedPaths, parent: this.nameTarget, name: name }),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.toastMsg('已压缩: ' + data.path);
                } else {
                    const r = await fetch('/api/files/new-folder', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: this.nameTarget, name: name }),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.toastMsg('已创建文件夹');
                }
                this.nameModal = false;
                await this.refresh();
            } catch (err) {
                this.nameError = err.message;
            }
        },

        // ---- 删除 (回收站) / 下载 ----
};
