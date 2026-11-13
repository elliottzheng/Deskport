// 任务页 — 常驻 ACP 会话：对话流、继续追问、授权卡片、完整日志
(function () {
    'use strict';
    const { createApp } = Vue;

    createApp({
        data() {
            return {
                prompt: '',
                mode: 'auto',
                runWhere: 'local',
                nodes: [],
                remoteCwd: '',
                localCwd: '',
                cwdRoots: [],
                paths: [],
                pathInput: '',
                pathMenuOpen: false,
                pathMenuLoaded: false,
                quickDirs: [],
                recentPaths: [],
                tasks: [],
                selectedId: null,
                detail: null,
                events: [],
                after: 0,
                busy: false,
                message: '',
                sending: false,
                modeBusy: false,
                fullEvents: {},
                error: '',
                notice: '',
                pickerOpen: false,
                pickerSource: 'all',
                inboxItems: [],
                picked: {},
                listTimer: null,
                detailTimer: null,
                autoSelected: false,
            };
        },
        computed: {
            isLocalRun() {
                return this.runWhere === 'local';
            },
            remoteNodes() {
                return this.nodes.filter((n) => n.kind !== 'local');
            },
            isRunning() {
                return !!this.detail && ['queued', 'running'].indexOf(this.detail.status) >= 0;
            },
            isWaiting() {
                return !!this.detail && this.detail.status === 'waiting';
            },
            isPaused() {
                return !!this.detail && this.detail.status === 'paused';
            },
            canSend() {
                if (!this.detail) { return false; }
                const s = this.detail.status;
                if (['waiting', 'paused'].indexOf(s) >= 0) { return true; }
                return ['failed', 'interrupted'].indexOf(s) >= 0 && !!this.detail.canResume;
            },
            canEnd() {
                return !!this.detail && ['waiting', 'paused', 'running', 'queued'].indexOf(this.detail.status) >= 0;
            },
            statusHint() {
                if (!this.detail) { return ''; }
                const s = this.detail.status;
                if (this.detail.awaitingAnswer && s === 'waiting') { return 'Kimi 在等你回答上面的问题。'; }
                if (s === 'paused') { return '会话已休眠，发送消息会自动接续原来的上下文。'; }
                if (s === 'failed' || s === 'interrupted') {
                    return this.detail.canResume
                        ? '会话中断了，发送消息会从断点接续。'
                        : '会话不可继续，请新建任务。';
                }
                if (s === 'closed') { return '会话已结束，不能再继续。'; }
                if (s === 'running') {
                    const n = this.detail.queuedMessages || 0;
                    return n > 0
                        ? '这一轮执行中；已有 ' + n + ' 条消息排队，本轮结束后自动送达。'
                        : '这一轮执行中，新消息会排队，本轮结束后自动送达。';
                }
                return '';
            },
            pendingPermission() { return this.detail && this.detail.pendingPermission || ''; },
            pickedCount() { return Object.keys(this.picked).length; },
            pickerSources() {
                const map = {};
                this.inboxItems.forEach(it => { const s = it.source || '其他'; map[s] = (map[s] || 0) + 1; });
                const out = [{ value: 'all', label: '全部', count: this.inboxItems.length }];
                Object.keys(map).sort((a, b) => map[b] - map[a])
                    .forEach(s => out.push({ value: s, label: s, count: map[s] }));
                return out;
            },
            pickerItems() {
                if (this.pickerSource === 'all') { return this.inboxItems; }
                return this.inboxItems.filter(it => (it.source || '其他') === this.pickerSource);
            },
            logUrl() {
                return '/api/tasks/' + encodeURIComponent(this.selectedId || '') + '/log?download=1';
            },
            cwdLine() {
                const d = this.detail;
                if (!d) { return ''; }
                const node = d.node || 'local';
                if (node !== 'local') { return '执行点 ' + node + (d.cwd ? ' · ' + d.cwd : ''); }
                if (!d.cwd || !d.workspace) { return ''; }
                const norm = (p) => String(p).replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
                return norm(d.cwd) === norm(d.workspace) ? '' : '本机 · ' + d.cwd;
            },
        },
        mounted() {
            this.loadRecent();
            this.loadCwdRoots();
            this.refresh();
            this.loadNodes();
            this.listTimer = setInterval(this.refresh, 2000);
            this.detailTimer = setInterval(this.pollDetail, 1000);
        },
        beforeUnmount() {
            clearInterval(this.listTimer);
            clearInterval(this.detailTimer);
        },
        methods: {
            async refresh() {
                if (document.visibilityState !== 'visible') { return; }
                try {
                    const r = await fetch('/api/tasks?full=1');
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.tasks = data.tasks || [];
                    if (!this.selectedId && !this.autoSelected && this.tasks.length) {
                        this.autoSelected = true;
                        this.select(this.tasks[0]);
                    } else if (this.selectedId && this.detail) {
                        const id = this.selectedId;
                        const cur = this.tasks.find(function (t) { return t.id === id; });
                        if (cur) {
                            // R4-0 列表是精简摘要：只合并存在的键，
                            // 缺的字段（resultFiles/canContinue…）由 loadEvents 全量轮询补
                            const patch = {};
                            ['status', 'resultFiles', 'error', 'lastText', 'canContinue',
                             'pendingPermission', 'canResume', 'awaitingAnswer', 'mode',
                             'lastActiveLabel', 'queuedMessages'].forEach(function (k) {
                                if (cur[k] !== undefined) { patch[k] = cur[k]; }
                            });
                            this.detail = Object.assign({}, this.detail, patch);
                        }
                    }
                } catch (err) { /* 保持当前列表 */ }
            },
            async select(task) {
                this.selectedId = task.id;
                this.detail = task;
                this.events = [];
                this.after = 0;
                this.message = '';
                this.notice = '';
                this.fullEvents = {};
                this.loadDetail();   // R4-0 精简列表缺 resultFiles/createdLabel 等，详情端点补全
                await this.loadEvents();
            },
            async loadDetail() {
                if (!this.selectedId) { return; }
                try {
                    const r = await fetch('/api/tasks/' + encodeURIComponent(this.selectedId));
                    if (!r.ok) { return; }
                    const data = await r.json();
                    // 详情默认不带 events（键不存在），不会覆盖 this.events
                    if (data.task && data.task.id === this.selectedId) {
                        this.detail = Object.assign({}, this.detail, data.task);
                    }
                } catch (err) { /* 单次失败保持现状 */ }
            },
            async loadEvents() {
                if (!this.selectedId) { return; }
                const chat = this.$refs.chat;
                const nearBottom = chat ? (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 90) : true;
                try {
                    const r = await fetch('/api/tasks/' + this.selectedId + '/events?after=' + this.after);
                    const data = await r.json();
                    if (!r.ok) { return; }
                    let added = false;
                    (data.events || []).forEach((e) => {
                        this.events.push(e);
                        if (e.seq > this.after) { this.after = e.seq; }
                        added = true;
                    });
                    this.detail = Object.assign({}, this.detail, {
                        status: data.status,
                        lastText: data.lastText || '',
                        resultFiles: data.resultFiles || [],
                        error: data.error || '',
                        canContinue: data.canContinue,
                        canResume: data.canResume,
                        awaitingAnswer: data.awaitingAnswer,
                        pendingPermission: data.pendingPermission || '',
                        mode: data.mode,
                        queuedMessages: data.queuedMessages || 0,
                    });
                    if (added && nearBottom) {
                        const self = this;
                        this.$nextTick(function () {
                            if (self.$refs.chat) { self.$refs.chat.scrollTop = self.$refs.chat.scrollHeight; }
                        });
                    }
                } catch (err) { /* 忽略单次轮询失败 */ }
            },
            async pollDetail() {
                if (!this.selectedId || document.visibilityState !== 'visible') { return; }
                if (this.detail && ['closed', 'failed', 'cancelled', 'interrupted'].indexOf(this.detail.status) >= 0) { return; }
                await this.loadEvents();
            },
            async create() {
                const prompt = (this.prompt || '').trim();
                if (!prompt || this.busy) { return; }
                this.busy = true;
                this.error = '';
                try {
                    const body = { prompt: prompt, mode: this.mode, node: this.runWhere };
                    const isLocal = this.isLocalRun;   // await 前捕获，避免请求途中切执行点
                    const cwd = (this.localCwd || '').trim();
                    if (isLocal) {
                        body.paths = this.paths;
                        // 留空就不发 cwd 键，让服务端用任务目录（空串也按未指定处理，但不必发）
                        if (cwd) { body.cwd = cwd; }
                    } else {
                        body.cwd = this.remoteCwd;
                    }
                    const r = await fetch('/api/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.tasks.unshift(data.task);
                    this.prompt = '';
                    this.paths = [];
                    this.pathMenuOpen = false;
                    if (isLocal) { this.rememberCwd(cwd); }
                    await this.select(data.task);
                } catch (err) {
                    this.error = err.message;
                } finally {
                    this.busy = false;
                }
            },
            rememberCwd(p) {
                try { localStorage.setItem('wp.taskCwd', p); } catch (err) { /* ignore */ }
            },
            async loadCwdRoots() {
                try {
                    const r = await fetch('/api/ai/config');
                    const d = await r.json();
                    this.cwdRoots = d.workDirs || [];
                } catch (err) { /* 拉不到就只留通用提示 */ }
            },
            async loadNodes() {
                try {
                    const r = await fetch('/api/tasks/nodes');
                    if (!r.ok) { return; }
                    const data = await r.json();
                    this.nodes = data.nodes || [];
                } catch (err) { /* 节点列表拉取失败不阻塞页面 */ }
            },
            async sendMessage() {
                const text = (this.message || '').trim();
                if (!text || !this.canSend || this.sending) { return; }
                this.sending = true;
                this.error = '';
                this.notice = '';
                try {
                    const r = await fetch('/api/tasks/' + this.selectedId + '/message', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ text: text }),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.message = '';
                    this.notice = data.queued
                        ? '已排队（第 ' + data.position + ' 位' +
                          (data.steered ? '，插话将打断当前轮' : '') + '），本轮结束后自动送达。'
                        : '';
                    await this.loadEvents();
                } catch (err) {
                    this.error = err.message;
                } finally {
                    this.sending = false;
                }
            },
            async loadFull(e) {
                if (this.fullEvents[e.seq] !== undefined) { return; }
                try {
                    const r = await fetch('/api/tasks/' + this.selectedId + '/event/' + e.seq);
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.fullEvents = Object.assign({}, this.fullEvents, {
                        [e.seq]: (data.event && data.event.text) || e.text,
                    });
                } catch (err) {
                    this.error = err.message;
                }
            },
            async changeMode(mode) {
                if (!this.selectedId || this.modeBusy || !mode) { return; }
                this.modeBusy = true;
                this.error = '';
                try {
                    const r = await fetch('/api/tasks/' + this.selectedId + '/mode', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ mode: mode }),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                    this.detail = Object.assign({}, this.detail, { mode: data.mode });
                } catch (err) {
                    this.error = err.message;
                } finally {
                    this.modeBusy = false;
                    await this.loadEvents();
                }
            },
            backToList() {
                this.autoSelected = true;
                this.selectedId = null;
                this.detail = null;
                this.events = [];
                this.after = 0;
                this.message = '';
            },
            async cancel() {
                if (!this.selectedId) { return; }
                await fetch('/api/tasks/' + this.selectedId + '/cancel', { method: 'POST' });
                await this.loadEvents();
            },
            async closeSession() {
                if (!this.selectedId) { return; }
                if (!await WPCommon.confirm('结束这个会话？之后不能再继续追问。',
                                            { okText: '结束', danger: true })) { return; }
                await fetch('/api/tasks/' + this.selectedId + '/close', { method: 'POST' });
                await this.loadEvents();
            },
            async resolvePermission(e, opt) {
                if (!this.selectedId) { return; }
                const body = opt
                    ? { requestId: e.requestId, optionId: opt.optionId }
                    : { requestId: e.requestId, deny: true };
                try {
                    const r = await fetch('/api/tasks/' + this.selectedId + '/permission', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    const data = await r.json();
                    if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                } catch (err) {
                    this.error = err.message;
                }
                await this.loadEvents();
            },
            permissionResult(e) {
                const rid = e.requestId;
                for (let i = 0; i < this.events.length; i += 1) {
                    const x = this.events[i];
                    if (x.type === 'permission_result' && x.requestId === rid) { return x; }
                }
                return null;
            },
            permissionResolved(e) { return !!this.permissionResult(e); },
            permissionResultText(e) {
                const r = this.permissionResult(e);
                if (!r) { return ''; }
                return r.text || ({ approved: '已允许', denied: '已拒绝', timeout: '授权超时' }[r.status] || r.status);
            },
            async openPicker() {
                this.pickerOpen = true;
                this.pickerSource = 'all';
                this.picked = {};
                try {
                    const r = await fetch('/api/inbox/recent?limit=100');
                    const data = await r.json();
                    this.inboxItems = (data.items || []).filter(function (it) { return it.path; });
                } catch (err) {
                    this.inboxItems = [];
                }
            },
            togglePick(it) {
                if (this.picked[it.path]) { delete this.picked[it.path]; }
                else { this.picked[it.path] = it; }
            },
            addPicked() {
                const self = this;
                Object.keys(this.picked).forEach(function (p) {
                    if (self.paths.indexOf(p) < 0) { self.paths.push(p); }
                });
                this.pickerOpen = false;
            },
            addPath() {
                const p = (this.pathInput || '').trim();
                if (!p) { return; }
                if (this.paths.indexOf(p) < 0) { this.paths.push(p); }
                this.rememberPath(p);
                this.pathInput = '';
            },
            togglePathMenu() {
                this.pathMenuOpen = !this.pathMenuOpen;
                if (this.pathMenuOpen) { this.loadPathMenu(); }
            },
            loadRecent() {
                try {
                    const list = JSON.parse(localStorage.getItem('wp.taskPathRecent') || '[]');
                    this.recentPaths = Array.isArray(list) ? list.slice(0, 8) : [];
                } catch (err) { this.recentPaths = []; }
                try { this.localCwd = localStorage.getItem('wp.taskCwd') || ''; } catch (err) { this.localCwd = ''; }
            },
            rememberPath(p) {
                const list = [p].concat(this.recentPaths.filter(function (x) { return x !== p; })).slice(0, 8);
                this.recentPaths = list;
                try { localStorage.setItem('wp.taskPathRecent', JSON.stringify(list)); } catch (err) { /* ignore */ }
            },
            async loadPathMenu() {
                if (this.pathMenuLoaded) { return; }
                this.pathMenuLoaded = true;
                const out = [];
                const seen = new Set();
                const push = (path, label, tag) => {
                    if (!path || seen.has(path)) { return; }
                    seen.add(path);
                    out.push({ path: path, label: label || this.baseName(path), tag: tag || '' });
                };
                try {
                    const r = await fetch('/api/ai/config');
                    const d = await r.json();
                    (d.workDirs || []).forEach((p) => push(p, this.baseName(p), '常用'));
                    push(d.fallbackDir, this.baseName(d.fallbackDir), '默认');
                } catch (err) { /* ignore */ }
                try {
                    const r = await fetch('/api/files/pins');
                    const d = await r.json();
                    (d.pins || []).forEach((p) => push(p.path, p.label, '固定'));
                } catch (err) { /* ignore */ }
                this.quickDirs = out;
            },
            addQuick(p) {
                if (this.paths.indexOf(p) < 0) { this.paths.push(p); }
                this.rememberPath(p);
            },
            removePath(p) {
                this.paths = this.paths.filter(function (x) { return x !== p; });
            },
            baseName(p) {
                return String(p).split(/[\\/]/).pop() || p;
            },
            fmtSize(n) {
                return WPCommon.fmtSize(n);
            },
            fmtTime(ts) {
                return WPCommon.fmtTime(ts);
            },
            statusLabel(s) {
                return {
                    queued: '排队中', running: '运行中', waiting: '等你继续', paused: '已休眠',
                    closed: '已结束', done: '完成', failed: '失败',
                    cancelled: '已取消', interrupted: '已中断',
                }[s] || s || '-';
            },
            modeLabel(m) {
                return {
                    auto: '自动批准', default: '逐项批准', plan: '只读规划', yolo: '全部放行',
                }[m] || m || 'auto';
            },
            shortArgs(a) {
                try {
                    const obj = JSON.parse(a || '{}');
                    const vals = Object.keys(obj).map(function (k) { return obj[k]; });
                    return String(vals.join(' · ')).slice(0, 120);
                } catch (err) {
                    return String(a || '').slice(0, 120);
                }
            },
            downloadUrl(p) {
                return '/api/tasks/' + encodeURIComponent(this.selectedId)
                    + '/download?path=' + encodeURIComponent(p);
            },
        },
    }).mount('#app');
})();