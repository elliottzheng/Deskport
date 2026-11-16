// 任务页「按主管筛」（批次 S）— 数据来自 GET /api/settings/leads（与设置页同一
// 端点；失败/空列表时整排不显示，任务列表照常，规格 S3）。纯函数
// （leadName/filterItems/taskListUrl）经 module.exports 供 Node 单测；
// 页面侧经 window.WPTasksLeads 的 mixin 挂进 tasks.js——tasks.js 只多两行：
// refresh 的 URL 带上 leadFilter、挂载 mixin。
(function () {
    'use strict';

    var RETIRED = 'retired';

    // 主管 id → 显示名：列表里找得到用名字，找不到（没加载出来）退回 id，
    // 没有主管（未绑定/老任务）返回空——调用方据此决定显不显示这一段。
    function leadName(leads, leadId) {
        var id = leadId || '';
        if (!id) { return ''; }
        var list = Array.isArray(leads) ? leads : [];
        var hit = list.find(function (l) { return l && l.id === id; });
        return (hit && hit.name) || id;
    }

    // 筛选排的项：全部 + 每个非停用的主管（停用的不该再筛新任务）。
    function filterItems(leads) {
        var list = Array.isArray(leads) ? leads : [];
        return list.filter(function (l) {
            return l && (l.status || '') !== RETIRED;
        });
    }

    // 列表 URL：带筛选才加 lead 参数（不带时与现在逐字一致）。
    function taskListUrl(base, leadFilter) {
        var url = base || '/api/tasks?full=1';
        var f = leadFilter || '';
        return f ? url + '&lead=' + encodeURIComponent(f) : url;
    }

    function mixin() {
        return {
            data() {
                return {leads: [], leadsState: 'loading', leadFilter: ''};
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
                        this.leads = data.leads || [];
                        this.leadsState = 'ok';
                    } catch (err) {
                        // 筛选用不了不影响看任务：整排藏起来，列表照常
                        this.leads = [];
                        this.leadsState = 'error';
                    }
                },
                leadName(leadId) {
                    return leadName(this.leads, leadId);
                },
                filterItems() {
                    return filterItems(this.leads);
                },
                setLeadFilter(id) {
                    this.leadFilter = id || '';
                    this.refresh();
                },
            },
        };
    }

    if (typeof window !== 'undefined') {
        window.WPTasksLeads = {leadName: leadName, filterItems: filterItems,
                               taskListUrl: taskListUrl, mixin: mixin};
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {leadName: leadName, filterItems: filterItems,
                          taskListUrl: taskListUrl};
    }
})();
