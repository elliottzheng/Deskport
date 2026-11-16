// 指挥台一屏（批次 U）— 数据来自 GET /api/command/overview（后端
// modules/command_view.py 只读聚合 tasks_store/leads_store/leads_inbox）。
// 纯函数经 module.exports 供 Node 单测（tests/js/tasks_overview.test.mjs）；
// 页面侧经 window.WPTasksOverview 的 mixin 挂进 tasks.js（照 settings-leads.js
// 的形状，批次 S 先例）。主管文案（kindText/lastSeenText）不复制实现：调
// window.WPSettingsLeads 的同名纯函数（settings-leads.js，本页一并加载）。
(function () {
    'use strict';

    var STATUS_TEXT = {
        queued: '排队中', running: '运行中', waiting: '等你继续', paused: '已休眠',
        closed: '已结束', done: '完成', failed: '失败',
        cancelled: '已取消', interrupted: '已中断',
    };
    var MODE_TEXT = {
        auto: '自动批准', default: '逐项批准', plan: '只读规划', yolo: '全部放行',
    };
    var STATE_TEXT = {busy: '在忙', asleep: '睡着', waiting_you: '等你',
                      paused: '已暂停'};
    var KIND_TEXT = {permission: '审批', report: '汇报', notice: '提醒', problem: '问题', stall: '卡住'};
    var OVERVIEW_INTERVAL = 5000;         // 5 秒一刷；页面不可见时暂停（mixin 里挡）

    // 任务状态/模式文案：从 tasks.js 的 statusLabel/modeLabel 原样搬来
    // （详情面板与指挥台行共用一份，详情面板经 mixin 方法继续调）。
    function taskStatusText(status) {
        return STATUS_TEXT[status] || status || '-';
    }

    function taskModeText(mode) {
        return MODE_TEXT[mode] || mode || 'auto';
    }

    function stateText(state) {
        return STATE_TEXT[state] || state || '-';
    }

    // 首行 + 截断（汇报/原因都只要第一句）；截断时补省略号。
    function firstLine(text, max) {
        var line = String(text === null || text === undefined ? '' : text).trim().split('\n')[0].trim();
        var limit = Number(max) || 0;
        return limit && line.length > limit ? line.slice(0, limit) + '…' : line;
    }

    function countsText(counts) {
        // 树节点用 waiting_you（规格 U2 键名），旧 leads[].counts 用 waiting
        var c = counts || {};
        var waiting = c.waiting_you !== undefined ? c.waiting_you : c.waiting;
        return '在跑 ' + (Number(c.running) || 0) + ' · 等你 '
            + (Number(waiting) || 0) + ' · 出错 ' + (Number(c.failed) || 0);
    }

    // overview 的 summaryBrief → 行视图。agoText 由调用方给（WPCommon.fmtAgo 的产物，
    // 相对时间格式全站只许 common.js 一份）。resultLabel（批次 G1）：纯函数在
    // tasks-result.js，node 单测环境没有该全局时按空处理。
    function taskRowView(task, agoText) {
        var t = task || {};
        var bits = [];
        var node = t.node || 'local';
        if (node) { bits.push(node); }
        var model = t.modelEffective || t.model || '';
        if (model) { bits.push(model); }
        if (agoText) { bits.push('最近 ' + agoText); }
        var label = typeof WPTasksResult === 'undefined' ? '' : WPTasksResult.rowLabel(t.result);
        return {
            id: t.id || '',
            title: t.title || t.prompt || '(无标题)',
            status: t.status || '',
            statusText: taskStatusText(t.status),
            dotClass: 'task-dot is-' + (t.status || ''),
            node: node,
            model: model,
            lastActiveAt: t.lastActiveAt || 0,
            sub: bits.join(' · '),
            resultLabel: label,
        };
    }

    // 后端 pending 三类 → 显示行：补 kindText、主管名（找不到退回 id）、稳定
    // key，并按时间倒序（规格：一条按时间倒序的清单）；retiredById=停用主管 id→名字。
    function pendingRows(pending, leadsById, retiredById) {
        var byId = leadsById || {};
        var retired = retiredById || {};
        var rows = (Array.isArray(pending) ? pending : []).map(function (p) {
            var kind = p.kind || '';
            var lead = byId[p.leadId] || null;
            return {
                key: kind + ':' + (p.taskId || p.leadId || '') + ':'
                    + (p.requestId || p.seq || ''),
                kind: kind,
                kindText: KIND_TEXT[kind] || kind || '待办',
                title: p.title || '',
                detail: p.detail || '',
                // U3：审批行的「下属自述的用途」与派活令牌名（后端 _pendingPermission）
                purpose: p.purpose || '',
                owner: p.owner || '',
                at: p.at || 0,
                taskId: p.taskId || '',
                leadId: p.leadId || '',
                leadName: lead ? (lead.name || '未命名')
                               : (retired[p.leadId] || ''),
                requestId: p.requestId || '',
                options: Array.isArray(p.options) ? p.options : [],
                seq: p.seq || 0,
            };
        });
        rows.sort(function (a, b) { return (b.at || 0) - (a.at || 0); });
        return rows;
    }

    // 「更多」：把 /api/tasks?full=1&lead=<id> 拉回来的较全列表并进卡片里的
    // 裁剪列表（按 id 去重，不重复、不打乱原顺序）。
    function mergeMoreTasks(current, more) {
        var out = Array.isArray(current) ? current.slice() : [];
        var seen = {};
        out.forEach(function (t) { if (t && t.id) { seen[t.id] = true; } });
        (Array.isArray(more) ? more : []).forEach(function (t) {
            if (t && t.id && !seen[t.id]) {
                seen[t.id] = true;
                out.push(t);
            }
        });
        return out;
    }

    // 新建主管的两个类型：本批只做外部接入；托管置灰（批次 A 落地后接上）。
    function newLeadKindChoices() {
        return [
            {kind: 'external', title: '外部接入',
             hint: '对方拿接入包连进来；令牌在设置页签发并绑定', enabled: true},
            {kind: 'hosted', title: '托管',
             hint: '即将支持（批次 A 落地后由它接上）', enabled: false},
        ];
    }

    function ago(ts) {
        return (typeof WPCommon !== 'undefined' && WPCommon.fmtAgo)
            ? WPCommon.fmtAgo(ts) : '';
    }

    function mixin() {
        return {
            data() {
                return {
                    overview: null,
                    overviewState: 'loading',   // loading | ok | error（仅首屏）
                    overviewError: '',
                    overviewTimer: null,
                    newSheetOpen: false,
                    newSheetTab: '',            // '' 选项 | 'lead' | 'task'
                    newLeadName: '',
                    newLeadProject: '',
                    newLeadBusy: false,
                    newLeadOk: '',
                    newLeadError: '',
                    speakOpen: false,
                    speakLeadId: '',
                    speakLeadName: '',
                    speakText: '',
                    speakBusy: false,
                    speakError: '',
                    rowErrors: {}, leadErrors: {},
                };
            },
            computed: {
                leadsById() {
                    var map = {};
                    ((this.overview && this.overview.leads) || [])
                        .forEach(function (l) { map[l.id] = l; });
                    return map;
                },
                pendingRows() {
                    return pendingRows(this.overview && this.overview.pending,
                                       this.leadsById, (this.overview && this.overview.retiredLeads) || {});
                },
                newLeadKinds() {
                    return newLeadKindChoices();
                },
            },
            mounted() {
                this.loadOverview();
                this.overviewTimer = setInterval(this.loadOverview,
                                                 OVERVIEW_INTERVAL);
            },
            beforeUnmount() {
                clearInterval(this.overviewTimer);
            },
            methods: {
                // 模板直接调的纯函数：复用同一实现，不复制
                statusLabel: taskStatusText,
                modeLabel: taskModeText,
                fmtAgo: function (ts) { return ago(ts); },
                leadNameOf(leadId) {
                    // 停用/悬空的主管不露原始 id：详情里显示「已停用的主管」
                    var lead = this.leadsById[leadId || ''];
                    return leadId ? (lead ? (lead.name || leadId) : '已停用的主管') : '';
                },
                async loadOverview() {
                    if (document.visibilityState !== 'visible') { return; }
                    try {
                        const r = await fetch('/api/command/overview');
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.overview = data;
                        this.overviewState = 'ok';
                        this.overviewError = '';
                    } catch (err) {
                        // 500/网络失败：留着旧数据 + 报错条 + 重试，不白屏（例 10）
                        this.overviewState = 'error';
                        this.overviewError = err.message;
                    }
                },
                openNewSheet(tab) {
                    this.newSheetOpen = true;
                    this.newSheetTab = tab || '';
                    this.newLeadError = '';
                    this.newLeadOk = '';
                },
                async createExternalLead() {
                    var name = (this.newLeadName || '').trim();
                    if (!name || this.newLeadBusy) { return; }
                    this.newLeadBusy = true;
                    this.newLeadError = '';
                    this.newLeadOk = '';
                    try {
                        var body = {name: name, kind: 'external'};
                        var project = (this.newLeadProject || '').trim();
                        if (project) { body.project = project; }
                        const r = await fetch('/api/settings/leads', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(body),
                        });
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.newLeadOk = '已建成外部主管「' + name
                            + '」——到设置页给它签发令牌并绑定，它就能派活';
                        this.newLeadName = '';
                        this.newLeadProject = '';
                        await this.loadOverview();
                    } catch (err) {
                        this.newLeadError = err.message;
                    } finally {
                        this.newLeadBusy = false;
                    }
                },
                async toggleLeadPause(lead) {
                    var status = lead.status === 'paused' ? 'active' : 'paused';
                    try {
                        const r = await fetch('/api/settings/leads/'
                                              + encodeURIComponent(lead.id), {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({status: status}),
                        });
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadOverview();
                        WPCommon.clearError(this, 'leadErrors', lead.id);
                    } catch (err) {
                        WPCommon.flashError(this, 'leadErrors', lead.id, err.message);
                    }
                },
                speakToLead(leadId) {
                    var lead = this.leadsById[leadId];
                    this.speakLeadId = leadId || '';
                    this.speakLeadName = lead ? (lead.name || leadId)
                                              : (leadId || '');
                    this.speakText = '';
                    this.speakError = '';
                    this.speakOpen = true;
                },
                async sendSpeak() {
                    var text = (this.speakText || '').trim();
                    if (!text || !this.speakLeadId || this.speakBusy) { return; }
                    this.speakBusy = true;
                    this.speakError = '';
                    try {
                        const r = await fetch('/api/settings/leads/'
                                              + encodeURIComponent(this.speakLeadId)
                                              + '/inbox', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({text: text}),
                        });
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.speakOpen = false;
                        this.speakText = '';
                        await this.loadOverview();
                    } catch (err) {
                        this.speakError = err.message;
                    } finally {
                        this.speakBusy = false;
                    }
                },
                findTask(taskId) {
                    // 树里找（裁剪掉的/更早的已关闭不在这），找不到由 openTask 拉详情
                    var found = null;
                    var walk = function (nodes) {
                        (Array.isArray(nodes) ? nodes : []).forEach(function (n) {
                            if (found) { return; }
                            if (n.type === 'task' && n.id === taskId) { found = n; }
                            else { walk(n.children); }
                        });
                    };
                    walk((this.overview || {}).tree);
                    return found;
                },
                async openTask(taskId, row) {
                    var hit = this.findTask(taskId);
                    if (hit) {
                        await this.select(hit);
                        return;
                    }
                    // 裁剪掉的任务（如更老的已关闭）：直接拉详情
                    try {
                        const r = await fetch('/api/tasks/'
                                              + encodeURIComponent(taskId));
                        const data = await r.json();
                        if (r.ok && data.task) {
                            await this.select(data.task);
                        } else {
                            this.error = (data && data.error) || ('HTTP ' + r.status);
                            if (row) { WPCommon.flashError(this, 'rowErrors', row.key, this.error); }
                        }
                    } catch (err) {
                        this.error = err.message;
                        if (row) { WPCommon.flashError(this, 'rowErrors', row.key, err.message); }
                    }
                },
                async resolvePendingPermission(row, option) {
                    if (!row.taskId || !row.requestId) { return; }
                    var body = option
                        ? {requestId: row.requestId, optionId: option.optionId}
                        : {requestId: row.requestId, deny: true};
                    try {
                        const r = await fetch('/api/tasks/'
                                              + encodeURIComponent(row.taskId)
                                              + '/permission', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(body),
                        });
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadOverview();
                        WPCommon.clearError(this, 'rowErrors', row.key);
                    } catch (err) {
                        WPCommon.flashError(this, 'rowErrors', row.key, err.message);
                    }
                },
                async markReportRead(row) {
                    if (!row.leadId || !row.seq) { return; }
                    try {
                        const r = await fetch('/api/command/leads/'
                                              + encodeURIComponent(row.leadId)
                                              + '/reports/read', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({seq: row.seq}),
                        });
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadOverview();
                        WPCommon.clearError(this, 'rowErrors', row.key);
                    } catch (err) {
                        WPCommon.flashError(this, 'rowErrors', row.key, err.message);
                    }
                },
                async closeProblemTask(row) {
                    if (!row.taskId) { return; }
                    var ok = await WPCommon.confirm(
                        '关闭任务「' + row.title + '」？之后不能再继续追问。',
                        {okText: '关闭', danger: true});
                    if (!ok) { return; }
                    try {
                        const r = await fetch('/api/tasks/'
                                              + encodeURIComponent(row.taskId)
                                              + '/close', {method: 'POST'});
                        const data = await r.json();
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.loadOverview();
                        WPCommon.clearError(this, 'rowErrors', row.key);
                    } catch (err) {
                        WPCommon.flashError(this, 'rowErrors', row.key, err.message);
                    }
                },
            },
        };
    }

    if (typeof window !== 'undefined') {
        window.WPTasksOverview = {
            taskStatusText: taskStatusText, taskModeText: taskModeText,
            stateText: stateText, firstLine: firstLine, countsText: countsText,
            taskRowView: taskRowView,
            pendingRows: pendingRows, mergeMoreTasks: mergeMoreTasks,
            newLeadKindChoices: newLeadKindChoices, mixin: mixin,
        };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            taskStatusText: taskStatusText, taskModeText: taskModeText,
            stateText: stateText, firstLine: firstLine, countsText: countsText,
            taskRowView: taskRowView,
            pendingRows: pendingRows, mergeMoreTasks: mergeMoreTasks,
            newLeadKindChoices: newLeadKindChoices,
        };
    }
})();
