// 命令执行页
(function () {
    'use strict';

    var RECENT_KEY = 'cmd.recent';   // 最近执行列表的本地存储键（规格 M3 #6）
    var RECENT_MAX = 5;              // 首屏只摆 3–5 条，取上限 5

    // 纯函数：执行成功后更新「最近执行」——去重、新的在前、封顶。
    // 空命令/坏数据原样带回（不写坏状态），供 tests/js/ 单测。
    function recentList(current, cmd) {
        var list = Array.isArray(current)
            ? current.filter(function (x) { return typeof x === 'string' && x; })
            : [];
        var text = String(cmd === null || cmd === undefined ? '' : cmd).trim();
        if (!text) { return list.slice(0, RECENT_MAX); }
        return [text].concat(list.filter(function (x) { return x !== text; }))
            .slice(0, RECENT_MAX);
    }

    function readRecent() {
        try {
            var raw = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
            return Array.isArray(raw) ? recentList(raw, '') : [];
        } catch (err) {
            return [];                // 隐私模式/坏 JSON：按没存过处理
        }
    }

    function mount() {
        const { createApp } = Vue;
        createApp({
            data() {
                return {
                    cmd: '',
                    running: false,
                    error: '',
                    result: null,
                    recent: [],
                };
            },
            mounted() {
                this.recent = readRecent();
            },
            methods: {
                // 点最近命令：填入输入框（不直接执行——命令可先看一眼再改）
                useRecent(c) {
                    this.cmd = c;
                },
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
                        this.recent = recentList(this.recent, cmd);
                        try {
                            localStorage.setItem(RECENT_KEY, JSON.stringify(this.recent));
                        } catch (err) { /* 存不下不挡执行 */ }
                    } catch (err) {
                        this.error = err.message;
                    } finally {
                        this.running = false;
                    }
                },
            },
        }).mount('#app');
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { RECENT_KEY: RECENT_KEY, RECENT_MAX: RECENT_MAX,
                           recentList: recentList };
    }
    if (typeof Vue !== 'undefined') { mount(); }
})();
