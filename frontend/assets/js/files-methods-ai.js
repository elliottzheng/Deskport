// 自动拆分自 files.js：FilesMethodsAi
window.FilesMethodsAi = {
        openAsText(e) {
            if (e.type !== 'file') { return; }
            if ((e.size || 0) > 20 * 1024 * 1024) { this.toastMsg('文件超过 20MB，仅可下载'); return; }
            this.openPreview(e, true);
        },

        // ---- 问 AI (文件页与收件箱共用) ----
        openAskAI() {
            const entries = this.selectedPaths.map(p => this.selected[p]).filter(Boolean);
            this.askAIFor(entries);
        },
        askAIFor(entries) {
            const list = (entries || []).filter(e => e &&
                (e.type === 'file' || e.type === 'folder' || e.type === 'drive'));
            if (!list.length) { this.toastMsg('请先选择文件或文件夹'); return; }
            this.aiEntries = list;
            this.aiError = '';
            this.aiPlan = null;
            this.aiPlanLoading = false;
            this.aiDest = '';
            this.aiDestMode = 'new';
            this.aiNote = '';
            // 单个文件夹：直接以它为工作目录启动 AI，不复制、不建案例目录
            this.aiDirect = list.length === 1 && list[0].type === 'folder';
            this.aiDirectPath = this.aiDirect ? list[0].path : '';
            const first = list[0];
            this.aiTask = this.aiDirect ? '' : (first.name || '').replace(/\.[^.]+$/, '').slice(0, 40);
            this.aiOpen = true;
            if (!this.aiCfg) { this.loadAIConfig(); }
            if (!this.aiDirect) { this.scheduleAIPlan(0); }
        },
        // ---- 上传到已定义的 SSH 服务器 (SFTP) ----
        async openUploadHost(paths) {
            const list = (paths && paths.length ? paths
                : (this.selCount ? this.selectedPaths : [])).slice();
            if (!list.length) { this.toastMsg('请先选择文件或文件夹'); return; }
            this.uploadPaths = list;
            this.uploadOpen = true;
            this.uploadError = '';
            this.uploadBusy = false;
            this.uploadRemoteDir = '';
            this.uploadPins = [];
            if (!this.uploadHostId && this.sshHosts.length) {
                this.uploadHostId = this.sshHosts[0].id;
            }
            this.applyUploadMemory();
            try {
                const r = await fetch('/api/ssh/hosts');
                const d = await r.json();
                if (r.ok) {
                    this.sshHosts = d.hosts || [];
                    if (!this.uploadHostId && this.sshHosts.length) {
                        this.uploadHostId = this.sshHosts[0].id;
                    }
                    this.applyUploadMemory();
                }
            } catch (err) { /* 保持空列表提示 */ }
        },
        applyUploadMemory() {
            const mem = window.SftpMemory;
            if (!this.uploadHostId || !mem) { this.uploadPins = []; return; }
            this.uploadPins = mem.getPins(this.uploadHostId);
            const last = mem.getLast(this.uploadHostId);
            this.uploadRemoteDir = (last && last !== '.') ? last : '';
        },
        uploadPinLabel(p) {
            const mem = window.SftpMemory;
            const home = (mem && this.uploadHostId) ? mem.getHome(this.uploadHostId) : '';
            if (p === home || p === '.') { return '登录目录'; }
            const parts = String(p).split('/').filter(Boolean);
            return parts.length ? parts[parts.length - 1] : p;
        },
        selectUploadHost(h) {
            this.uploadHostId = h.id;
            this.applyUploadMemory();
        },
        async startUpload() {
            if (!this.uploadHostId || this.uploadBusy) { return; }
            this.uploadBusy = true;
            this.uploadError = '';
            try {
                const r = await fetch('/api/ssh/sftp/upload-local', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        hostId: this.uploadHostId,
                        remoteDir: this.uploadRemoteDir || '.',
                        paths: this.uploadPaths,
                    }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                if (data.errors && data.errors.length) {
                    this.uploadError = '部分失败：' + data.errors.map(function (e) {
                        const name = e.path.split(/[\\/]/).pop();
                        return name + ': ' + e.error;
                    }).join('；');
                    return;
                }
                const host = this.sshHosts.find(function (h) { return h.id === this.uploadHostId; }.bind(this));
                if (window.SftpMemory) {
                    window.SftpMemory.setLast(this.uploadHostId, this.uploadRemoteDir || '.');
                }
                this.uploadOpen = false;
                this.toastMsg('已上传 ' + data.files + ' 个文件到 ' + (host ? host.name : '服务器'));
            } catch (err) {
                this.uploadError = '上传失败: ' + err.message;
            } finally {
                this.uploadBusy = false;
            }
        },
        async openUploadPicker() {
            if (!this.uploadHostId) { this.toastMsg('请先选择服务器'); return; }
            this.uploadPickerOpen = true;
            this.uploadPickerError = '';
            this.uploadPickerEntries = [];
            await this.loadUploadPicker(this.uploadRemoteDir || '.');
        },
        async loadUploadPicker(path) {
            this.uploadPickerLoading = true;
            this.uploadPickerError = '';
            try {
                const r = await fetch('/api/ssh/sftp/list', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ hostId: this.uploadHostId, path: path }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.uploadPickerPath = data.currentPath || path;
                this.uploadPickerParent = data.parentPath || '';
                this.uploadPickerEntries = (data.entries || []).filter(function (e) {
                    return e.type === 'folder';
                });
            } catch (err) {
                if (path !== '.') { return this.loadUploadPicker('.'); }
                this.uploadPickerError = '读取远程目录失败: ' + err.message;
            } finally {
                this.uploadPickerLoading = false;
            }
        },
        chooseUploadDir() {
            this.uploadRemoteDir = (this.uploadPickerPath === '.' ? '' : this.uploadPickerPath);
            if (window.SftpMemory && this.uploadHostId) {
                window.SftpMemory.setLast(this.uploadHostId, this.uploadPickerPath || '.');
            }
            this.uploadPickerOpen = false;
        },
        async loadAIConfig() {
            try {
                const r = await fetch('/api/ai/config');
                if (r.ok) {
                    this.aiCfg = await r.json();
                    this.aiPickerRoots = this.aiCfg.workDirs || [];
                }
            } catch (err) { /* 忽略 */ }
        },
        scheduleAIPlan(delay) {
            if (!this.aiOpen || this.aiDirect) { return; }
            clearTimeout(this.aiTimer);
            this.aiTimer = setTimeout(() => this.runAIPlan(),
                delay == null ? 450 : delay);
        },
        async runAIPlan() {
            const task = (this.aiTask || '').trim();
            if (!task) { return; }
            const seq = ++this.aiSeq;
            this.aiPlanLoading = true;
            try {
                const names = this.aiEntries.map(e =>
                    e.type === 'folder' ? e.name + '/' : e.name);
                const r = await fetch('/api/ai/plan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task: task, files: names }),
                });
                const data = await r.json();
                if (seq !== this.aiSeq) { return; }
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.aiPlan = data;
                this.aiDest = data.suggestion || (this.aiCfg && this.aiCfg.fallbackDir) || '';
                this.aiDestMode = 'new';
            } catch (err) {
                if (seq === this.aiSeq) {
                    this.aiError = '落点判断失败：' + err.message;
                    this.aiDest = (this.aiCfg && this.aiCfg.fallbackDir) || '';
                }
            } finally {
                if (seq === this.aiSeq) { this.aiPlanLoading = false; }
            }
        },
        openAIPicker() {
            this.aiPickerOpen = true;
            this.loadAIPicker(this.aiDest || (this.aiPickerRoots || [])[0] || '');
        },
        async loadAIPicker(path) {
            if (!path) { return; }
            this.aiPickerPath = path;
            this.aiPickerEntries = [];
            try {
                const r = await fetch('/api/files?path=' + encodeURIComponent(path) + '&hidden=0');
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.aiPickerEntries = (data.entries || [])
                    .filter(e => e.type === 'folder' && !e.name.startsWith('.'));
            } catch (err) {
                this.aiError = '读取目录失败：' + err.message;
            }
        },
        aiPickerUp() {
            if (!this.aiCanUp) { return; }
            const parts = this.aiPickerPath.replace(/[\\/]+$/, '').split(/[\\/]/);
            parts.pop();
            const up = parts.join('\\');
            if (up) { this.loadAIPicker(up); }
        },
        aiPickHere() {
            this.aiDest = this.aiPickerPath;
            this.aiDestMode = 'new';
            this.aiPickerOpen = false;
        },
        modeLabel(mode) {
            return {
                auto: '高置信，直接新建',
                choose: '有多个可能，请选择',
                fallback: '没把握，先放兜底',
            }[mode] || mode;
        },
        async createAICase() {
            if (this.aiCreating) { return; }
            if (this.aiDirect) { return this.launchAIDirect(); }
            const task = (this.aiTask || '').trim();
            if (!task || !this.aiDest) { return; }
            this.aiCreating = true;
            this.aiError = '';
            try {
                const r = await fetch('/api/ai/case', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        task: task,
                        note: this.aiNote,
                        files: this.aiEntries.map(e => e.path),
                        mode: this.aiDestMode,
                        dest: this.aiDest,
                    }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                sessionStorage.setItem('terminal-open', JSON.stringify({
                    cwd: data.terminal.cwd,
                    cmd: data.terminal.cmd,
                    kick: data.terminal.kick,
                }));
                if (window.parent && window.parent !== window) {
                    window.parent.postMessage({ source: WP_MSG.SRC.EMBED, type: WP_MSG.SHELL.OPEN_TERMINAL }, '*');
                } else {
                    location.href = '/terminal';
                }
            } catch (err) {
                this.aiError = err.message;
            } finally {
                this.aiCreating = false;
            }
        },

        // 单文件夹：cwd 就是该文件夹，不复制、不写任务.txt
        async launchAIDirect() {
            if (this.aiCreating || !this.aiDirectPath) { return; }
            this.aiCreating = true;
            this.aiError = '';
            try {
                const r = await fetch('/api/ai/direct', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        path: this.aiDirectPath,
                        task: (this.aiTask || '').trim(),
                        note: (this.aiNote || '').trim(),
                    }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                sessionStorage.setItem('terminal-open', JSON.stringify(data.terminal));
                this.aiOpen = false;
                if (window.parent && window.parent !== window) {
                    window.parent.postMessage({ source: WP_MSG.SRC.EMBED, type: WP_MSG.SHELL.OPEN_TERMINAL }, '*');
                } else {
                    location.href = '/terminal';
                }
            } catch (err) {
                this.aiError = err.message;
            } finally {
                this.aiCreating = false;
            }
        },

        // ---- FM-15 在终端中打开 (跨页交接给 /terminal) ----
        parentDirOf(path) {
            const idx = Math.max(String(path).lastIndexOf('\\'), String(path).lastIndexOf('/'));
            return idx > 0 ? String(path).slice(0, idx) : String(path);
        },
        openEntryInTerminal(entry) {
            if (!entry) { return; }
            const cwd = (entry.type === 'folder' || entry.type === 'drive')
                ? entry.path : this.parentDirOf(entry.path);
            this.openInTerminal(cwd, '');
        },
        openSelectedInTerminal() {
            const p = this.selectedPaths[0];
            if (p && this.selected[p]) { this.openEntryInTerminal(this.selected[p]); }
        },
        openInTerminal(cwd, prog) {
            sessionStorage.setItem('terminal-open', JSON.stringify({ cwd: cwd, cmd: prog }));
            this.ctxMenu = null;
            if (window.parent && window.parent !== window) {
                window.parent.postMessage({ source: WP_MSG.SRC.EMBED, type: WP_MSG.SHELL.OPEN_TERMINAL }, '*');
                return;
            }
            window.location.href = '/terminal';
        },

        // ---- FM-14 复制/移动剪贴板 ----
        async pasteClipboard() {
            const cb = this.clipboard;
            if (!cb || !cb.paths.length) { return; }
            try {
                const r = await fetch('/api/files/paste', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: cb.mode, paths: cb.paths, dest: this.currentPath }),
                });
                const data = await r.json();
                if (!r.ok && !data.done) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.toastMsg((cb.mode === 'copy' ? '已粘贴 ' : '已移动 ') + (data.done || 0) + ' 项'
                    + (data.errors ? '（部分失败）' : ''));
                if (cb.mode === 'move') { this.clipboard = null; }   // 剪切粘贴后清空
                await this.refresh();
            } catch (err) {
                this.toastMsg('粘贴失败: ' + err.message);
            }
        },

        // ---- 快速访问固定 (FM-04) ----
        isPinned(path) { return this.pins.some(p => p.path === path); },
        async togglePin(entry) {
            const baseName = entry.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || entry.path;
            let next;
            if (this.isPinned(entry.path)) {
                next = this.pins.filter(p => p.path !== entry.path);
            } else {
                next = [...this.pins, { path: entry.path, label: baseName }];
                this.toastMsg('已固定到快速访问');
            }
            await this.savePins(next);
        },
        async savePins(next) {
            this.pins = next;
            const r = await fetch('/api/files/pins', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pins: next }),
            });
            if (!r.ok) { this.toastMsg('固定列表保存失败'); }
        },
};
