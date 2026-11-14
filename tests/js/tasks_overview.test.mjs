// 指挥台一屏（批次 U）纯函数单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import overview from '../../frontend/assets/js/tasks-overview.js';

const {
    taskStatusText, taskModeText, stateText, firstLine, countsText,
    unassignedTitle, taskRowView, pendingRows, mergeMoreTasks, newLeadKindChoices,
} = overview;

test('taskStatusText: 九态映射，未知/空原样兜底', () => {
    assert.equal(taskStatusText('queued'), '排队中');
    assert.equal(taskStatusText('running'), '运行中');
    assert.equal(taskStatusText('waiting'), '等你继续');
    assert.equal(taskStatusText('paused'), '已休眠');
    assert.equal(taskStatusText('closed'), '已结束');
    assert.equal(taskStatusText('done'), '完成');
    assert.equal(taskStatusText('failed'), '失败');
    assert.equal(taskStatusText('cancelled'), '已取消');
    assert.equal(taskStatusText('interrupted'), '已中断');
    assert.equal(taskStatusText('weird'), 'weird');
    assert.equal(taskStatusText(''), '-');
    assert.equal(taskStatusText(undefined), '-');
});

test('taskModeText: 四模式映射，缺省 auto', () => {
    assert.equal(taskModeText('auto'), '自动批准');
    assert.equal(taskModeText('default'), '逐项批准');
    assert.equal(taskModeText('plan'), '只读规划');
    assert.equal(taskModeText('yolo'), '全部放行');
    assert.equal(taskModeText('x'), 'x');
    assert.equal(taskModeText(''), 'auto');
});

test('stateText: 主管四态映射', () => {
    assert.equal(stateText('busy'), '在忙');
    assert.equal(stateText('asleep'), '睡着');
    assert.equal(stateText('waiting_you'), '等你');
    assert.equal(stateText('paused'), '已暂停');
    assert.equal(stateText('x'), 'x');
    assert.equal(stateText(''), '-');
});

test('firstLine: 只取首行；超限截断补省略号；空值安全', () => {
    assert.equal(firstLine('第一行\n第二行', 60), '第一行');
    assert.equal(firstLine('  前面有空格  \n 第二行', 60), '前面有空格');
    assert.equal(firstLine('一二三四五六', 3), '一二三…');
    assert.equal(firstLine('一二三', 3), '一二三');
    assert.equal(firstLine('', 10), '');
    assert.equal(firstLine(null, 10), '');
    assert.equal(firstLine(undefined, 10), '');
    assert.equal(firstLine('只有一行没有上限', 0), '只有一行没有上限');
});

test('countsText: 三计数拼成一行，缺省 0', () => {
    assert.equal(countsText({running: 2, waiting: 1, failed: 0}),
                 '在跑 2 · 等待 1 · 出错 0');
    assert.equal(countsText({}), '在跑 0 · 等待 0 · 出错 0');
    assert.equal(countsText(null), '在跑 0 · 等待 0 · 出错 0');
});

test('unassignedTitle: 「N 个，其中在跑 K」', () => {
    assert.equal(unassignedTitle(2, 0), '2 个，其中在跑 0');
    assert.equal(unassignedTitle(0, 0), '0 个，其中在跑 0');
    assert.equal(unassignedTitle(12, 3), '12 个，其中在跑 3');
    assert.equal(unassignedTitle(undefined, undefined), '0 个，其中在跑 0');
});

test('taskRowView: 行字段齐全，执行点/模型/最近活跃拼副行', () => {
    const row = taskRowView({
        id: 't1', title: '整理简历', status: 'running', node: 'ai-xmyz',
        model: '', modelEffective: 'kimi-k2', lastActiveAt: 123,
    }, '3 分钟前');
    assert.equal(row.id, 't1');
    assert.equal(row.title, '整理简历');
    assert.equal(row.dotClass, 'task-dot is-running');
    assert.equal(row.statusText, '运行中');
    assert.equal(row.model, 'kimi-k2');
    assert.equal(row.sub, 'ai-xmyz · kimi-k2 · 最近 3 分钟前');
});

test('taskRowView: 缺字段兜底不抛，老任务无 lastActiveAt', () => {
    const row = taskRowView({id: 't2', status: 'done'}, '');
    assert.equal(row.title, '(无标题)');
    assert.equal(row.node, 'local');
    assert.equal(row.sub, 'local');
    assert.equal(row.dotClass, 'task-dot is-done');
    assert.equal(row.lastActiveAt, 0);
});

test('pendingRows: 三类映射、主管名回填、时间倒序、key 稳定', () => {
    const rows = pendingRows([
        {kind: 'report', at: 100, leadId: 'ld_1', title: '乙',
         detail: '跑完了', seq: 2},
        {kind: 'permission', at: 300, taskId: 't1', leadId: 'ld_2',
         title: 'Bash', detail: 'ls', requestId: 'rq-1',
         options: [{optionId: 'o1', name: '允许', kind: 'allow_once'}]},
        {kind: 'problem', at: 200, taskId: 't2', leadId: '',
         title: '坏任务', detail: '崩了'},
    ], {ld_1: {id: 'ld_1', name: '乙'}, ld_2: {id: 'ld_2', name: ''}});
    assert.deepEqual(rows.map(r => r.kind), ['permission', 'problem', 'report']);
    assert.deepEqual(rows.map(r => r.kindText), ['审批', '问题', '汇报']);
    // 名字：找得到用名字，空名兜底「未命名」，无主任务为空
    assert.equal(rows[0].leadName, '未命名');
    assert.equal(rows[2].leadName, '乙');
    // key 带类型与主键，同任务多条待办不会撞
    assert.equal(rows[0].key, 'permission:t1:rq-1');
    assert.equal(rows[2].key, 'report:ld_1:2');
    assert.equal(rows[2].seq, 2);
    assert.deepEqual(rows[0].options.map(o => o.optionId), ['o1']);
});

test('pendingRows: 空/坏数据不抛', () => {
    assert.deepEqual(pendingRows(null, null), []);
    assert.deepEqual(pendingRows([], {}), []);
    assert.deepEqual(pendingRows([{}], {}).map(r => r.kindText), ['待办']);
});

test('mergeMoreTasks: 按 id 去重合并，保留原顺序', () => {
    const current = [{id: 'a', title: 'A'}, {id: 'b', title: 'B'}];
    const more = [{id: 'b', title: 'B2'}, {id: 'c', title: 'C'}];
    const merged = mergeMoreTasks(current, more);
    assert.deepEqual(merged.map(t => t.id), ['a', 'b', 'c']);
    assert.equal(merged[1].title, 'B');       // 不覆盖已有的
    assert.deepEqual(mergeMoreTasks(null, null), []);
    assert.deepEqual(mergeMoreTasks([], 'x'), []);
});

test('newLeadKindChoices: 外部接入可建，托管置灰即将支持', () => {
    const choices = newLeadKindChoices();
    assert.equal(choices.length, 2);
    const external = choices.find(c => c.kind === 'external');
    const hosted = choices.find(c => c.kind === 'hosted');
    assert.equal(external.enabled, true);
    assert.equal(hosted.enabled, false);
    assert.ok(hosted.hint.includes('即将支持'));
});
