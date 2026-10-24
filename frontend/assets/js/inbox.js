// 收件箱 — 选文件 → 判落点 → 建案例 → 拉起 AI
const { createApp } = Vue;

createApp({
    data() {
        return {
            items: [],
            dirs: [],
            total: 0,
            loading: false,
            error: '',
            q: '',
            filter: '全部',
            sel: [],
            cfg: { workDirs: [], fallbackDir: '', aiCommand: '', detectedCommands: [] },
            taskOpen: false,
            task: '',
            note: '',
            plan: null,
            planLoading: false,
            dest: '',
            destMode: 'new',
            pickerOpen: false,
            pickerRoots: [],
            pickerPath: '',
            pickerEntries: [],
            creating: false,
            planTimer: null,
            planSeq: 0,
        };
    },
    computed: {
        sources() {
            const out = ['全部'];
            this.dirs.forEach(d => { if (!out.includes(d.source)) { out.push(d.source); } });
            return out;
        },
        visible() {
            const q = this.q.toLowerCase();
            return this.items.filter(it => {
                if (this.filter !== '全部' && it.source !== this.filter) { return false; }
                return !q || it.name.toLowerCase().includes(q);
            });
        },
        selItems() { return this.sel.map(id => this.itemById(id)).filter(Boolean); },
        today() {
            const d = new Date();
            const p = n => String(n).padStart(2, '0');
            return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
        },
        destPreview() {
            if (!this.dest) { return ''; }
            if (this.destMode === 'existing') { return this.dest; }
            return this.dest.replace(/[\\/]+$/, '') + '\\' + this.today + ' ' + (this.task || '未命名任务');
        },
        canUp() {
            const roots = this.cfg.workDirs || [];
            return !!this.pickerPath && !roots.some(r => r.toLowerCase() === this.pickerPath.toLowerCase());
        },
    },
    watch: {
        task() { this.schedulePlan(); },
        visible() { this.$nextTick(() => this.injectIcons()); },
        pickerOpen(v) { if (v) { this.$nextTick(() => this.injectIcons()); } },
    },
    mounted() {
        this.cfg.workDirs = [];
        this.load();
        this.loadCfg();
        this.$nextTick(() => this.injectIcons());
    },
    methods: {
        injectIcons() {
            const root = document.getElementById('app');
            if (root && window.Icons) { Icons.injectAll(root); }
        },
        async load() {
            this.loading = true;
            this.error = '';
            try {
                const r = await fetch('/api/inbox/recent?limit=120');
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.items = data.items || [];
                this.dirs = data.dirs || [];
                this.total = data.total || 0;
                const alive = new Set(this.items.map(i => i.id));
                this.sel = this.sel.filter(id => alive.has(id));
            } catch (err) {
                this.error = err.message;
            } finally {
                this.loading = false;
                this.$nextTick(() => this.injectIcons());
            }
        },
        async loadCfg() {
            try {
                const r = await fetch('/api/ai/config');
                if (r.ok) {
                    this.cfg = await r.json();
                    this.pickerRoots = this.cfg.workDirs || [];
                }
            } catch (err) { /* 保持默认 */ }
        },
        itemById(id) { return this.items.find(i => i.id === id); },
        isSel(it) { return this.sel.includes(it.id); },
        toggle(it) {
            const i = this.sel.indexOf(it.id);
            if (i >= 0) { this.sel.splice(i, 1); } else { this.sel.push(it.id); }
        },
        fmtSize(n) {
            return WPCommon.fmtSize(n);
        },
        fmtTime(ts) {
            return WPCommon.fmtAgo(ts);
        },
        iconOf(it) {
            const ext = (it.ext || '').toLowerCase();
            if (it.kind === 'image') { return 'image'; }
            if (it.kind === 'sheet') { return 'table'; }
            if (it.kind === 'archive') { return 'archive'; }
            if (it.kind === 'media') { return ['.mp3', '.wav', '.m4a'].includes(ext) ? 'music' : 'video'; }
            if (ext === '.exe' || ext === '.msi') { return 'exe'; }
            if (it.kind === 'other') { return 'file'; }
            return 'fileText';
        },
        modeLabel(mode) {
            return { auto: '高置信，直接新建', choose: '有多个可能，请选择', fallback: '没把握，先放兜底' }[mode] || mode;
        },
        openTask() {
            if (!this.sel.length) { return; }
            this.taskOpen = true;
            this.error = '';
            this.plan = null;
            const first = this.selItems[0];
            if (first) {
                // 任务名是目录名的一部分, 太长会撑爆落点预览和目录名
                this.task = first.name.replace(/\.[^.]+$/, '').slice(0, 40);
            }
            this.dest = '';
            this.destMode = 'new';
            this.schedulePlan(0);
        },
        schedulePlan(delay) {
            if (!this.taskOpen) { return; }
            clearTimeout(this.planTimer);
            this.planTimer = setTimeout(() => this.runPlan(), delay == null ? 450 : delay);
        },
        async runPlan() {
            const task = this.task.trim();
            if (!task) { return; }
            const seq = ++this.planSeq;
            this.planLoading = true;
            try {
                const r = await fetch('/api/ai/plan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task: task, files: this.selItems.map(i => i.name) }),
                });
                const data = await r.json();
                if (seq !== this.planSeq) { return; }
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.plan = data;
                this.dest = data.suggestion || this.cfg.fallbackDir || '';
                this.destMode = 'new';
            } catch (err) {
                if (seq === this.planSeq) {
                    this.error = '落点判断失败：' + err.message;
                    this.dest = this.cfg.fallbackDir || '';
                }
            } finally {
                if (seq === this.planSeq) { this.planLoading = false; }
            }
        },
        chooseAlt(path) {
            this.dest = path;
            this.destMode = 'new';
        },
        openPicker() {
            this.pickerOpen = true;
            this.loadPicker(this.dest || (this.cfg.workDirs || [])[0] || '');
        },
        async loadPicker(path) {
            if (!path) { return; }
            this.pickerPath = path;
            this.pickerEntries = [];
            try {
                const r = await fetch('/api/files?path=' + encodeURIComponent(path) + '&hidden=0');
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.pickerEntries = (data.entries || []).filter(e => e.type === 'folder' && !e.name.startsWith('.'));
            } catch (err) {
                this.error = '读取目录失败：' + err.message;
            }
            this.$nextTick(() => this.injectIcons());
        },
        pickerUp() {
            if (!this.canUp) { return; }
            const parts = this.pickerPath.replace(/[\\/]+$/, '').split(/[\\/]/);
            parts.pop();
            const up = parts.join('\\');
            if (up) { this.loadPicker(up); }
        },
        pickHere() {
            this.dest = this.pickerPath;
            this.destMode = 'new';
            this.pickerOpen = false;
        },
        async create() {
            if (this.creating) { return; }
            const task = this.task.trim();
            if (!task || !this.dest) { return; }
            this.creating = true;
            this.error = '';
            try {
                const r = await fetch('/api/ai/case', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        task: task,
                        note: this.note,
                        files: this.selItems.map(i => i.path),
                        mode: this.destMode,
                        dest: this.dest,
                    }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                sessionStorage.setItem('terminal-open', JSON.stringify({
                    cwd: data.terminal.cwd,
                    cmd: data.terminal.cmd,
                    kick: data.terminal.kick,
                }));
                location.href = '/terminal';
            } catch (err) {
                this.error = err.message;
            } finally {
                this.creating = false;
            }
        },
    },
}).mount('#app');