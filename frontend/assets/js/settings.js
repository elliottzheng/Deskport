// 设置 — 单用户改密
const { createApp } = Vue;
// 「我的权限」卡片（批次 L）经 mixin 挂进来: 自带 capability/capabilityState/capabilityError 三态与 loadCapability，规则判定全在服务端，前端只展示
const app = createApp({
    data() {
        return {
            current: '',
            next: '',
            confirm: '',
            error: '',
            ok: '',
            busy: false,
            inboxEnabled: false,
            inboxDirs: [],
            defaultDirs: [],
            homeDir: '',
            newInboxDir: '',
            inboxError: '',
            inboxOk: '',
            inboxBusy: false,
            brandName: '',
            brandError: '',
            brandOk: '',
            brandBusy: false,
            fedToken: '',
            fedRemotes: [],
            fedDiscovered: {},
            fedError: '',
            fedOk: '',
            fedBusy: false,
            fedRoots: [],
            fedRootsState: 'loading',   // loading | ok | error: 空列表只有 ok 时才等于"确实为空"
            newFedRoot: '',
            rootsError: '',
            rootsOk: '',
            rootsBusy: false,
            devices: [],
            deviceError: '',
            deviceOk: '',
            deviceBusy: false,
            uploadTargets: [],
            phonePath: '',
            utError: '',
            utOk: '',
            utBusy: false,
            handbookUrl: '',
            handbookOk: '',
            agentTokens: [],
            agentTokenError: '',
            agentTokenOk: '',
            agentTokenBusy: false,
            newTokenName: '',
            newTokenScope: '',
            plainToken: null,
            plainCopied: '',
        };
    },
    mounted() {
        this.handbookUrl = location.origin + '/api/agent/handbook';
        this.loadInbox();
        this.loadBrand();
        this.loadFederation();
        this.loadDevices();
        this.loadUploadTargets();
        this.loadAgentTokens();
    },
    methods: {
        async loadBrand() {
            try {
                const r = await fetch('/api/settings/brand');
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.brandName = data.name || 'Deskport';
            } catch (err) {
                this.brandError = '加载品牌名称失败: ' + err.message;
            }
        },
        async saveBrand() {
            if (this.brandBusy) { return; }
            this.brandError = '';
            this.brandOk = '';
            const name = this.brandName.trim();
            if (!name) { this.brandError = '品牌名不能为空'; return; }
            if (name.length > 24) { this.brandError = '品牌名最多 24 个字符'; return; }
            this.brandBusy = true;
            try {
                const r = await fetch('/api/settings/brand', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.brandName = data.name;
                this.brandOk = '已保存，正在刷新页面…';
                setTimeout(() => {
                    if (window.top && window.top !== window) { window.top.location.reload(); }
                    else { location.reload(); }
                }, 700);
            } catch (err) {
                this.brandError = err.message;
            } finally {
                this.brandBusy = false;
            }
        },
        async loadInbox() {
            try {
                const r = await fetch('/api/settings/inbox');
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.inboxEnabled = !!data.enabled;
                this.inboxDirs = data.dirs || [];
                this.defaultDirs = data.defaults || [];
                this.homeDir = data.home || '';
            } catch (err) {
                this.inboxError = '加载收件箱设置失败: ' + err.message;
            }
        },
        prettyPath(path) {
            if (this.homeDir && path.startsWith(this.homeDir)) {
                return '~' + path.slice(this.homeDir.length);
            }
            return path;
        },
        addInboxDir() {
            const path = this.newInboxDir.trim();
            if (!path) { return; }
            if (!this.inboxDirs.includes(path)) { this.inboxDirs.push(path); }
            this.newInboxDir = '';
        },
        removeInboxDir(index) {
            this.inboxDirs.splice(index, 1);
        },
        useDefaultDirs() {
            for (const path of this.defaultDirs) {
                if (!this.inboxDirs.includes(path)) { this.inboxDirs.push(path); }
            }
        },
        async saveInbox() {
            if (this.inboxBusy) { return; }
            this.inboxError = '';
            this.inboxOk = '';
            this.inboxBusy = true;
            try {
                const r = await fetch('/api/settings/inbox', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: this.inboxEnabled, dirs: this.inboxDirs }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.inboxDirs = data.dirs || [];
                this.inboxOk = data.enabled ? '收件箱扫描已开启' : '收件箱扫描已关闭';
                setTimeout(() => { this.inboxOk = ''; }, 2500);
            } catch (err) {
                this.inboxError = err.message;
            } finally {
                this.inboxBusy = false;
            }
        },
        toRemoteRow(x) {
            return { id: x.id, name: x.name, base: x.base, token: x.token || '',
                     terminal: x.terminal !== false,
                     files: x.files === true,
                     // 写依赖读：老配置/手改 config.json 里可能出现 files=false
                     // 但 filesWrite=true，这里一并归一，别让勾选框「勾着但灰掉」
                     filesWrite: x.files === true && x.filesWrite === true,
                     appsText: (x.apps || []).join(', ') };
        },
        async loadFederation() {
            try {
                const r = await fetch('/api/settings/federation');
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.fedToken = data.token || '';
                this.fedRemotes = (data.remotes || []).map(x => this.toRemoteRow(x));
                this.fedRoots = data.roots || [];
                this.fedRootsState = 'ok';
                const seen = {};
                (data.discoveredApps || []).forEach(function (a) {
                    const key = a.remoteId;
                    seen[key] = seen[key] || [];
                    seen[key].push(a.label ? (a.label + ' (' + a.appId + ')') : a.appId);
                });
                this.fedDiscovered = seen;
            } catch (err) {
                // 加载失败时 fedRoots 是 []，但那是"不知道"，不是"确实为空"
                this.fedRootsState = 'error';
                this.fedError = '加载联邦设置失败: ' + err.message;
            }
        },
        async copyHandbook() {
            try {
                await navigator.clipboard.writeText(this.handbookUrl);
                this.handbookOk = '已复制，发给智能体即可；令牌请另行发送';
                setTimeout(() => { this.handbookOk = ''; }, 2500);
            } catch (err) {
                this.handbookOk = '复制失败，请手动选中链接复制';
            }
        },
        async copyToken() {
            try {
                await navigator.clipboard.writeText(this.fedToken);
                this.fedOk = '本机令牌已复制，粘贴到对方机器的「远端机器」里';
                setTimeout(() => { this.fedOk = ''; }, 2500);
            } catch (err) {
                this.fedError = '复制失败，请手动选中令牌复制';
            }
        },
        genToken() {
            const arr = new Uint8Array(24);
            crypto.getRandomValues(arr);
            this.fedToken = Array.from(arr, function (b) {
                return b.toString(36).padStart(2, '0');
            }).join('').slice(0, 32);
        },
        async saveToken() {
            if (this.fedBusy) { return; }
            this.fedError = '';
            this.fedOk = '';
            this.fedBusy = true;
            try {
                const r = await fetch('/api/settings/federation/token', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: this.fedToken.trim() }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.fedToken = data.token || '';
                this.fedOk = this.fedToken ? '本机联邦令牌已保存' : '本机联邦已关闭（留空）';
                setTimeout(() => { this.fedOk = ''; }, 2500);
            } catch (err) {
                this.fedError = err.message;
            } finally {
                this.fedBusy = false;
            }
        },
        addRemote() {
            const arr = new Uint8Array(4);
            crypto.getRandomValues(arr);
            const id = 'r' + Array.from(arr, function (b) {
                return b.toString(36).padStart(2, '0');
            }).join('').slice(0, 6);
            this.fedRemotes.push({ id: id, name: '', base: '', token: '',
                                   terminal: true, files: false, filesWrite: false,
                                   appsText: '' });
        },
        remoteMode(r) {
            // 未开文件浏览 = 未接文件；开了浏览才分只读/可写
            if (!r.files) { return '未接文件'; }
            return r.filesWrite ? '可写' : '只读';
        },
        syncFiles(r) {
            if (!r.files) { r.filesWrite = false; }
        },
        async saveRemotes() {
            if (this.fedBusy) { return; }
            this.fedError = '';
            this.fedOk = '';
            for (const r of this.fedRemotes) {
                if (!r.name || !r.base) { this.fedError = '每台机器都需要名称和地址'; return; }
            }
            const payload = this.fedRemotes.map(function (r) {
                const apps = (r.appsText || '').split(/[,，\s]+/).filter(Boolean);
                return { id: r.id, name: r.name, base: r.base, token: r.token || '',
                         terminal: r.terminal !== false, files: r.files === true,
                         filesWrite: r.files === true && r.filesWrite === true, apps: apps };
            });
            this.fedBusy = true;
            try {
                const r = await fetch('/api/settings/federation/remotes', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ remotes: payload }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.fedRemotes = (data.remotes || []).map(x => this.toRemoteRow(x));
                this.fedOk = '远端列表已保存；允许的外部应用会在几秒内自动挂到本机';
                setTimeout(() => { this.fedOk = ''; }, 2500);
            } catch (err) {
                this.fedError = err.message;
            } finally {
                this.fedBusy = false;
            }
        },
        addFedRoot() {
            const path = this.newFedRoot.trim();
            if (!path) { return; }
            if (!this.fedRoots.includes(path)) { this.fedRoots.push(path); }
            this.newFedRoot = '';
        },
        removeFedRoot(index) {
            this.fedRoots.splice(index, 1);
        },
        async saveRoots() {
            if (this.rootsBusy) { return; }
            this.rootsError = '';
            this.rootsOk = '';
            this.rootsBusy = true;
            try {
                const r = await fetch('/api/settings/federation/roots', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ roots: this.fedRoots }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.fedRoots = data.roots || [];
                this.fedRootsState = 'ok';
                this.rootsOk = this.fedRoots.length
                    ? ('已保存 ' + this.fedRoots.length + ' 个可写目录，立即生效（无需重启）')
                    : '已清空：带 X-Deskport-From 的联邦转发写现在一律 403（本机自己的文件操作不受影响）';
                setTimeout(() => { this.rootsOk = ''; }, 3000);
            } catch (err) {
                this.rootsError = err.message;
            } finally {
                this.rootsBusy = false;
            }
        },
        async loadDevices() {
            try {
                const r = await fetch('/api/settings/devices');
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.devices = data.devices || [];
            } catch (err) {
                this.deviceError = '加载设备列表失败: ' + err.message;
            }
        },
        async revokeDevice(d) {
            if (this.deviceBusy) { return; }
            this.deviceError = '';
            this.deviceOk = '';
            this.deviceBusy = true;
            try {
                const r = await fetch('/api/settings/devices/revoke', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: d.id }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.devices = data.devices || [];
                this.deviceOk = '已吊销「' + d.name + '」的令牌，该设备需重新登录';
                setTimeout(() => { this.deviceOk = ''; }, 2500);
            } catch (err) {
                this.deviceError = err.message;
            } finally {
                this.deviceBusy = false;
            }
        },
        toTargetRow(x) {
            return { id: x.id || '', name: x.name || '', path: x.path || '' };
        },
        async loadUploadTargets() {
            try {
                const r = await fetch('/api/settings/uploadTargets');
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                const builtin = data.builtinId || 'phone';
                const rows = data.targets || [];
                this.phonePath = (rows.find(t => t.id === builtin) || {}).path || '';
                this.uploadTargets = rows.filter(t => t.id !== builtin).map(x => this.toTargetRow(x));
            } catch (err) {
                this.utError = '加载上传目标失败: ' + err.message;
            }
        },
        addUploadTarget() {
            this.uploadTargets.push({ id: '', name: '', path: '' });
        },
        async saveUploadTargets() {
            if (this.utBusy) { return; }
            this.utError = '';
            this.utOk = '';
            for (const t of this.uploadTargets) {
                if (!t.name || !t.path) { this.utError = '每个目标都需要名称和目录'; return; }
            }
            this.utBusy = true;
            try {
                const r = await fetch('/api/settings/uploadTargets', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ targets: this.uploadTargets }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                const builtin = data.builtinId || 'phone';
                this.uploadTargets = (data.targets || []).filter(t => t.id !== builtin).map(x => this.toTargetRow(x));
                this.utOk = '上传目标已保存；App 端只会看到名称';
                setTimeout(() => { this.utOk = ''; }, 2500);
            } catch (err) {
                this.utError = err.message;
            } finally {
                this.utBusy = false;
            }
        },
        scopeText(t) {
            // 缺省 scopes = 全权（agent_api.agentHasScope 的同款语义）
            const scopes = (t && t.scopes) || [];
            if (!scopes.length || scopes.includes('full')) { return '全权'; }
            if (scopes.includes('upload') && scopes.includes('share') && scopes.length === 2) { return '上传 + 派活'; }
            if (scopes.length === 1 && scopes[0] === 'share') { return '仅派活'; }
            return scopes.join(' + ');
        },
        async loadAgentTokens() {
            try {
                const r = await fetch('/api/settings/agent-tokens');
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.agentTokens = data.tokens || [];
            } catch (err) {
                this.agentTokenError = '加载智能体令牌失败: ' + err.message;
            }
        },
        async createAgentToken() {
            if (this.agentTokenBusy) { return; }
            this.agentTokenError = '';
            this.agentTokenOk = '';
            const name = this.newTokenName.trim();
            if (!name) { this.agentTokenError = '请填写令牌名称'; return; }
            if (name.length > 24) { this.agentTokenError = '名称最多 24 个字符'; return; }
            this.agentTokenBusy = true;
            try {
                const body = { name: name };
                if (this.newTokenScope) { body.scopes = this.newTokenScope.split('+'); }
                const r = await fetch('/api/settings/agent-tokens', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.plainToken = { token: data.token, name: data.name, scopes: data.scopes };
                this.plainCopied = '';
                this.newTokenName = '';
                this.newTokenScope = '';
                await this.loadAgentTokens();
            } catch (err) {
                this.agentTokenError = err.message;
            } finally {
                this.agentTokenBusy = false;
            }
        },
        async revokeAgentToken(t) {
            if (this.agentTokenBusy) { return; }
            this.agentTokenError = '';
            this.agentTokenOk = '';
            this.agentTokenBusy = true;
            try {
                const r = await fetch('/api/settings/agent-tokens/' + t.id, { method: 'DELETE' });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.agentTokens = this.agentTokens.filter(x => x.id !== t.id);
                this.agentTokenOk = '已吊销「' + t.name + '」，持该令牌的程序会立刻失效';
                setTimeout(() => { this.agentTokenOk = ''; }, 2500);
            } catch (err) {
                this.agentTokenError = err.message;
            } finally {
                this.agentTokenBusy = false;
            }
        },
        async copyPlainToken() {
            try {
                await navigator.clipboard.writeText(this.plainToken.token);
                this.plainCopied = '已复制';
                setTimeout(() => { this.plainCopied = ''; }, 2000);
            } catch (err) {
                this.plainCopied = '复制失败，请手动选中复制';
            }
        },
        async submit() {
            if (this.busy) { return; }
            this.error = '';
            this.ok = '';
            if (!this.current) { this.error = '请输入当前密码'; return; }
            if (this.next.length < 6) { this.error = '新密码至少 6 位'; return; }
            if (this.next !== this.confirm) { this.error = '两次输入的新密码不一致'; return; }
            this.busy = true;
            try {
                const r = await fetch('/api/auth/password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current: this.current, new: this.next }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.ok = '密码已修改，请用新密码重新登录…';
                setTimeout(() => { location.href = '/login'; }, 1200);
            } catch (err) {
                this.error = err.message;
            } finally {
                this.busy = false;
            }
        },
    },
});
app.mixin(WPSettingsCapability.mixin());
app.mount('#app');