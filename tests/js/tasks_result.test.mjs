// 结构化结果呈现（批次 G1）纯函数单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import result from '../../frontend/assets/js/tasks-result.js';

const {
    verdictOf, testsText, commitOf, gateItems, cardView, rowLabel,
} = result;

const implResult = {
    expects: 'impl', summary: '交付了 X', commit: 'abc1234def',
    branch: 'batch/result-tool',
    tests: {passed: 12, failed: 0, skipped: 1},
    gates: {structure: 'pass', frontend: 'fail'},
    risks: ['风险一'], questions: ['要不要发版？'],
    via: 'mcp', at: 1,
};
const verifyPass = {
    expects: 'verify', verdict: 'pass', target: '0badf00d',
    summary: '验过了', mustFix: [], evidence: ['跑了测试'],
};
const verifyRework = {
    expects: 'verify', verdict: 'rework', target: '0badf00d',
    summary: '要返工', mustFix: ['少一个字段'],
};
const verifyJudge = {
    expects: 'verify', verdict: 'needs_judgment', target: '0badf00d',
    summary: '拿不准',
};
const plainResult = {summary: '自由结果', data: {a: 1}};

test('verdictOf: impl=已交付，verify 三态着色', () => {
    assert.deepEqual(verdictOf(implResult), {text: '已交付', cls: 'is-pass'});
    assert.deepEqual(verdictOf(verifyPass), {text: '通过', cls: 'is-pass'});
    assert.deepEqual(verdictOf(verifyRework), {text: '返工', cls: 'is-rework'});
    assert.deepEqual(verdictOf(verifyJudge), {text: '需判断', cls: 'is-judge'});
    assert.deepEqual(verdictOf(plainResult), {text: '有结果', cls: 'is-pass'});
    assert.deepEqual(verdictOf(null), {text: '有结果', cls: 'is-pass'});
});

test('testsText: 三项拼一行；全 0/缺失为空', () => {
    assert.equal(testsText({passed: 12, failed: 0, skipped: 1}), '12过/0败/1跳过');
    assert.equal(testsText({passed: 0, failed: 0, skipped: 0}), '');
    assert.equal(testsText(null), '');
    assert.equal(testsText(undefined), '');
});

test('commitOf: impl 取 commit，verify 取 target', () => {
    assert.equal(commitOf(implResult), 'abc1234def');
    assert.equal(commitOf(verifyPass), '0badf00d');
    assert.equal(commitOf(plainResult), '');
    assert.equal(commitOf(null), '');
});

test('gateItems: 只列有值的门禁，中文名', () => {
    assert.deepEqual(gateItems(implResult.gates),
                     [{name: '结构', value: 'pass'}, {name: '前端', value: 'fail'}]);
    assert.deepEqual(gateItems({structure: 'fail'}), [{name: '结构', value: 'fail'}]);
    assert.deepEqual(gateItems(null), []);
    assert.deepEqual(gateItems({}), []);
});

test('cardView: 卡片一次拿全（verdict/commit/测试/清单）', () => {
    const v = cardView(implResult);
    assert.equal(v.verdict.text, '已交付');
    assert.equal(v.commit, 'abc1234def');
    assert.equal(v.branch, 'batch/result-tool');
    assert.equal(v.summary, '交付了 X');
    assert.equal(v.testsText, '12过/0败/1跳过');
    assert.equal(v.gates.length, 2);
    assert.equal(v.risks.length, 1);
    assert.equal(v.questions.length, 1);
    assert.equal(v.mustFix.length, 0);
    assert.equal(v.via, 'mcp');
    const r = cardView(verifyRework);
    assert.equal(r.verdict.text, '返工');
    assert.equal(r.mustFix.length, 1);
    assert.equal(r.testsText, '');
    assert.deepEqual(cardView(null).gates, []);
});

test('rowLabel: 指挥台小标签——verdict + 测试数或短 commit；无结果为空', () => {
    assert.equal(rowLabel(implResult), '已交付 · 12过/0败/1跳过');
    assert.equal(rowLabel(verifyPass), '通过 · 0badf00');
    assert.equal(rowLabel(verifyRework), '返工 · 0badf00');
    assert.equal(rowLabel(verifyJudge), '需判断 · 0badf00');
    assert.equal(rowLabel(plainResult), '有结果');
    assert.equal(rowLabel(null), '');
    assert.equal(rowLabel({}), '');
    assert.equal(rowLabel({summary: 'x', tests: {passed: 3, failed: 0, skipped: 0}}),
                 '有结果 · 3过/0败/0跳过');
});
