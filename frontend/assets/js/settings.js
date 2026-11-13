// 设置 — 单用户改密
const { createApp } = Vue;

createApp({
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
        };
    },
    mounted() {
        this.loadInbox();
        this.loadBrand();
        this.loadFederation();
        this.loadDevices();
        this.loadUploadTargets();
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
                     filesWrite: x.filesWrite === true,
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
                const seen = {};
                (data.discoveredApps || []).forEach(function (a) {
                    const key = a.remoteId;
                    seen[key] = seen[key] || [];
                    seen[key].push(a.label ? (a.label + ' (' + a.appId + ')') : a.appId);
                });
                this.fedDiscovered = seen;
            } catch (err) {
                this.fedError = '加载联邦设置失败: ' + err.message;
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
                this.rootsOk = this.fedRoots.length
                    ? ('已保存 ' + this.fedRoots.length + ' 个可写目录，立即生效（无需重启）')
                    : '已清空：联邦写现在一律拒绝';
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
}).mount('#app');