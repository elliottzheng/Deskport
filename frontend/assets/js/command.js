// 命令执行页
const { createApp } = Vue;

createApp({
    data() {
        return {
            cmd: '',
            running: false,
            error: '',
            result: null,
        };
    },
    methods: {
        async execute() {
            const cmd = this.cmd.trim();
            if (!cmd || this.running) { return; }
            this.running = true;
            this.error = '';
            this.result = null;
            try {
                const r = await fetch('/api/command', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cmd: cmd }),
                });
                const data = await r.json();
                if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                this.result = data;
            } catch (err) {
                this.error = err.message;
            } finally {
                this.running = false;
            }
        },
    },
}).mount('#app');
