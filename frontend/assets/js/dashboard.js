// 控制台 — 状态 / 快动作 / 进程·窗口·应用工作区 / 发送文本 / 审计
const { createApp } = Vue;

createApp({
    data() {
        return {
            info: null,
            processes: [],
            windows: [],
            loading: false,
            error: '',
            procQuery: '',
            procSort: 'mem',
            appQuery: '',
            workTab: 'processes',
            textOpen: false,
            textBody: '',
            textSubmit: false,
            textTarget: '',
            powerModal: null,
            pendingAbort: false,
            closeTarget: null,
            auditOpen: false,
            diagOpen: false,
            diagText: '',
            toast: '',
            toastTimer: null,
            timer: null,
            build: '',
            resumeHidden: false,
            pageActive: true,
        };
    },
    computed: {
        uptimeText() {
            if (!this.info) { return ''; }
            const s = this.info.uptimeSeconds;
            const d = Math.floor(s / 86400);
            const h = Math.floor((s % 86400) / 3600);
            const m = Math.floor((s % 3600) / 60);
            return (d ? d + ' 天 ' : '') + h + ' 小时 ' + m + ' 分';
        },
        mainDisks() {
            if (!this.info) { return []; }
            return this.info.disks.filter(d => /^[A-Z]:/i.test(d.path)).slice(0, 2);
        },
        processGroups() {
            const q = this.procQuery.toLowerCase();
            const map = new Map();
            for (const p of this.processes) {
                if (q && !p.name.toLowerCase().includes(q)) { continue; }
                let g = map.get(p.name);
                if (!g) {
                    g = { name: p.name, count: 0, mem: 0, cpu: 0, pids: [] };
                    map.set(p.name, g);
                }
                g.count += 1;
                g.mem += p.mem || 0;
                g.cpu += p.cpu || 0;
                g.pids.push(p.pid);
            }
            const out = [...map.values()];
            out.sort(this.procSort === 'cpu'
                ? (a, b) => b.cpu - a.cpu
                : (a, b) => b.mem - a.mem);
            return out;
        },
        appMatches() {
            if (!this.info || !this.appQuery) { return []; }
            const q = this.appQuery.toLowerCase();
            return this.info.apps.filter(a => a.name.toLowerCase().includes(q)).slice(0, 12);
        },
        audit() {
            return this.info ? (this.info.audit || []).slice().reverse() : [];
        },
        resume() {
            try {
                const raw = localStorage.getItem('wp.lastView');
                if (!raw) { return null; }
                const v = JSON.parse(raw);
                if (!v || !v.path || v.path === '/' || !v.at) { return null; }
                if (Date.now() - v.at > 12 * 3600 * 1000) { return null; }
                const labels = {
                    '/ssh': 'SSH 会话', '/terminal': '本地终端', '/files': '文件管理',
                    '/screen': '屏幕', '/command': '命令', '/settings': '设置',
                };
                const mins = Math.round((Date.now() - v.at) / 60000);
                const ago = mins < 1 ? '刚刚' : (mins < 60 ? mins + ' 分钟前' : Math.round(mins / 60) + ' 小时前');
                return { path: v.path, label: labels[v.path] || v.path, ago: ago };
            } catch (err) { return null; }
        },
    },
    mounted() {
        this.loadAll();
        this.timer = setInterval(() => {
            if (this.pageActive && document.visibilityState === 'visible') { this.refreshLive(); }
        }, 12000);
        // 壳内隐藏时停止轮询 (iframe 的 visibilityState 不会变)
        this._onShellView = (e) => {
            const d = e.data || {};
            if (d.source !== WP_MSG.SRC.SHELL || d.type !== WP_MSG.SHELL.VIEW_ACTIVE) { return; }
            this.pageActive = !!d.active;
            if (this.pageActive) { this.refreshLive(); }
        };
        window.addEventListener('message', this._onShellView);
    },
    beforeUnmount() {
        clearInterval(this.timer);
        window.removeEventListener('message', this._onShellView);
    },
    methods: {
        fmtSize(n) {
            return WPCommon.fmtSize(n);
        },
        async loadAll() {
            this.loading = true;
            this.error = '';
            try {
                const [summary, procs, wins, version] = await Promise.all([
                    fetch('/api/control/summary').then(r => r.json()),
                    fetch('/api/control/processes?sort=' + this.procSort).then(r => r.json()),
                    fetch('/api/control/windows').then(r => r.json()),
                    fetch('/api/version').then(r => r.json()),
                ]);
                this.info = summary;
                this.processes = procs.processes || [];
                this.windows = wins.windows || [];
                this.build = version.build || '';
            } catch (err) {
                this.error = '加载失败：' + err.message;
            } finally {
                this.loading = false;
            }
        },
        async refreshLive() {
            try {
                const [procs, wins] = await Promise.all([
                    fetch('/api/control/processes?sort=' + this.procSort).then(r => r.json()),
                    fetch('/api/control/windows').then(r => r.json()),
                ]);
                this.processes = procs.processes || [];
                this.windows = wins.windows || [];
            } catch (err) { /* 静默 */ }
        },
        showToast(msg) {
            this.toast = msg;
            clearTimeout(this.toastTimer);
            this.toastTimer = setTimeout(() => { this.toast = ''; }, 2600);
        },
        async post(url, body) {
            const r = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
            return data;
        },
        async key(action) {
            try {
                await this.post('/api/control/keys', { action: action });
                this.showToast('已执行: ' + action);
            } catch (err) { this.showToast(err.message); }
        },
        async power(action) {
            try {
                await this.post('/api/control/power', { action: action });
                this.showToast('已执行: ' + action);
            } catch (err) { this.showToast(err.message); }
        },
        askPower(action) { this.powerModal = action; },
        async doPower(delay) {
            const action = this.powerModal;
            this.powerModal = null;
            try {
                await this.post('/api/control/power', { action: action, delay: delay });
                if (delay > 0) { this.pendingAbort = true; }
                this.showToast(delay > 0 ? ('已计划 ' + delay + ' 秒后执行') : '已执行');
            } catch (err) { this.showToast(err.message); }
        },
        async abortPower() {
            try {
                await this.post('/api/control/power', { action: 'abort' });
                this.pendingAbort = false;
                this.showToast('已取消计划关机');
            } catch (err) { this.showToast(err.message); }
        },
        askCloseGroup(group) {
            this.closeTarget = {
                name: group.name,
                count: group.count,
                pids: group.pids.slice(),
            };
        },
        async doClose(force) {
            const target = this.closeTarget;
            this.closeTarget = null;
            if (!target) { return; }
            let ok = 0; let lastError = '';
            for (const pid of target.pids) {
                try {
                    await this.post('/api/control/process/close', { pid: pid, force: force });
                    ok += 1;
                } catch (err) {
                    lastError = err.message;
                }
            }
            this.showToast(ok ? ('已结束 ' + target.name + '（' + ok + '/' + target.pids.length + '）')
                : lastError);
            this.refreshLive();
        },
        async windowAct(w, action) {
            try {
                await this.post('/api/control/window', { hwnd: w.hwnd, action: action });
                this.showToast(action === 'focus' ? '已切换' : '已关闭');
                this.refreshLive();
            } catch (err) { this.showToast(err.message); }
        },
        async launch(a) {
            try {
                await this.post('/api/control/launch', { path: a.path });
                this.showToast('已启动 ' + a.name);
                setTimeout(() => this.refreshLive(), 1500);
            } catch (err) { this.showToast(err.message); }
        },
        async sendText() {
            try {
                const data = await this.post('/api/control/text', {
                    text: this.textBody,
                    submit: this.textSubmit,
                    hwnd: this.textTarget || null,
                });
                this.showToast('已发送 ' + data.length + ' 字');
                this.textBody = '';
                this.textOpen = false;
            } catch (err) { this.showToast(err.message); }
        },
        async openDiag() {
            this.diagOpen = true;
            this.diagText = '加载中…';
            try {
                const r = await fetch('/api/diagnostics');
                const data = await r.json();
                this.diagText = JSON.stringify(data, null, 1);
            } catch (err) {
                this.diagText = '诊断失败: ' + err.message;
            }
        },
        svcName(kind) {
            return { web: '界面', local: '终端服务', ssh: 'SSH 服务' }[kind] || kind;
        },
        actionText(action) {
            const map = {
                'process.close': '结束进程', 'window.focus': '切换窗口',
                'window.close': '关闭窗口', 'window.minimize': '最小化',
                'app.launch': '启动应用', 'power.shutdown': '关机',
                'power.restart': '重启', 'power.lock': '锁屏',
                'power.sleep': '睡眠', 'power.abort': '取消关机',
                'power.display_off': '关显示器', 'text.send': '发送文本',
                'auth.login': '登录', 'auth.logout': '退出',
            };
            if (action && action.startsWith('keys.')) { return '按键 ' + action.slice(5); }
            return map[action] || action;
        },
    },
}).mount('#app');