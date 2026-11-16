// 工作区面板纯函数单测（批次 G2）：阶段条、文案、行映射。
// 跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import WP from '../../frontend/assets/js/workitems.js';

const { stageBar, stateText, verdictText, rowOf, fmtTime } = WP;

test('stageBar marks steps done/active by stage', () => {
    assert.deepStrictEqual(stageBar('impl').map(s => s.state),
                           ['active', 'todo', 'todo']);
    assert.deepStrictEqual(stageBar('verify').map(s => s.state),
                           ['done', 'active', 'todo']);
    assert.deepStrictEqual(stageBar('integrate').map(s => s.state),
                           ['done', 'done', 'active']);
    assert.deepStrictEqual(stageBar('done').map(s => s.state),
                           ['done', 'done', 'done']);
    // 排队/待判断/失败：没有"进行到哪一段"，全 todo
    assert.deepStrictEqual(stageBar('queue').map(s => s.state),
                           ['todo', 'todo', 'todo']);
    assert.deepStrictEqual(stageBar('judge').map(s => s.state),
                           ['todo', 'todo', 'todo']);
});

test('stageBar keeps labels in order', () => {
    assert.deepStrictEqual(stageBar('verify').map(s => s.label),
                           ['实施', '验收', '集成']);
});

test('stateText and verdictText have Chinese fallbacks', () => {
    assert.strictEqual(stateText('needs_judgment'), '待判断');
    assert.strictEqual(stateText('integrated'), '已集成');
    assert.strictEqual(stateText('weird'), 'weird');
    assert.strictEqual(verdictText('rework'), '返工');
    assert.strictEqual(verdictText(''), '');
});

test('rowOf maps brief into display row', () => {
    const row = rowOf({
        id: 'wi-ab12cd', title: '工作项存储', state: 'verifying',
        stage: 'verify', attempt: 2, maxAttempts: 3,
        lastVerdict: 'rework', lastImplCommit: 'abcdef1234567890',
        lastImplSummary: '交付了存储', queuedFor: '', queuedShare: [],
        judgmentReason: '', integration: {}, updatedAt: 1794742746
    });
    assert.strictEqual(row.id, 'wi-ab12cd');
    assert.strictEqual(row.stateText, '验收中');
    assert.strictEqual(row.attemptText, 'attempt 2/3');
    assert.strictEqual(row.verdictText, '返工');
    assert.strictEqual(row.lastImplCommit, 'abcdef123456');
    assert.strictEqual(row.stageBar[1].state, 'active');
    assert.ok(row.updatedText.length >= 10);
});

test('rowOf builds queue hint when queued', () => {
    const row = rowOf({
        id: 'wi-x', title: '乙', state: 'queued', stage: 'queue',
        queuedFor: 'wi-aaaaaaaa', queuedShare: ['modules/*.py'],
        updatedAt: 0
    });
    assert.strictEqual(row.queueHint, '等待 wi-aaaaa（共享 modules/*.py）');
    assert.strictEqual(row.updatedText, '');
});

test('fmtTime formats epoch seconds', () => {
    assert.strictEqual(fmtTime(0), '');
    assert.match(fmtTime(1794742746), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
});
