// 自动拆分自 files.js：FilesMethodsUi
window.FilesMethodsUi = {
    // ---- 右键菜单 ----
        rowContext(e, ev) {
            if (this.isMobile) { return; }
            if (!this.isSelected(e)) { this.selected = { [e.path]: e }; }
            this.ctxMenu = { x: ev.clientX, y: ev.clientY, entry: e };
            this.$nextTick(() => this.clampCtxMenu());   // FM-02: 靠近视口边缘时收拢进可视区
        },
        clampCtxMenu() {
            const el = this.$refs.ctxMenu;
            if (!el || !this.ctxMenu) { return; }
            const r = el.getBoundingClientRect();
            let { x, y } = this.ctxMenu;
            if (x + r.width > window.innerWidth - 8) { x = Math.max(8, window.innerWidth - r.width - 8); }
            if (y + r.height > window.innerHeight - 8) { y = Math.max(8, window.innerHeight - r.height - 8); }
            if (x !== this.ctxMenu.x || y !== this.ctxMenu.y) { this.ctxMenu = { ...this.ctxMenu, x, y }; }
        },
        ctxAction(action, termProg) {
            const entry = this.ctxMenu ? this.ctxMenu.entry : null;
            this.ctxMenu = null;
            if (!entry) { return; }
            // 多选状态下(右键项属于选区) 下载/压缩/删除作用于整个选区 (Explorer 语义)
            const batch = this.selCount > 1 && this.isSelected(entry) ? this.selectedPaths : [entry.path];
            if (action === 'open') { this.openEntry(entry); }
            else if (action === 'preview') { this.openPreview(entry); }
            else if (action === 'text') { this.openAsText(entry); }          // FM-16 用记事本打开
            else if (action === 'download') { this.downloadPaths(batch); }   // 文件夹自动走 ZIP (FM-09)
            else if (action === 'compress') { this.startCompressPaths(batch); }
            else if (action === 'extract') { this.extractPaths([entry.path]); }
            else if (action === 'pin') { this.togglePin(entry); }
            else if (action === 'copy' || action === 'cut') {   // FM-14
                this.clipboard = { mode: action === 'copy' ? 'copy' : 'move', paths: batch.slice() };
                this.toastMsg(action === 'copy' ? '已复制 ' + batch.length + ' 项' : '已标记移动 ' + batch.length + ' 项，请进入目标目录粘贴');
            }
            else if (action === 'copypath') {
                navigator.clipboard.writeText(entry.path)
                    .then(() => this.toastMsg('路径已复制'))
                    .catch(() => this.toastMsg('复制失败'));
            }
            else if (action === 'term') {   // FM-15 在终端中打开 (termProg 由菜单项第二参数传入)
                const progMap = { pwsh7: 'pwsh.exe', cmd: 'cmd.exe', powershell: 'powershell.exe' };
                const cwd = (entry.type === 'folder' || entry.type === 'drive')
                    ? entry.path : this.parentDirOf(entry.path);
                this.openInTerminal(cwd, progMap[termProg] || '');
            }
            else if (action === 'ai') { this.askAIFor([entry]); }
            else if (action === 'upload-host') { this.openUploadHost(batch); }
            else if (action === 'start') { this.doStart(entry.path); }
            else if (action === 'rename') { this.startRenameFrom(entry); }
            else if (action === 'props') { this.propsTarget = entry; }
            else if (action === 'delete') { this.deletePaths(batch); }
        },
        // FM-15: 空白处右键 (当前目录)
        listContext(ev) {
            if (this.isMobile) { return; }
            if (ev.target.closest('.fm-row')) { return; }   // 行上右键走 rowContext
            this.ctxMenu = { x: ev.clientX, y: ev.clientY, entry: { type: 'dir', name: this.currentPath, path: this.currentPath } };
            this.$nextTick(() => this.clampCtxMenu());
        },

        // ---- 类型判断与图标 (FM-06) / 预览 (FM-05, 档位A) ----
        extOf(e) {
            const m = (e.name || '').match(/\.([^.]+)$/);
            return m ? m[1].toLowerCase() : '';
        },
        typeIcon(e) {
            if (e.type === 'folder' || e.type === 'drive') { return Icons.svg('folder'); }
            const ext = this.extOf(e);
            const map = {
                archive: ['zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'tgz'],
                disc: ['iso', 'img', 'vhd', 'vhdx', 'wim', 'mds'],
                table: ['csv', 'xls', 'xlsx', 'tsv', 'ods'],
                image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico'],
                music: ['mp3', 'wav', 'flac', 'm4a', 'ogg', 'aac'],
                video: ['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm'],
                exe: ['exe', 'msi', 'bat', 'cmd', 'ps1', 'com', 'apk'],
                fileText: ['txt', 'md', 'json', 'log', 'xml', 'yml', 'yaml', 'ini', 'cfg', 'conf', 'html', 'htm', 'css', 'js', 'mjs', 'py', 'java', 'c', 'cpp', 'h', 'cs', 'go', 'rs', 'sql', 'sh', 'pdf'],
            };
            for (const [icon, exts] of Object.entries(map)) {
                if (exts.includes(ext)) { return Icons.svg(icon); }
            }
            return Icons.svg('file');
        },
        isTextFile(e) {
            return this.typeIcon(e) === Icons.svg('fileText');
        },
        isExeFile(e) {
            return ['exe', 'msi', 'bat', 'cmd'].includes(this.extOf(e));
        },
        isArchiveEntry(e) {
            if (e.type !== 'file') { return false; }
            const ext = this.extOf(e);
            return ['zip', 'tar', 'gz', 'tgz', 'bz2', 'xz'].some(a => ext === a || e.name.toLowerCase().endsWith('.tar.' + a));
        },
        isPreviewable(e) {
            if (e.type !== 'file') { return false; }
            const ext = this.extOf(e);
            return TEXT_EXTS.includes(ext) || IMAGE_EXTS.includes(ext);
        },
        async openPreview(e, forceText) {
            if (e.type === 'file' && (e.size || 0) > 20 * 1024 * 1024) {
                this.toastMsg('文件超过 20MB，仅可下载');   // FM-18
                return;
            }
            this.previewError = '';
            this.previewSaved = false;
            this.previewTruncated = false;
            this.previewLossy = false;
            this.previewEncoding = 'utf-8';
            const ext = this.extOf(e);
            if (!forceText && this.isImageEntry(e)) {
                this.previewMode = 'image';
                this.previewTarget = e;
                this.previewSrc = '/api/files/thumb?path=' + encodeURIComponent(e.path);   // FM-13 预览用缩略图
                return;
            }
            this.previewMode = 'text';
            this.previewTarget = e;
            this.previewContent = '加载中…';
            try {
                const r = await fetch('/api/files/read', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: e.path }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || 'HTTP ' + r.status); }
                this.previewContent = data.content;
                this.previewTruncated = !!data.truncated;
                this.previewLossy = !!data.lossy;
                this.previewEncoding = data.encoding || 'utf-8';
            } catch (err) {
                this.previewContent = '';
                this.previewError = '无法预览：' + err.message;
            }
        },
        async savePreview() {
            if (this.previewLossy) { this.toastMsg('二进制文件只读，不能保存'); return; }
            try {
                const r = await fetch('/api/files/write', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: this.previewTarget.path, content: this.previewContent,
                                           encoding: this.previewEncoding }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || 'HTTP ' + r.status); }
                this.previewSaved = true;
                setTimeout(() => { this.previewSaved = false; }, 2000);
            } catch (err) {
                alert('保存失败: ' + err.message);
            }
        },
        closePreview() { this.previewTarget = null; },

        // ---- FM-13 缩略图 ----
        isImageEntry(e) {
            return e.type === 'file' && IMAGE_EXTS.includes(this.extOf(e));
        },
        thumbUrl(e) {
            return '/api/files/thumb?path=' + encodeURIComponent(e.path);
        },

        // ---- FM-16/18 打开方式: 强制文本 (乱码原文可见, 有损只读) ----
};
