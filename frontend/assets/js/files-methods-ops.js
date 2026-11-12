// 自动拆分自 files.js：FilesMethodsOps
window.FilesMethodsOps = {
        async deletePaths(paths) {
            if (this.machine) { this.toastMsg('成员文件只读，暂不支持删除'); return; }
            if (!paths.length) { return; }
            try {
                const r = await fetch('/api/files/recycle-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: paths }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.toastMsg('已移入回收站 (' + paths.length + ' 项)');
                this.clearSelection();
                await this.refresh();
            } catch (err) {
                this.toastMsg('删除失败: ' + err.message);
            }
        },
        deleteSelected() {
            if (!this.selCount) { return; }
            return this.deletePaths(this.selectedPaths);
        },
        async downloadPaths(paths) {
            if (this.machine && paths.length > 1) {
                // 多选拢 ZIP 要在成员机上写文件, 第一版只读不放行
                this.toastMsg('成员模式暂只支持单个文件下载');
                return;
            }
            if (paths.length === 1) {
                const entry = this.entries.find(e => e.path === paths[0]);
                if (entry && entry.type === 'file') {
                    // 移动端/PWA 里 window.open 下载附件会被拦或打开空白页, 统一用 <a download>
                    this.doDownloadOne(paths[0]);
                    return;
                }
            }
            this.busyZip = true;
            try {
                const r = await fetch('/api/files/zip', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: paths }),
                });
                if (!r.ok) {
                    const data = await r.json().catch(() => ({}));
                    throw new Error(data.error || ('HTTP ' + r.status));
                }
                const blob = await r.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'files.zip';
                a.click();
                setTimeout(() => URL.revokeObjectURL(a.href), 5000);
            } catch (err) {
                this.toastMsg('打包下载失败: ' + err.message);
            } finally {
                this.busyZip = false;
            }
        },
        downloadSelected() {
            if (this.selCount) { return this.downloadPaths(this.selectedPaths); }
        },
        doDownloadOne(path) {
            const a = document.createElement('a');
            a.href = this.fmUrl('/api/files/download?path=' + encodeURIComponent(path));
            a.download = '';
            document.body.appendChild(a);
            a.click();
            a.remove();
            if (this.isMobile) { this.toastMsg('开始下载…'); }
        },
        async doStart(path) {
            if (this.machine) { this.toastMsg('成员文件只读，暂不支持启动'); return; }
            try {
                const r = await fetch('/api/files/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: path }),
                });
                const data = await r.json();
                this.toastMsg(r.ok ? '已启动' : '启动失败: ' + data.error);
            } catch (err) {
                this.toastMsg('启动失败: ' + err.message);
            }
        },

        // ---- 上传 ----
        pickLocalFile() { this.$refs.fileInput.click(); },
        async uploadLocal(ev) {
            const files = Array.from(ev.target.files || []);
            ev.target.value = '';
            if (!files.length) { return; }
            if (this.machine) { this.toastMsg('成员文件只读，暂不支持上传'); return; }
            // 「此电脑」根视图没有真实目录, 后端会 400; 这里提前说明, 别让用户
            // 选完文件才看到一句"无效的保存目录"
            if (!this.canUpload) {
                this.toastMsg('请先进入某个磁盘或文件夹再上传');
                return;
            }
            const box = WPCommon.uploadBox, post = WPCommon.postProgress;
            const saveDir = this.useTempDir ? '' : this.currentPath;
            this.busy = true;
            box.open('上传文件');
            let okCount = 0;
            const failed = [];
            for (let i = 0; i < files.length; i++) {
                const file = files[i];
                const label = files.length > 1
                    ? (i + 1) + '/' + files.length + ' · ' + WPCommon.fmtSize(file.size)
                    : WPCommon.fmtSize(file.size);
                box.progress({ file: file.name, label: label, percent: 0 });
                try {
                    const fd = new FormData();
                    fd.append('file', file);
                    fd.append('path', saveDir);
                    fd.append('execute', this.executeAfter ? 'true' : 'false');
                    fd.append('tempdir', this.useTempDir ? 'true' : 'false');
                    const data = await post('/api/files/upload', fd, function (loaded, total) {
                        box.progress({ file: file.name, label: label,
                                       percent: Math.round(loaded / total * 100) });
                    });
                    okCount += 1;
                    box.progress({ file: file.name, label: label, percent: 100 });
                    if (data.warn) { this.toastMsg(data.warn); }
                } catch (err) {
                    failed.push(file.name + ': ' + err.message);
                    box.progress({ file: file.name, label: label, percent: 100 });
                }
            }
            this.busy = false;
            if (failed.length) {
                box.finish(false, '上传失败 ' + failed.length + ' 个');
                this.toastMsg('上传失败: ' + failed[0]);
            } else {
                box.finish(true, '已上传 ' + okCount + ' 个文件');
            }
            await this.refresh();
        },

        // ---- L2: 解压 / 压缩 / 搜索 / 拖拽上传 ----
        isArchivePath(path) {
            const lower = (path || '').toLowerCase();
            return ['.zip', '.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar']
                .some(ext => lower.endsWith(ext));
        },
        async startExtract() {
            if (!this.canExtract) { return; }
            await this.extractPaths(this.selectedPaths);
        },
        async extractPaths(paths) {
            if (!paths.length) { return; }
            if (this.machine) { this.toastMsg('成员文件只读，暂不支持解压'); return; }
            this.busy = true;
            try {
                const r = await fetch('/api/files/extract', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ archivePath: paths[0] }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.toastMsg('已解压 ' + data.count + ' 项');
                await this.refresh();
            } catch (err) {
                this.toastMsg('解压失败: ' + err.message);
            } finally {
                this.busy = false;
            }
        },
        startCompress() {
            if (!this.selCount) { return; }
            this.startCompressPaths(this.selectedPaths);
        },
        startCompressPaths(paths) {
            if (!paths.length) { return; }
            this.compressTargets = paths;
            this.nameMode = 'compress';
            this.nameTarget = this.currentPath;
            this.nameValue = 'archive';
            this.nameError = '';
            this.nameModal = true;
        this.$nextTick(() => { const el = this.$refs.nameInput; if (el) { el.select(); } });
    },
    async startSearch() {
        const q = this.searchQuery.trim();
        if (!q) { await this.closeSearch(); return; }
        if (this.loc === 'inbox') {
            this.searchActive = true;      // 收件箱本地过滤
            this.clearSelection();
            return;
        }
        this.loading = true;
        try {
            const r = await fetch(this.fmUrl('/api/files/search?path=' + encodeURIComponent(this.currentPath)
                + '&q=' + encodeURIComponent(q)));
            const data = await r.json();
            this.entries = data.results;
            this.searchActive = true;
            this.searchTruncated = !!data.truncated;
            this.clearSelection();
        } catch (err) {
            this.toastMsg('搜索失败: ' + err.message);
        } finally {
            this.loading = false;
        }
    },
    async closeSearch() {
        this.searchActive = false;
        this.searchQuery = '';
        await this.refresh();
    },
    async dropUpload(ev) {
        this.dragOver = false;
        const files = Array.from((ev.dataTransfer && ev.dataTransfer.files) || []);
        if (!files.length) { return; }
        if (this.machine) {
            this.toastMsg('成员文件只读，暂不支持上传');
            return;
        }
        if (!this.canUpload) {
            this.toastMsg('请先进入某个磁盘或文件夹再上传');
            return;
        }
        const box = WPCommon.uploadBox, post = WPCommon.postProgress;
        this.busy = true;
        box.open('拖入上传');
        let okCount = 0;
        const failed = [];
        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            const label = files.length > 1
                ? (i + 1) + '/' + files.length + ' · ' + WPCommon.fmtSize(file.size)
                : WPCommon.fmtSize(file.size);
            box.progress({ file: file.name, label: label, percent: 0 });
            try {
                const fd = new FormData();
                fd.append('file', file);
                fd.append('path', this.useTempDir ? '' : this.currentPath);
                const data = await post('/api/files/upload', fd, function (loaded, total) {
                    box.progress({ file: file.name, label: label,
                                   percent: Math.round(loaded / total * 100) });
                });
                okCount += 1;
                box.progress({ file: file.name, label: label, percent: 100 });
                if (data.warn) { this.toastMsg(data.warn); }
            } catch (err) {
                failed.push(file.name + ': ' + err.message);
                box.progress({ file: file.name, label: label, percent: 100 });
            }
        }
        this.busy = false;
        if (failed.length) {
            box.finish(false, '拖入上传失败 ' + failed.length + ' 个');
            this.toastMsg('拖入上传失败: ' + failed[0]);
        } else {
            box.finish(true, '已上传 ' + okCount + ' 个文件');
        }
        await this.refresh();
    },

};
