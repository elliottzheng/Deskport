// 设置页「反馈」卡片（批次 F1，规格 §6.1 处理端视图）：列表（状态/路由/提交者/
// 归属/日期/超时筛选，待受理优先）+ 详情（信封全文只读、截图、trace、errors、
// history、评论）+ 操作（流转、追问、批量受理）。
// 放在设置页而不是反馈面板加页签：处理视图要同时放筛选列表与信封全文/trace/
// 历史/评论，设置卡片有整页宽度和现成卡片模式；反馈面板是 720px 快速捕获浮层
// （手机是底部抽屉），塞不下 triage 详情，且已近 500 行预算。
// 纯函数经 module.exports 供 tests/js/ 的 node 单测；页面侧经
// window.WPSettingsFeedback 的 mixin 挂进 settings.js。
(function () {
    'use strict';

    var STATUS_TEXT = {
        submitted: '待受理', acknowledged: '已收到', processing: '处理中',
        resolved: '已解决', rejected: '未采纳', user_confirmed: '已确认'
    };

    // 客户端镜像：与服务端 modules/feedback_status.py 的 TRANSITIONS/ACTORS 一致
    // （规格 §5 末条：状态机定义在服务端一处，客户端镜像注释指向它，二者靠
    // tests/js/settings_feedback.test.mjs 与 tests/test_feedback_loop.py 里
    // 同一张共享测试数据保持一致）。这里只放处理者（handler）可走的边。
    var HANDLER_NEXT = {
        submitted: ['acknowledged'],
        acknowledged: ['processing', 'rejected'],
        processing: ['resolved', 'rejected'],
        resolved: [],
        rejected: ['processing'],
        user_confirmed: []
    };
    var REASON_REQUIRED = [
        ['processing', 'resolved'], ['acknowledged', 'rejected'],
        ['processing', 'rejected'], ['resolved', 'processing']
    ];

    function statusText(status) {
        return STATUS_TEXT[status] || status || '待受理';
    }

    function needsReason(from, to) {
        return REASON_REQUIRED.some(function (p) { return p[0] === from && p[1] === to; });
    }

    function nextOptions(item) {
        var from = (item && item.status) || 'submitted';
        return (HANDLER_NEXT[from] || []).map(function (to) {
            return { value: to, text: statusText(to) + (needsReason(from, to) ? '（必填说明）' : ''),
                     reason: needsReason(from, to) };
        });
    }

    function submitterText(submitter) {
        var s = submitter || {};
        var name = s.display_name || s.id || '未知';
        if (s.kind === 'agent') { name += '（智能体）'; }
        if (s.group) { name += ' · ' + s.group; }
        return name;
    }

    function rowView(item) {
        var src = item || {};
        return {
            id: src.feedback_id || '',
            status: src.status || 'submitted',
            statusText: statusText(src.status),
            submitter: submitterText(src.submitter),
            route: src.route || '',
            date: (src.submitted_at || '').slice(0, 16).replace('T', ' '),
            overdue: !!src.overdue,
            unread: !!src.unread,
            message: (src.message || '').slice(0, 60),
            pageId: src.page_id || ''
        };
    }

    function traceRow(entry) {
        var e = entry || {};
        var parts = [];
        if (typeof e.t === 'number') { parts.push((e.t / 1000).toFixed(2) + 's'); }
        parts.push(e.action || e.kind || '');
        parts.push(e.target || '');
        if (e.status) { parts.push('→ ' + e.status); }
        if (e.duration_ms) { parts.push((e.duration_ms / 1000).toFixed(1) + 's'); }
        return parts.filter(Boolean).join(' ');
    }

    function historyRow(entry) {
        var e = entry || {};
        var to = e.to || e.status || '';
        var from = e.from || '';
        var who = e.actor === 'system' ? '系统' : (e.actor === 'handler' ? '处理者' : '提交者');
        var reason = e.reason || e.note || '';
        return (e.at || '').slice(0, 16).replace('T', ' ') + ' · ' + who + '：' +
            (from ? from + ' → ' + to : to) + (reason ? '（' + reason + '）' : '');
    }

    function listQuery(filter, page, size) {
        var f = filter || {};
        var params = [];
        if (f.status) { params.push('status=' + encodeURIComponent(f.status)); }
        if (f.route) { params.push('route=' + encodeURIComponent(f.route)); }
        if (f.submitter) { params.push('submitter=' + encodeURIComponent(f.submitter)); }
        if (f.group) { params.push('group=' + encodeURIComponent(f.group)); }
        if (f.overdue) { params.push('overdue=1'); }
        params.push('page=' + (page || 1));
        params.push('size=' + (size || 20));
        return '/api/feedback?' + params.join('&');
    }

    function mixin() {
        return {
            data() {
                return {
                    fbState: 'loading',        // loading | ok | error：没拿到数据不下"没有反馈"的结论
                    fbError: '',
                    fbPending: 0, fbOverdue: 0,
                    fbItems: [], fbTotal: 0, fbPage: 1, fbSize: 20,
                    fbFilter: { status: '', route: '', submitter: '', group: '', overdue: false },
                    fbChecked: [],
                    fbDetail: null,
                    fbMove: { status: '', reason: '' },
                    fbComment: '',
                    fbBusy: false, fbNote: ''
                };
            },
            computed: {
                fbRows() { return this.fbItems.map(rowView); }
            },
            mounted() {
                this.loadFeedbackAll();
            },
            methods: {
                statusText: statusText,
                nextOptions: nextOptions,
                submitterText: submitterText,
                traceRow: traceRow,
                historyRow: historyRow,
                async loadFeedbackAll() {
                    await Promise.all([this.loadFeedbackPending(), this.loadFeedbackList()]);
                },
                async loadFeedbackPending() {
                    try {
                        const r = await fetch('/api/feedback/pending', { credentials: 'same-origin' });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.fbPending = data.pending || 0;
                        this.fbOverdue = data.overdue || 0;
                        this.fbState = 'ok';
                    } catch (err) {
                        this.fbState = 'error';
                        this.fbError = '待受理数没加载出来：' + err.message;
                    }
                },
                async loadFeedbackList() {
                    try {
                        const r = await fetch(listQuery(this.fbFilter, this.fbPage, this.fbSize),
                                              { credentials: 'same-origin' });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.fbItems = data.items || [];
                        this.fbTotal = data.total || 0;
                        this.fbChecked = [];
                        this.fbState = 'ok';
                    } catch (err) {
                        this.fbState = 'error';
                        this.fbError = '反馈列表没加载出来：' + err.message;
                    }
                },
                async openFeedback(id) {
                    this.fbNote = '';
                    try {
                        const r = await fetch('/api/feedback/' + encodeURIComponent(id),
                                              { credentials: 'same-origin' });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.fbDetail = data.feedback || null;
                        this.fbMove = { status: '', reason: '' };
                        this.fbComment = '';
                    } catch (err) {
                        this.fbNote = '详情读取失败：' + err.message;
                    }
                },
                closeFeedback() {
                    this.fbDetail = null;
                    this.loadFeedbackAll();
                },
                fbMoveText(item) {
                    return statusText(item && item.status);
                },
                async submitFeedbackMove() {
                    if (!this.fbDetail || !this.fbMove.status) { return; }
                    if (this.fbBusy) { return; }
                    this.fbBusy = true;
                    this.fbNote = '';
                    try {
                        const r = await fetch('/api/feedback/' + encodeURIComponent(this.fbDetail.feedback_id)
                                              + '/status', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'same-origin',
                            body: JSON.stringify({ status: this.fbMove.status,
                                                   reason: this.fbMove.reason.trim() })
                        });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        await this.openFeedback(this.fbDetail.feedback_id);
                        await this.loadFeedbackAll();
                    } catch (err) {
                        this.fbNote = err.message;
                    } finally {
                        this.fbBusy = false;
                    }
                },
                async submitFeedbackComment() {
                    if (!this.fbDetail) { return; }
                    const text = (this.fbComment || '').trim();
                    if (!text) { return; }
                    if (this.fbBusy) { return; }
                    this.fbBusy = true;
                    this.fbNote = '';
                    try {
                        const r = await fetch('/api/feedback/' + encodeURIComponent(this.fbDetail.feedback_id)
                                              + '/comments', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'same-origin',
                            body: JSON.stringify({ body: text })
                        });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        this.fbComment = '';
                        await this.openFeedback(this.fbDetail.feedback_id);
                    } catch (err) {
                        this.fbNote = err.message;
                    } finally {
                        this.fbBusy = false;
                    }
                },
                async batchAcknowledge() {
                    if (!this.fbChecked.length || this.fbBusy) { return; }
                    this.fbBusy = true;
                    this.fbNote = '';
                    try {
                        const r = await fetch('/api/feedback/batch', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'same-origin',
                            body: JSON.stringify({ ids: this.fbChecked })
                        });
                        const data = await r.json().catch(() => ({}));
                        if (!r.ok) { throw new Error(data.error || ('HTTP ' + r.status)); }
                        const skipped = (data.skipped || []).length;
                        this.fbNote = '已受理 ' + (data.ok || []).length + ' 条' +
                            (skipped ? '，跳过 ' + skipped + ' 条（非待受理或不存在）' : '');
                        await this.loadFeedbackAll();
                    } catch (err) {
                        this.fbNote = err.message;
                    } finally {
                        this.fbBusy = false;
                    }
                },
                resetFeedbackFilter() {
                    this.fbFilter = { status: '', route: '', submitter: '', group: '', overdue: false };
                    this.fbPage = 1;
                    this.loadFeedbackList();
                }
            }
        };
    }

    if (typeof window !== 'undefined') {
        window.WPSettingsFeedback = { statusText: statusText, nextOptions: nextOptions,
                                      submitterText: submitterText, rowView: rowView,
                                      traceRow: traceRow, historyRow: historyRow,
                                      listQuery: listQuery, mixin: mixin };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { statusText: statusText, nextOptions: nextOptions,
                           submitterText: submitterText, rowView: rowView,
                           traceRow: traceRow, historyRow: historyRow,
                           listQuery: listQuery };
    }
})();
