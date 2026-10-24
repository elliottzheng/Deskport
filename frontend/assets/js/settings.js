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
        };
    },
    mounted() {
        this.loadInbox();
    },
    methods: {
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