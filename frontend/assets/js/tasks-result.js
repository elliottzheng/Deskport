// 结构化结果呈现（批次 G1）——任务详情顶部结果卡片 + 指挥台任务行小标签。
// 数据来自 GET /api/tasks/<id> 的 task.result（最新精简版，服务端
// modules/task_result.py 校验落库）；纯函数经 module.exports 供 Node 单测
// （tests/js/tasks_result.test.mjs），页面侧经 window.WPTasksResult 的 mixin
// 挂进 tasks.js（照 tasks-overview.js 的形状，批次 U 先例）。
(function () {
    'use strict';

    var GATE_NAMES = {structure: '结构', frontend: '前端'};

    // impl 没有 verdict：交付即过（门禁失败在 gates 里单看）；verify 三态。
    function verdictOf(result) {
        var r = result || {};
        if (r.expects === 'verify') {
            if (r.verdict === 'pass') { return {text: '通过', cls: 'is-pass'}; }
            if (r.verdict === 'rework') { return {text: '返工', cls: 'is-rework'}; }
            return {text: '需判断', cls: 'is-judge'};
        }
        if (r.expects === 'impl') { return {text: '已交付', cls: 'is-pass'}; }
        return {text: '有结果', cls: 'is-pass'};
    }

    function testsText(tests) {
        var t = tests || {};
        var sum = (Number(t.passed) || 0) + (Number(t.failed) || 0) + (Number(t.skipped) || 0);
        if (!sum) { return ''; }
        return (Number(t.passed) || 0) + '过/' + (Number(t.failed) || 0) + '败/'
            + (Number(t.skipped) || 0) + '跳过';
    }

    function commitOf(result) {
        var r = result || {};
        return r.commit || r.target || '';
    }

    function gateItems(gates) {
        var out = [];
        var g = gates || {};
        Object.keys(GATE_NAMES).forEach(function (key) {
            if (g[key]) {
                out.push({name: GATE_NAMES[key], value: g[key]});
            }
        });
        return out;
    }

    // 结果卡视图：模板只读这一份（不在模板里翻字典）。
    function cardView(result) {
        var r = result || {};
        return {
            expects: r.expects || '',
            verdict: verdictOf(r),
            commit: commitOf(r),
            branch: r.branch || '',
            summary: r.summary || '',
            testsText: testsText(r.tests),
            gates: gateItems(r.gates),
            risks: r.risks || [],
            questions: r.questions || [],
            mustFix: r.mustFix || [],
            evidence: r.evidence || [],
            unverified: r.unverified || [],
            via: r.via || '',
        };
    }

    // 指挥台任务行小标签：verdict + 测试数（或短 commit）。无结果 → ''。
    function rowLabel(result) {
        var r = result || {};
        if (!r.summary && !r.expects && !r.verdict && !commitOf(r)) { return ''; }
        var bits = [verdictOf(r).text];
        var t = testsText(r.tests);
        if (t) {
            bits.push(t);
        } else if (commitOf(r)) {
            bits.push(commitOf(r).slice(0, 7));
        }
        return bits.join(' · ');
    }

    function mixin() {
        return {
            methods: {
                resultCardView: function (result) { return cardView(result); },
                resultVerdictText: function (result) { return verdictOf(result).text; },
                resultVerdictClass: function (result) { return verdictOf(result).cls; },
                resultTestsText: function (result) { return testsText(result && result.tests); },
                resultCommit: function (result) { return commitOf(result); },
                resultGateItems: function (result) { return gateItems(result && result.gates); },
                resultRowLabel: function (result) { return rowLabel(result); },
            },
        };
    }

    if (typeof window !== 'undefined') {
        window.WPTasksResult = {
            verdictOf: verdictOf, testsText: testsText, commitOf: commitOf,
            gateItems: gateItems, cardView: cardView, rowLabel: rowLabel,
            mixin: mixin,
        };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            verdictOf: verdictOf, testsText: testsText, commitOf: commitOf,
            gateItems: gateItems, cardView: cardView, rowLabel: rowLabel,
        };
    }
})();
