// 设置页「主管」卡片（批次 S）— 数据来自 GET /api/settings/leads（批次 Q 的后端）。
// 纯函数部分（statusText/kindText/lastSeenText/view/bindable）经 module.exports
// 供 Node 单测（tests/js/settings_leads.test.mjs）；页面侧经 window.WPSettingsLeads
// 的 mixin 挂进 settings.js 的 Vue 应用。三态与「我的权限」卡片同款：
// loading | ok | error——没拿到数据时绝不下"还没有主管"的结论（规格 S1）。
(function () {
    'use strict';

    var STATUS_TEXT = {active: '在用', paused: '已暂停', retired: '已停用'};
    var KIND_TEXT = {external: '外部接入', hosted: '托管'};

    function statusText(status) {
        return STATUS_TEXT[status] || status || '未知状态';
    }

    function kindText(kind) {
        return KIND_TEXT[kind] || kind || '未知类型';
    }

    function lastSeenText(lastSeenAt, nowSec) {
        var seen = Number(lastSeenAt) || 0;
        if (!seen) { return '从未活跃'; }
        var now = Number(nowSec) || 0;
        var delta = now > seen ? now - seen : 0;
        if (delta < 60) { return '刚刚'; }
        if (delta < 3600) { return Math.floor(delta / 60) + ' 分钟前'; }
        if (delta < 86400) { return Math.floor(delta / 3600) + ' 小时前'; }
        return Math.floor(delta / 86400) + ' 天前';
    }

    // 端点原始 JSON → 卡片行。缺字段按空值兜底（老服务端/坏数据不白屏），
    // 但不改数据本身；lastSeenText 的"现在"由调用方传入（纯函数可测）。
    function view(data, nowSec) {
        var src = data || {};
        var rows = Array.isArray(src.leads) ? src.leads : [];
        return rows.map(function (l) {
            var tasks = l.tasks || {};
            return {
                id: l.id || '',
                name: l.name || '未命名',
                project: l.project || '',
                kind: kindText(l.kind),
                status: l.status || '',
                statusText: statusText(l.status),
                tokens: Array.isArray(l.tokens) ? l.tokens : [],
                total: Number(tasks.total) || 0,
                running: Number(tasks.running) || 0,
                waiting: Number(tasks.waiting) || 0,
                lastSeenText: lastSeenText(l.lastSeenAt, nowSec),
            };
        });
    }

    // 可绑的令牌：还没绑任何主管的（lead 为空）。leadId 参数按规格保留——
    // 语义上"绑到这个主管的候选"，已绑别家的不该出现在下拉里。
    function bindable(tokens, leadId) {
        var list = Array.isArray(tokens) ? tokens : [];
        return list.filter(function (t) {
            return t && !(t.lead || '');
        });
    }

    function mixin() {
        return {
            data() {
                return {leads: [], leadsState: 'loading', leadsError: '',
                        leadsBusy: false, leadsOk: '',
                        newLeadName: '', newLeadProject: '', leadBindChoice: {}};
            },
            mounted() {
                this.loadLeads();
            },
            methods: {
                async loadLeads() {
                    try {
                        const r = await fetch('/api/settings/leads');
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.leads = view(data, Date.now() / 1000);
                        this.leadsState = 'ok';
                    } catch (err) {
                        // 与「我的权限」同款：加载失败是"不知道"，不是"没有主管"
                        this.leadsState = 'error';
                        this.leadsError = '加载主管列表失败: ' + err.message;
                    }
                },
                async createLead() {
                    if (this.leadsBusy) { return; }
                    this.leadsError = ''; this.leadsOk = '';
                    const name = this.newLeadName.trim();
                    if (!name) { this.leadsError = '请填写主管名称'; return; }
                    this.leadsBusy = true;
                    try {
                        const body = {name: name};
                        if (this.newLeadProject.trim()) { body.project = this.newLeadProject.trim(); }
                        const r = await fetch('/api/settings/leads', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(body),
                        });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.newLeadName = ''; this.newLeadProject = '';
                        this.leadsOk = '已建主管「' + name + '」';
                        setTimeout(() => { this.leadsOk = ''; }, 2500);
                        await this.loadLeads();
                    } catch (err) {
                        this.leadsError = err.message;
                    } finally {
                        this.leadsBusy = false;
                    }
                },
                async setLeadStatus(lead, status) {
                    if (this.leadsBusy) { return; }
                    this.leadsError = ''; this.leadsOk = '';
                    if (status === 'retired') {
                        const ok = await WPCommon.confirm(
                            '停用主管「' + lead.name + '」？它的令牌不能再派新活，已有任务不受影响。',
                            {okText: '停用', danger: true});
                        if (!ok) { return; }
                    }
                    this.leadsBusy = true;
                    try {
                        const r = await fetch('/api/settings/leads/' + encodeURIComponent(lead.id), {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({status: status}),
                        });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadLeads();
                        await this.loadAgentTokens();
                    } catch (err) {
                        this.leadsError = err.message;
                    } finally {
                        this.leadsBusy = false;
                    }
                },
                async bindToken(lead) {
                    if (this.leadsBusy) { return; }
                    this.leadsError = ''; this.leadsOk = '';
                    const tokenId = this.leadBindChoice[lead.id];
                    if (!tokenId) { this.leadsError = '先选一个要绑定的令牌'; return; }
                    this.leadsBusy = true;
                    try {
                        const r = await fetch('/api/settings/leads/' + encodeURIComponent(lead.id)
                                              + '/tokens', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({tokenId: tokenId}),
                        });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        delete this.leadBindChoice[lead.id];
                        await this.loadLeads();
                        await this.loadAgentTokens();
                    } catch (err) {
                        this.leadsError = err.message;
                    } finally {
                        this.leadsBusy = false;
                    }
                },
                async unbindToken(lead, tokenName) {
                    if (this.leadsBusy) { return; }
                    this.leadsError = ''; this.leadsOk = '';
                    // 行里只有令牌名（Q 的列表视图给名字）：按名字在本页令牌表里找 id
                    const token = (this.agentTokens || []).find(function (t) {
                        return t.name === tokenName;
                    });
                    if (!token) {
                        this.leadsError = '本页令牌列表里找不到「' + tokenName + '」，刷新后再试';
                        return;
                    }
                    this.leadsBusy = true;
                    try {
                        const r = await fetch('/api/settings/leads/' + encodeURIComponent(lead.id)
                                              + '/tokens/' + encodeURIComponent(token.id),
                                              {method: 'DELETE'});
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadLeads();
                        await this.loadAgentTokens();
                    } catch (err) {
                        this.leadsError = err.message;
                    } finally {
                        this.leadsBusy = false;
                    }
                },
                async deleteLead(lead) {
                    if (this.leadsBusy) { return; }
                    this.leadsError = ''; this.leadsOk = '';
                    const ok = await WPCommon.confirm(
                        '删除主管「' + lead.name + '」？此操作不可恢复。', {okText: '删除', danger: true});
                    if (!ok) { return; }
                    this.leadsBusy = true;
                    try {
                        const r = await fetch('/api/settings/leads/' + encodeURIComponent(lead.id),
                                              {method: 'DELETE'});
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadLeads();
                    } catch (err) {
                        this.leadsError = err.message;
                    } finally {
                        this.leadsBusy = false;
                    }
                },
            },
        };
    }

    if (typeof window !== 'undefined') {
        window.WPSettingsLeads = {statusText: statusText, kindText: kindText,
                                  lastSeenText: lastSeenText, view: view,
                                  bindable: bindable, mixin: mixin};
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {statusText: statusText, kindText: kindText,
                          lastSeenText: lastSeenText, view: view, bindable: bindable};
    }
})();
