// 设置页「我的权限」卡片（批次 L）— 数据来自 GET /api/capability/me（只读，
// 无入参，服务端固定 6 条示例判定）。纯函数部分（view/kindText）经
// module.exports 供 Node 单测（tests/js/settings_capability.test.mjs）；
// 页面侧经 window.WPSettingsCapability 的 mixin 挂进 settings.js 的 Vue
// 应用。三态与 fedRootsState 同款：loading | ok | error——没拿到数据时
// 绝不下"你什么都没有权限"的结论。
(function () {
    'use strict';

    var KIND_TEXT = {human: '人', agent: '智能体', device: '设备', remote: '联邦成员'};

    function kindText(kind) {
        return KIND_TEXT[kind] || kind || '未知主体';
    }

    // 端点原始 JSON → 卡片视图：主体一行 + 规则行（动作 @ 资源 + 来源）+
    // 示例判定行（✓/✗）。缺字段按空值兜底（老服务端/坏数据不白屏），
    // 但不改判定结果本身。
    function view(data) {
        var src = data || {};
        var principal = src.principal || {};
        return {
            kind: kindText(principal.kind),
            name: principal.name || '未命名',
            rules: (src.rules || []).map(function (r) {
                return {action: r.action || '', resource: r.resource || '',
                        source: r.source || ''};
            }),
            decisions: (src.decisions || []).map(function (d) {
                return {action: d.action || '', resource: d.resource || '',
                        allow: d.verdict === 'allow'};
            }),
        };
    }

    function mixin() {
        return {
            data() {
                return {capability: null, capabilityState: 'loading',
                        capabilityError: ''};
            },
            mounted() {
                this.loadCapability();
            },
            methods: {
                async loadCapability() {
                    try {
                        const r = await fetch('/api/capability/me');
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.capability = view(data);
                        this.capabilityState = 'ok';
                    } catch (err) {
                        // 与 fedRootsState 同款: 加载失败是"不知道", 不是"没有权限"
                        this.capabilityState = 'error';
                        this.capabilityError = '加载权限信息失败: ' + err.message;
                    }
                },
            },
        };
    }

    if (typeof window !== 'undefined') {
        window.WPSettingsCapability = {view: view, kindText: kindText, mixin: mixin};
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {view: view, kindText: kindText};
    }
})();
