// 文件管理页 — 资源管理器式 (桌面) / 文件管理器式 (移动端)
// 方法按领域拆到 files-methods-*.js，本文件只做装配。
const { createApp } = Vue;

createApp({
    data() {
        return {
            entries: [],
            currentPath: _initialPath(),
            parentPath: null,
            pins: [],
            drivesCache: [],
            loading: false,
            busy: false,
            busyZip: false,
            error: '',
            useTempDir: false,
            executeAfter: false,
        searchActive: false,   // L2: 搜索结果模式
        searchTruncated: false,
        dragOver: false,
        searchQuery: '',

            selected: {},          // { path: entry }
            anchorIdx: null,
            selMode: false,        // 移动端多选模式 (长按进入)

            backStack: [],
            fwdStack: [],

            sortKey: 'name',
            sortDir: 1,
            viewMode: localStorage.getItem(FM_VIEW_KEY) || 'details',
            showHidden: localStorage.getItem(FM_HIDDEN_KEY) === '1',
            navCollapsed: localStorage.getItem(FM_NAV_KEY) === '1',

            addressEditing: false,
            addressInput: '',
            isMobile: window.matchMedia('(hover: none) and (pointer: coarse)').matches,

            nameModal: false,
            nameMode: 'new-folder',   // new-folder | rename
            nameTarget: null,          // rename 的原路径
            nameValue: '',
            nameError: '',
            propsTarget: null,
            actionTarget: null,
            confirmDelete: false,

            ctxMenu: null,             // {x, y, entry}
            touchTimer: null,
            touchStartPos: null,
            longPressFired: false,     // 长按已触发: 吞掉随后的合成 click
            suppressClick: false,
            blankSheet: false,         // 移动端空白处长按的目录菜单
            blankOpenedAt: 0,          // 长按后浏览器会补一个 click, 用它挡住遮罩自关

            toast: '',
            toastTimer: null,
            previewTarget: null,
            previewMode: '',
            previewContent: '',
            previewSrc: '',
            previewTruncated: false,
            previewSaved: false,
            previewError: '',
            previewLossy: false,
            previewEncoding: 'utf-8',
            clipboard: null,   // {mode: 'copy'|'move', paths: []}
            marquee: { show: false },
            compressTargets: null,
            mobileNav: false,

            sshHosts: [],
            uploadOpen: false,
            uploadPaths: [],
            uploadHostId: '',
            uploadRemoteDir: '',
            uploadBusy: false,
            uploadError: '',
            uploadPins: [],
            uploadPickerOpen: false,
            uploadPickerPath: '.',
            uploadPickerParent: '',
            uploadPickerEntries: [],
            uploadPickerError: '',
            uploadPickerLoading: false,

            loc: _initialLoc(),
            inboxTotal: 0,
            inboxEnabled: true,
            inboxSource: 'all',
            inboxKind: 'all',

            aiOpen: false,
            aiEntries: [],
            aiTask: '',
            aiNote: '',
            aiPlan: null,
            aiPlanLoading: false,
            aiDest: '',
            aiDestMode: 'new',
            aiDirect: false,
            aiDirectPath: '',
            aiCreating: false,
            aiError: '',
            aiTimer: null,
            aiSeq: 0,
            aiPickerOpen: false,
            aiPickerRoots: [],
            aiPickerPath: '',
            aiPickerEntries: [],
            aiCfg: null,
        };
    },
    computed: {
        drives() { return this.drivesCache; },
        quickList() { return this.pins; },
        visibleEntries() {
            let list = this.entries;
            if (!this.showHidden) {
                list = list.filter(e => !e.hidden && !e.system);
            }
            if (this.searchMode) {
                const q = this.searchQuery.toLowerCase();
                list = list.filter(e => e.name.toLowerCase().includes(q));
            }
            if (this.loc === 'inbox') {
                if (this.inboxSource !== 'all') {
                    list = list.filter(e => (e.source || '其他') === this.inboxSource);
                }
                if (this.inboxKind !== 'all') {
                    list = list.filter(e => (e.kind || 'other') === this.inboxKind);
                }
            }
            const dir = this.sortDir;
            const key = this.sortKey;
            return list.slice().sort((a, b) => {
                if (a.type !== b.type) { return a.type === 'folder' || a.type === 'drive' ? -1 : 1; }
                let r = 0;
                if (key === 'name') { r = a.name.toLowerCase().localeCompare(b.name.toLowerCase(), 'zh-CN'); }
                else if (key === 'size') { r = a.size - b.size; }
                else if (key === 'mtime') { r = a.mtime - b.mtime; }
                else if (key === 'type') {
                    const ea = a.name.includes('.') ? a.name.split('.').pop().toLowerCase() : '';
                    const eb = b.name.includes('.') ? b.name.split('.').pop().toLowerCase() : '';
                    r = ea.localeCompare(eb);
                }
                return r * dir;
            });
        },
        inboxSourceOptions() {
            const map = {};
            this.entries.forEach(e => { const s = e.source || '其他'; map[s] = (map[s] || 0) + 1; });
            const out = [{ value: 'all', label: '全部', count: this.entries.length }];
            Object.keys(map).sort((a, b) => map[b] - map[a])
                .forEach(s => out.push({ value: s, label: s, count: map[s] }));
            return out;
        },
        inboxKindOptions() {
            const labels = { image: '图片', pdf: 'PDF', doc: '文档', sheet: '表格',
                             slide: '演示', archive: '压缩包', media: '音视频', other: '其他' };
            const map = {};
            this.entries.forEach(e => { const k = e.kind || 'other'; map[k] = (map[k] || 0) + 1; });
            const out = [{ value: 'all', label: '全部', count: this.entries.length }];
            ['image', 'pdf', 'doc', 'sheet', 'slide', 'archive', 'media', 'other'].forEach(k => {
                if (map[k]) { out.push({ value: k, label: labels[k], count: map[k] }); }
            });
            return out;
        },
        selectedPaths() {
            return Object.keys(this.selected);
        },
        selCount() { return this.selectedPaths.length; },
        selectedSize() {
            return this.selectedPaths.reduce((sum, p) => sum + (this.selected[p] ? this.selected[p].size : 0), 0);
        },
        crumbs() {
            if (this.loc === 'inbox') { return [{ label: '收件箱', path: '/files?loc=inbox' }]; }
            const p = this.currentPath || '/';
            if (p === '/') { return [{ label: '此电脑', path: '/' }]; }
            const parts = p.split('\\').filter(Boolean);
            const crumbs = [{ label: '此电脑', path: '/' }];
            let acc = '';
            parts.forEach((seg, i) => {
                acc = i === 0 ? seg + '\\' : acc + seg + '\\';
                crumbs.push({ label: seg, path: acc });
            });
            return crumbs;
        },
        canExtract() {
            if (this.selCount !== 1) { return false; }
            return this.isArchivePath(this.selectedPaths[0]);
        },
        searchMode() { return this.searchActive; },   // L2
        aiDestPreview() {
            if (!this.aiDest) { return ''; }
            if (this.aiDestMode === 'existing') { return this.aiDest; }
            const d = new Date();
            const p = n => String(n).padStart(2, '0');
            const today = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
            return this.aiDest.replace(/[\\/]+$/, '') + '\\' + today + ' ' + (this.aiTask || '未命名任务');
        },
        aiCanUp() {
            return !!this.aiPickerPath &&
                !(this.aiPickerRoots || []).some(r => r.toLowerCase() === this.aiPickerPath.toLowerCase());
        },
    },
    watch: {
        aiTask() {
            if (this.aiOpen && !this.aiDirect) { this.scheduleAIPlan(); }
        },
    },
    methods: Object.assign({},
        window.FilesMethodsCore,
        window.FilesMethodsOps,
        window.FilesMethodsUi,
        window.FilesMethodsAi),
    mounted() {
        // 地址栏编辑失焦即提交 (enter 已绑定)
        if (this.loc === 'inbox') { this.openInbox(); } else { this.refresh(); }
        // 侧栏「此电脑」独立加载, 收件箱模式下也要能直接跳回磁盘
        fetch('/api/files?path=%2F').then(r => r.json())
            .then(d => { if (d && d.entries) { this.drivesCache = d.entries; } })
            .catch(() => {});
        fetch('/api/files/pins').then(r => r.json()).then(d => { this.pins = d.pins || []; }).catch(() => {});
        window.addEventListener('scroll', () => { this.ctxMenu = null; }, true);
        window.addEventListener('resize', () => { this.ctxMenu = null; });

        // 键盘快捷键 (输入框聚焦时跳过)
        document.addEventListener('keydown', (e) => {
            const tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || this.nameModal) { return; }
            if (e.key === 'F5') { e.preventDefault(); this.refresh(); }
            else if (e.key === 'Delete') { this.deleteSelected(); }
            else if (e.key === 'F2') { this.startRename(); }
            else if (e.key === 'Escape') { this.clearSelection(); this.ctxMenu = null; }
            else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
                e.preventDefault();
                this.selectAll();
            }
        });
        // 点击空白关闭右键菜单
        document.addEventListener('click', () => { this.ctxMenu = null; });
        window.addEventListener('resize', () => {
            this.isMobile = window.matchMedia('(hover: none) and (pointer: coarse)').matches;
        });
    },
}).mount('#app');
