// 文件管理: 选中与手势 — 桌面鼠标圈选 (FM-10)、移动端长按多选、剪贴板。
// 拆自 files-methods-core.js (2026-11-05): 原 523 行把导航、选中手势、
// 地址栏、重命名混在一个对象里, 这里只留"怎么选中"这一件事。
window.FilesMethodsSelect = {
        // ---- 图标视图鼠标圈选 (FM-10, 桌面) ----
        startMarquee(e) {
            if (this.viewMode !== 'icons' || this.isMobile) { return; }
            if (e.button !== 0) { return; }
            if (e.target.closest('.fm-row')) { return; }   // 瓦片上不启动, 只在空白处
            const list = e.currentTarget;
            const rect = list.getBoundingClientRect();
            this.marquee = {
                show: true,
                x1: e.clientX, y1: e.clientY,
                left: e.clientX - rect.left, top: e.clientY - rect.top,
                width: 0, height: 0,
                additive: e.ctrlKey || e.metaKey,
                base: { ...this.selected },
                listRect: rect,
                listEl: list,
            };
            this._mm = this.marqueeMove.bind(this);
            this._me = this.marqueeEnd.bind(this);
            window.addEventListener('mousemove', this._mm);
            window.addEventListener('mouseup', this._me);
        },
        marqueeMove(e) {
            const m = this.marquee;
            if (!m.show) { return; }
            const left = Math.min(m.x1, e.clientX), top = Math.min(m.y1, e.clientY);
            const width = Math.abs(e.clientX - m.x1), height = Math.abs(e.clientY - m.y1);
            m.left = left - m.listRect.left;
            m.top = top - m.listRect.top;
            m.width = width; m.height = height;
            // 命中: 瓦片矩形与圈选矩形(视口坐标)相交
            const box = { l: left, t: top, r: left + width, b: top + height };
            const sel = m.additive ? { ...m.base } : {};
            m.listEl.querySelectorAll('.fm-row').forEach(row => {
                const r = row.getBoundingClientRect();
                const hit = r.left < box.r && r.right > box.l && r.top < box.b && r.bottom > box.t;
                if (hit) {
                    const p = row.getAttribute('data-path');
                    const en = this.visibleEntries.find(x => x.path === p);
                    if (en) { sel[p] = en; }
                }
            });
            this.selected = sel;
        },
        marqueeEnd() {
            window.removeEventListener('mousemove', this._mm);
            window.removeEventListener('mouseup', this._me);
            this.marquee = { ...this.marquee, show: false };
        },

        isSelected(e) { return !!this.selected[e.path]; },
        clearSelection() {
            this.selected = {};
            this.selMode = false;
            this.anchorIdx = null;
        },
        toggleSelect(e, idx) {
            if (this.selected[e.path]) { delete this.selected[e.path]; }
            else { this.selected[e.path] = e; this.anchorIdx = idx; }
            this.selected = { ...this.selected };   // 触发响应式
        },
        selectAll() {
            const next = {};
            this.visibleEntries.forEach(e => { next[e.path] = e; });
            this.selected = next;
        },
        invertSelect() {
            const next = {};
            this.visibleEntries.forEach(e => { if (!this.isSelected(e)) { next[e.path] = e; } });
            this.selected = next;
        },
        batchCopyMove(mode) {   // 移动端多选: 复制/移动 (去目标目录粘贴)
            if (!this.selCount) { return; }
            this.clipboard = { mode: mode, paths: this.selectedPaths.slice() };
            this.toastMsg((mode === 'copy' ? '已复制 ' : '已标记移动 ') + this.selCount + ' 项，请进入目标目录粘贴');
            this.exitSelMode();
        },
        copyPath(path) {
            this.copyToClipboard(path, '路径已复制');
        },
        // 剪贴板: 优先 navigator.clipboard (需安全上下文), 局域网 HTTP 回退 execCommand
        async copyToClipboard(text, okMsg) {
            try {
                await navigator.clipboard.writeText(text);
                this.toastMsg(okMsg);
                return;
            } catch (err) { /* 回退 execCommand */ }
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            let ok = false;
            try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
            ta.remove();
            this.toastMsg(ok ? okMsg : '复制失败，请改用地址栏编辑后复制');
        },

        // ---- 长按 (移动端进入多选) ----
        touchStart(e, ev) {
            if (!this.isMobile) { return; }
            if (ev.target.closest('input,button')) { return; }
            const touch = ev.touches[0];
            this.touchStartPos = { x: touch.clientX, y: touch.clientY };
            clearTimeout(this.touchTimer);
            this.touchTimer = setTimeout(() => {
                this.touchTimer = null;
                this.suppressClick = true;
                // 长按后再松手会补一个 click, 必须吞掉, 否则刚选中的项又被切掉
                setTimeout(() => { this.suppressClick = false; }, 700);
                const idx = this.visibleEntries.indexOf(e);
                if (!this.selMode) {
                    this.selMode = true;
                    if (!this.isSelected(e)) { this.toggleSelect(e, idx); }
                } else if (!this.isSelected(e)) {
                    this.toggleSelect(e, idx);
                }
                if (navigator.vibrate) { try { navigator.vibrate(15); } catch (err) { /* ignore */ } }
            }, 500);
        },
        // 空白处长按: 打开当前目录菜单 (移动端没有右键)
        listTouchStart(ev) {
            if (!this.isMobile) { return; }
            if (ev.target.closest('.fm-row')) { return; }
            const touch = ev.touches[0];
            this.touchStartPos = { x: touch.clientX, y: touch.clientY };
            clearTimeout(this.touchTimer);
            this.touchTimer = setTimeout(() => {
                this.touchTimer = null;
                this.suppressClick = true;
                setTimeout(() => { this.suppressClick = false; }, 700);
                this.blankOpenedAt = Date.now();
                this.blankSheet = true;
            }, 500);
        },
        touchMove(ev) {
            if (this.touchTimer && this.touchStartPos) {
                const t = ev.touches[0];
                if (Math.abs(t.clientX - this.touchStartPos.x) > 10 ||
                    Math.abs(t.clientY - this.touchStartPos.y) > 10) {
                    clearTimeout(this.touchTimer);
                    this.touchTimer = null;
                }
            }
        },
        touchEnd() {
            clearTimeout(this.touchTimer);
            this.touchTimer = null;
            this.touchStartPos = null;
        },
        closeBlankSheet() {
            if (Date.now() - this.blankOpenedAt < 350) { return; }   // 吞掉长按后的合成 click
            this.blankSheet = false;
        },
        blankNewFolder() { this.blankSheet = false; this.startNewFolder(); },
        blankPaste() { this.blankSheet = false; this.pasteClipboard(); },
        blankSelectAll() { this.blankSheet = false; this.selMode = true; this.selectAll(); },
        blankRefresh() { this.blankSheet = false; this.refresh(); },
        blankTerminal() { this.blankSheet = false; this.openInTerminal(this.currentPath, ''); },

};
