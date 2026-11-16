// 工作区面板（批次 G2）——指挥台「工作项」：一行一个（标题、阶段进度条
// 实施/验收/集成、attempt、最新 verdict），展开为其下任务；needs_judgment
// 行进「等你处理」（后端 overview 的 kind='workitem' 行）并可直接决策。
// 数据来自 GET /api/workitems 与 /api/workitems/<id>；纯函数经
// module.exports 供 Node 单测（tests/js/workitems.test.mjs），页面侧经
// window.WPWorkItems 的 mixin 挂进 tasks.js（照 tasks-result.js 的形状）。
(function () {
    'use strict';

    var STAGE_TEXT = {
        impl: '实施', verify: '验收', integrate: '集成',
        queue: '排队', done: '已集成', judge: '待判断',
        fail: '失败', cancelled: '已取消'
    };
    var STATE_TEXT = {
        queued: '排队中', implementing: '实施中', verifying: '验收中',
        integrating: '集成中', integrated: '已集成',
        needs_judgment: '待判断', failed: '失败', cancelled: '已取消'
    };
    // 阶段条三段：实施 → 验收 → 集成（done 全亮；queue/judge/fail 不高亮）
    var STAGE_STEPS = [
        {key: 'impl', label: '实施'},
        {key: 'verify', label: '验收'},
        {key: 'integrate', label: '集成'}
    ];
    var VERDICT_TEXT = {pass: '通过', rework: '返工', needs_judgment: '需判断'};

    // 阶段条：返回 [{key,label,state}]，state ∈ done/active/todo
    function stageBar(stage) {
        var order = {impl: 0, verify: 1, integrate: 2, done: 3};
        var at = order[stage];
        return STAGE_STEPS.map(function (step, i) {
            var state = 'todo';
            if (stage === 'done') { state = 'done'; }
            else if (at !== undefined && i < at) { state = 'done'; }
            else if (at !== undefined && i === at) { state = 'active'; }
            return {key: step.key, label: step.label, state: state};
        });
    }

    function stageText(stage) {
        return STAGE_TEXT[stage] || stage || '';
    }

    function stateText(state) {
        return STATE_TEXT[state] || state || '';
    }

    function verdictText(verdict) {
        return VERDICT_TEXT[verdict] || verdict || '';
    }

    function fmtTime(ts) {
        if (!ts) { return ''; }
        var d = new Date(ts * 1000);
        var pad = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
            + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    function rowOf(w) {
        return {
            id: w.id || '',
            title: w.title || w.id || '(无标题)',
            state: w.state || '',
            stateText: stateText(w.state),
            stage: w.stage || '',
            stageBar: stageBar(w.stage),
            attempt: w.attempt || 0,
            maxAttempts: w.maxAttempts || 0,
            attemptText: 'attempt ' + (w.attempt || 0) + '/' + (w.maxAttempts || 0),
            verdict: w.lastVerdict || '',
            verdictText: verdictText(w.lastVerdict),
            lastImplCommit: (w.lastImplCommit || '').slice(0, 12),
            lastImplSummary: w.lastImplSummary || '',
            queuedFor: w.queuedFor || '',
            queuedShare: Array.isArray(w.queuedShare) ? w.queuedShare : [],
            queueHint: w.state === 'queued' && w.queuedFor
                ? '等待 ' + (w.queuedFor || '').slice(0, 8)
                  + '（共享 ' + (w.queuedShare || []).join('、') + '）'
                : '',
            judgmentReason: w.judgmentReason || '',
            mustFix: Array.isArray(w.mustFix) ? w.mustFix : [],
            integration: w.integration || {},
            updatedAt: w.updatedAt || 0,
            updatedText: fmtTime(w.updatedAt),
            projectId: w.projectId || ''
        };
    }

    function mixin() {
        return {
            data: function () {
                return {
                    workItems: [],
                    workItemsError: '',
                    workItemsOpen: {},
                    workItemDetail: {},
                    workItemBusy: {},
                    workItemTimer: null
                };
            },
            computed: {
                workItemRows: function () {
                    var self = this;
                    var open = this.workItemsOpen || {};
                    return this.workItems.map(function (w) {
                        var row = rowOf(w);
                        row.open = !!open[row.id];
                        row.detail = self.workItemDetail[row.id] || null;
                        row.tasks = (row.detail && row.detail.tasks) || [];
                        row.busy = !!self.workItemBusy[row.id];
                        return row;
                    });
                },
                workItemJudging: function () {
                    return this.workItemRows.filter(function (r) {
                        return r.state === 'needs_judgment';
                    });
                }
            },
            mounted: function () {
                this.loadWorkItems();
                var self = this;
                this.workItemTimer = setInterval(function () {
                    if (document.hidden) { return; }
                    self.loadWorkItems();
                }, 5000);
            },
            beforeUnmount: function () {
                if (this.workItemTimer) { clearInterval(this.workItemTimer); }
            },
            methods: {
                loadWorkItems: function () {
                    var self = this;
                    fetch('/api/workitems')
                        .then(function (r) { return r.json(); })
                        .then(function (d) {
                            self.workItemsError = d.error || '';
                            self.workItems = d.workItems || [];
                        })
                        .catch(function (err) {
                            self.workItemsError = String(err);
                        });
                },
                toggleWorkItem: function (row) {
                    var open = this.workItemsOpen || {};
                    if (open[row.id]) {
                        open[row.id] = false;
                        this.workItemsOpen = Object.assign({}, open);
                        return;
                    }
                    open[row.id] = true;
                    this.workItemsOpen = Object.assign({}, open);
                    var self = this;
                    fetch('/api/workitems/' + encodeURIComponent(row.id))
                        .then(function (r) { return r.json(); })
                        .then(function (d) {
                            if (d.workItem) {
                                self.workItemDetail = Object.assign(
                                    {}, self.workItemDetail,
                                    {[row.id]: d.workItem});
                            }
                        })
                        .catch(function () { /* 展开失败留着空详情，下一轮再拉 */ });
                },
                wiSetBusy: function (row, busy) {
                    this.workItemBusy = Object.assign({}, this.workItemBusy,
                                                      {[row.id]: busy});
                },
                wiDecide: function (row, action) {
                    var text = '';
                    if (action === 'continue' || action === 'rework') {
                        text = window.prompt(
                            action === 'rework' ? '返工要点（一句话）' : '给执行方的说明（可空）',
                            '') || '';
                    }
                    if (action === 'cancel') {
                        var ok = window.confirm('取消工作项「' + row.title
                                                + '」？会关掉名下任务并拆掉 worktree。');
                        if (!ok) { return; }
                    }
                    var self = this;
                    this.wiSetBusy(row, true);
                    fetch('/api/workitems/' + encodeURIComponent(row.id) + '/decision', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({action: action, text: text})
                    })
                        .then(function (r) { return r.json(); })
                        .then(function (d) {
                            self.wiSetBusy(row, false);
                            if (d.error) {
                                self.workItemsError = d.error;
                                return;
                            }
                            self.workItemsError = '';
                            self.loadWorkItems();
                            if (self.workItemsOpen[row.id]) {
                                return fetch('/api/workitems/'
                                             + encodeURIComponent(row.id))
                                    .then(function (r) { return r.json(); })
                                    .then(function (dd) {
                                        if (dd.workItem) {
                                            self.workItemDetail = Object.assign(
                                                {}, self.workItemDetail,
                                                {[row.id]: dd.workItem});
                                        }
                                    });
                            }
                            return null;
                        })
                        .catch(function (err) {
                            self.wiSetBusy(row, false);
                            self.workItemsError = String(err);
                        });
                },
                wiCancel: function (row) {
                    this.wiDecide(row, 'cancel');
                },
                // 「等你处理」里的工作项行（后端 overview 的 kind='workitem'）：
                // 只有 id/标题/原因，拼一个最小 row 走同一套决策。
                wiDecidePending: function (p, action) {
                    this.wiDecide({
                        id: p.workItemId || '',
                        title: p.title || '工作项'
                    }, action);
                },
                wiStateClass: function (state) {
                    if (state === 'needs_judgment') { return 'you'; }
                    if (state === 'integrated') { return 'ok'; }
                    if (state === 'failed') { return 'failed'; }
                    if (state === 'queued') { return 'plain'; }
                    return 'kind';
                },
                wiTaskStatusText: function (status) {
                    return typeof taskStatusText === 'function' ? taskStatusText(status)
                        : (status || '');
                }
            }
        };
    }

    if (typeof window !== 'undefined') {
        window.WPWorkItems = {
            stageBar: stageBar, stageText: stageText, stateText: stateText,
            verdictText: verdictText, rowOf: rowOf, fmtTime: fmtTime,
            mixin: mixin
        };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            stageBar: stageBar, stageText: stageText, stateText: stateText,
            verdictText: verdictText, rowOf: rowOf, fmtTime: fmtTime,
        };
    }
})();
