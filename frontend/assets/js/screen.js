// 实时屏幕页 — MJPEG 流 + 点击/键盘远程控制
// 坐标映射: img object-fit contain 的黑边修正 (与旧版 screen-control.js 同源逻辑)
const { createApp } = Vue;

createApp({
    data() {
        return {
            fps: 0,
            resolution: '',
            offline: false,
            status: '连接中',
            screenW: 0,
            screenH: 0,
            frameCount: 0,
            fpsTimer: null,
            resTimer: null,
            liveOn: false,
            pageActive: true,
            reconnectTimer: null,
            heldModifiers: new Set(),
        };
    },
    computed: {
        statusClass() {
            return this.offline ? 'status-err' : 'status-ok';
        },
    },
    methods: {
        sendControl(params) {
            const query = new URLSearchParams(params).toString();
            return fetch('/api/screen/control?' + query)
                .then(r => r.json())
                .catch(err => console.error('控制请求失败:', err));
        },
        imgToScreen(img, clientX, clientY) {
            if (!this.screenW && img.naturalWidth > 0) {
                this.screenW = img.naturalWidth;
                this.screenH = img.naturalHeight;
            }
            if (!this.screenW || !this.screenH) { return null; }
            const rect = img.getBoundingClientRect();
            const clickX = clientX - rect.left;
            const clickY = clientY - rect.top;
            const imgAspect = this.screenW / this.screenH;
            const containerAspect = rect.width / rect.height;
            let renderW, renderH, offsetX, offsetY;
            if (imgAspect > containerAspect) {
                renderW = rect.width;
                renderH = rect.width / imgAspect;
                offsetX = 0;
                offsetY = (rect.height - renderH) / 2;
            } else {
                renderH = rect.height;
                renderW = rect.height * imgAspect;
                offsetX = (rect.width - renderW) / 2;
                offsetY = 0;
            }
            if (clickX < offsetX || clickX > offsetX + renderW ||
                clickY < offsetY || clickY > offsetY + renderH) {
                return null;
            }
            const screenX = Math.max(0, Math.min(this.screenW - 1,
                Math.round((clickX - offsetX) / renderW * this.screenW)));
            const screenY = Math.max(0, Math.min(this.screenH - 1,
                Math.round((clickY - offsetY) / renderH * this.screenH)));
            return { x: screenX, y: screenY };
        },
        keyToName(e) {
            const map = { Control: 'ctrl', Alt: 'alt', Shift: 'shift', Meta: 'cmd' };
            if (map[e.key]) { return map[e.key]; }
            if (e.key.length === 1) { return e.key; }
            const k = e.key.toLowerCase();
            const named = {
                escape: 'esc', ' ': 'space', capslock: 'caps_lock',
                numlock: 'num_lock', scrolllock: 'scroll_lock',
                pageup: 'page_up', pagedown: 'page_down',
                printscreen: 'print_screen',
                arrowup: 'up', arrowdown: 'down',
                arrowleft: 'left', arrowright: 'right',
            };
            return named[k] || k;
        },
        startLive() {
            if (this.liveOn || !this.pageActive) { return; }
            const img = this.$refs.streamImg;
            if (!img) { return; }
            this.liveOn = true;
            const connect = () => {
                if (!this.liveOn) { return; }
                img.src = '/api/screen/stream?t=' + Date.now();
            };
            this._connectStream = connect;
            img.onload = () => { this.offline = false; this.status = '已连接'; };
            img.onerror = () => {
                if (!this.liveOn) { return; }
                this.offline = true;
                this.status = '重连中';
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = setTimeout(connect, 1000);
            };
            connect();
            this.fpsTimer = setInterval(() => {
                if (img.complete && img.naturalWidth > 0) { this.frameCount += 1; }
                this.fps = this.frameCount;
                this.frameCount = 0;
            }, 1000);
            this.resTimer = setInterval(() => {
                if (!this.screenW && img.naturalWidth > 0) {
                    this.screenW = img.naturalWidth;
                    this.screenH = img.naturalHeight;
                    this.resolution = this.screenW + ' x ' + this.screenH;
                }
            }, 500);
        },
        stopLive() {
            this.liveOn = false;
            clearTimeout(this.reconnectTimer);
            clearInterval(this.fpsTimer);
            clearInterval(this.resTimer);
            this.fpsTimer = null;
            this.resTimer = null;
            const img = this.$refs.streamImg;
            if (img) {
                img.onload = null;
                img.onerror = null;
                img.src = '';   // 断开 MJPEG，服务端 release() 停止抓屏
            }
            this.fps = 0;
            this.frameCount = 0;
            if (this.status !== '连接中') { this.status = '已暂停'; }
        },
    },
    mounted() {
        const img = this.$refs.streamImg;

        img.addEventListener('click', (e) => {
            const pos = this.imgToScreen(img, e.clientX, e.clientY);
            if (pos) { this.sendControl({ controlType: 'mouse', x: pos.x, y: pos.y, button: 'left' }); }
        });
        img.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            const pos = this.imgToScreen(img, e.clientX, e.clientY);
            if (pos) { this.sendControl({ controlType: 'mouse', x: pos.x, y: pos.y, button: 'right' }); }
        });

        // ---- 键盘 ----
        document.addEventListener('keydown', (e) => {
            const keyName = this.keyToName(e);
            const mods = { Control: 'ctrl', Alt: 'alt', Shift: 'shift', Meta: 'cmd' };
            if (mods[e.key]) {
                this.heldModifiers.add(mods[e.key]);
                e.preventDefault();
                return;
            }
            const parts = [];
            ['ctrl', 'alt', 'shift', 'cmd'].forEach(m => {
                if (this.heldModifiers.has(m)) { parts.push(m); }
            });
            parts.push(keyName);
            this.sendControl({ controlType: 'keyboard', key: parts.join('+') });
            e.preventDefault();
        });
        document.addEventListener('keyup', (e) => {
            const mods = { Control: 'ctrl', Alt: 'alt', Shift: 'shift', Meta: 'cmd' };
            if (mods[e.key]) { this.heldModifiers.delete(mods[e.key]); }
        });
        window.addEventListener('blur', () => { this.heldModifiers.clear(); });

        // 壳内切换视图时暂停/恢复 MJPEG，避免隐藏的 iframe 持续抓屏
        this._onShellView = (e) => {
            const d = e.data || {};
            if (d.source !== WP_MSG.SRC.SHELL || d.type !== WP_MSG.SHELL.VIEW_ACTIVE) { return; }
            this.pageActive = !!d.active;
            if (this.pageActive) { this.startLive(); } else { this.stopLive(); }
        };
        window.addEventListener('message', this._onShellView);
        this.startLive();
    },
    unmounted() {
        window.removeEventListener('message', this._onShellView);
        this.stopLive();
    },
}).mount('#app');
