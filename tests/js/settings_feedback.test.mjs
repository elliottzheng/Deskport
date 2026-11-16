// 设置页「反馈」处理卡片（settings-feedback.js）纯函数单测。
// 跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fb from '../../frontend/assets/js/settings-feedback.js';

const {statusText, nextOptions, submitterText, rowView, traceRow, historyRow, listQuery} = fb;

// 共享测试数据：与 modules/feedback_status.py 的 TRANSITIONS/ACTORS/REASON_REQUIRED
// 以及 tests/test_feedback_loop.py::test_client_mirror_matches_server_table 同一张表
// （规格 §5 末条：状态机定义在服务端一处，客户端镜像靠共享测试数据保持一致）。
const HANDLER_NEXT = {
    submitted: ['acknowledged'],
    acknowledged: ['processing', 'rejected'],
    processing: ['resolved', 'rejected'],
    resolved: [],
    rejected: ['processing'],
    user_confirmed: []
};
const REASON_REQUIRED = [['processing', 'resolved'], ['acknowledged', 'rejected'],
                         ['processing', 'rejected'], ['resolved', 'processing']];

test('nextOptions: 处理者可走的边与服务端白名单逐条一致', () => {
    for (const [from, expected] of Object.entries(HANDLER_NEXT)) {
        const got = nextOptions({status: from}).map(o => o.value);
        assert.deepEqual(got, expected, `from ${from}`);
    }
});

test('nextOptions: 必填说明的边带 reason 标记', () => {
    const opts = nextOptions({status: 'processing'});
    const resolved = opts.find(o => o.value === 'resolved');
    const rejected = opts.find(o => o.value === 'rejected');
    assert.equal(resolved.reason, true);
    assert.equal(rejected.reason, true);
    assert.equal(nextOptions({status: 'submitted'})[0].reason, false);
});

test('nextOptions: 终态没有可走的路', () => {
    assert.deepEqual(nextOptions({status: 'user_confirmed'}), []);
    assert.deepEqual(nextOptions({status: 'resolved'}), []);
});

test('statusText: 处理端口径（submitted 显示为「待受理」）', () => {
    assert.equal(statusText('submitted'), '待受理');
    assert.equal(statusText('user_confirmed'), '已确认');
    assert.equal(statusText('x'), 'x');
    assert.equal(statusText(''), '待受理');
});

test('submitterText: 智能体/归属标注，缺字段兜底', () => {
    assert.equal(submitterText({kind: 'agent', id: 'kimi-code', display_name: 'kimi-code'}), 'kimi-code（智能体）');
    assert.equal(submitterText({kind: 'agent', id: 'kimi', display_name: 'kimi', group: 'talent'}), 'kimi（智能体） · talent');
    assert.equal(submitterText({kind: 'human', id: 'owner', display_name: '主人'}), '主人');
    assert.equal(submitterText(null), '未知');
});

test('rowView: 行字段齐全，message 截断，overdue/unread 透传', () => {
    const row = rowView({
        feedback_id: 'fb1', status: 'submitted', route: '/tasks',
        submitted_at: '2026-11-15T10:00:00+08:00', overdue: true, unread: false,
        submitter: {kind: 'agent', id: 'kimi', display_name: 'kimi'}, message: 'x'.repeat(80)
    });
    assert.equal(row.id, 'fb1');
    assert.equal(row.statusText, '待受理');
    assert.equal(row.submitter, 'kimi（智能体）');
    assert.equal(row.overdue, true);
    assert.equal(row.message.length, 60);
    assert.equal(row.date, '2026-11-15 10:00');
});

test('traceRow: 四种轨迹成分拼一行，缺字段不炸', () => {
    assert.equal(traceRow({t: 1250, action: 'click', target: 'button#save'}),
                 '1.25s click button#save');
    assert.equal(traceRow({t: 3200, action: 'http', target: 'POST /api/x', status: 500, duration_ms: 3200}),
                 '3.20s http POST /api/x → 500 3.2s');
    assert.equal(traceRow({kind: 'route', target: '/tasks'}), 'route /tasks');
    assert.equal(traceRow(null), '');
});

test('historyRow: v2 形状（from/to/actor/reason）与 v1 形状（status/note）都认', () => {
    assert.equal(historyRow({from: 'submitted', to: 'acknowledged', actor: 'handler', at: '2026-11-15 10:00:00', reason: ''}),
                 '2026-11-15 10:00 · 处理者：submitted → acknowledged');
    assert.equal(historyRow({status: 'resolved', at: '2026-11-15 11:00:00', note: '已修复'}),
                 '2026-11-15 11:00 · 提交者：resolved（已修复）');
    assert.equal(historyRow({to: 'user_confirmed', actor: 'system', at: '2026-11-29 10:00:00', reason: '14 天未操作，系统自动确认'}),
                 '2026-11-29 10:00 · 系统：user_confirmed（14 天未操作，系统自动确认）');
});

test('listQuery: 只带非空筛选，分页参数总在', () => {
    assert.equal(listQuery({}, 1, 20), '/api/feedback?page=1&size=20');
    assert.equal(listQuery({status: 'submitted', route: '/tasks', overdue: true}, 2, 50),
                 '/api/feedback?status=submitted&route=%2Ftasks&overdue=1&page=2&size=50');
    assert.equal(listQuery({submitter: 'kimi', group: ''}, 1, 20),
                 '/api/feedback?submitter=kimi&page=1&size=20');
});

test('REASON_REQUIRED 共享表没被改坏（四必填边）', () => {
    assert.deepEqual(REASON_REQUIRED, [['processing', 'resolved'], ['acknowledged', 'rejected'],
                                       ['processing', 'rejected'], ['resolved', 'processing']]);
});
