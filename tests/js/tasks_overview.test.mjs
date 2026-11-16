// 指挥台一屏（批次 U）纯函数单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import overview from '../../frontend/assets/js/tasks-overview.js';

const {
    taskStatusText, taskModeText, stateText, firstLine, countsText,
    taskRowView, pendingRows, mergeMoreTasks, newLeadKindChoices,
    splitPermissionOptions, menuFlipUp,
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

test('countsText: 三计数拼成一行；树用 waiting_you、旧字段用 waiting', () => {
    assert.equal(countsText({running: 2, waiting: 1, failed: 0}),
                 '在跑 2 · 等你 1 · 出错 0');
    assert.equal(countsText({running: 2, waiting_you: 3, failed: 1}),
                 '在跑 2 · 等你 3 · 出错 1');
    assert.equal(countsText({}), '在跑 0 · 等你 0 · 出错 0');
    assert.equal(countsText(null), '在跑 0 · 等你 0 · 出错 0');
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
        {kind: 'notice', at: 150, leadId: 'ld_1', title: '乙',
         detail: '主管反应超时提醒', seq: 3},
        {kind: 'permission', at: 300, taskId: 't1', leadId: 'ld_2',
         title: 'Bash', detail: 'ls', requestId: 'rq-1',
         options: [{optionId: 'o1', name: '允许', kind: 'allow_once'}]},
        {kind: 'problem', at: 200, taskId: 't2', leadId: '',
         title: '坏任务', detail: '崩了'},
    ], {ld_1: {id: 'ld_1', name: '乙'}, ld_2: {id: 'ld_2', name: ''}});
    assert.deepEqual(rows.map(r => r.kind), ['permission', 'problem', 'notice', 'report']);
    // down 侧的 notice（主管超时提醒/主管自己待批）不能显示成「汇报」（L 返工）
    assert.deepEqual(rows.map(r => r.kindText), ['审批', '问题', '提醒', '汇报']);
    // 名字：找得到用名字，空名兜底「未命名」，无主任务为空
    assert.equal(rows[0].leadName, '未命名');
    assert.equal(rows[2].leadName, '乙');
    // key 带类型与主键，同任务多条待办不会撞
    assert.equal(rows[0].key, 'permission:t1:rq-1');
    assert.equal(rows[2].key, 'notice:ld_1:3');
    assert.equal(rows[3].key, 'report:ld_1:2');
    assert.equal(rows[3].seq, 2);
    assert.deepEqual(rows[0].options.map(o => o.optionId), ['o1']);
});

test('pendingRows: 空/坏数据不抛', () => {
    assert.deepEqual(pendingRows(null, null), []);
    assert.deepEqual(pendingRows([], {}), []);
    assert.deepEqual(pendingRows([{}], {}).map(r => r.kindText), ['待办']);
});

test('pendingRows: 停用主管的名字从 retiredById 带回（U 返工）', () => {
    const rows = pendingRows([
        {kind: 'problem', at: 100, taskId: 't1', leadId: 'ld_old',
         title: '旧任务', detail: '崩了'},
        {kind: 'problem', at: 90, taskId: 't2', leadId: '',
         title: '无主任务', detail: '崩了'},
    ], {}, {ld_old: '老甲'});
    assert.equal(rows[0].leadName, '老甲');   // 停用主管不上卡片，名字照常带出
    assert.equal(rows[1].leadName, '');        // 无主任务仍为空
    assert.deepEqual(pendingRows([{kind: 'problem', leadId: 'ld_x'}], null, null)
        .map(r => r.leadName), ['']);          // 两边都缺不抛
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

test('pendingRows: 审批行带上 purpose 与 owner（U3 人话字段）', () => {
    const rows = pendingRows([
        {kind: 'permission', at: 100, taskId: 't1', leadId: 'ld_1',
         title: 'Bash', detail: 'ls -la /tmp', purpose: '看看目录',
         owner: 'kimi-code', requestId: 'rq-1', options: []},
    ], {ld_1: {id: 'ld_1', name: '甲'}}, {});
    assert.equal(rows[0].purpose, '看看目录');
    assert.equal(rows[0].owner, 'kimi-code');
    // 老行（没有这两个键）不抛，退回空串
    const old = pendingRows([{kind: 'report', leadId: 'ld_1'}], {ld_1: {name: '甲'}}, {});
    assert.equal(old[0].purpose, '');
    assert.equal(old[0].owner, '');
});

test('splitPermissionOptions: allow_once 进主操作，其余进「更多」（M3 #4）', () => {
    const opts = [
        {optionId: 'o1', name: '允许一次', kind: 'allow_once'},
        {optionId: 'o2', name: '总是允许', kind: 'allow_always'},
    ];
    const split = splitPermissionOptions(opts);
    assert.deepEqual(split.primary.map(o => o.optionId), ['o1']);
    assert.deepEqual(split.more.map(o => o.optionId), ['o2']);
});

test('splitPermissionOptions: 没有 allow_once 的老链路把第一个可允许选项提为主操作', () => {
    const split = splitPermissionOptions([
        {optionId: 'o1', name: '总是允许', kind: 'allow_always'},
    ]);
    assert.deepEqual(split.primary.map(o => o.optionId), ['o1']);
    assert.deepEqual(split.more, []);
    // 一个选项都没有（事件被挤掉）：两级都空，模板只留「更多」里的拒绝/查看
    const empty = splitPermissionOptions([]);
    assert.deepEqual(empty.primary, []);
    assert.deepEqual(empty.more, []);
    assert.deepEqual(splitPermissionOptions(null), {primary: [], more: []});
});

test('pendingRows: 审批行带 primaryOptions/moreOptions（M3 #4）', () => {
    const rows = pendingRows([
        {kind: 'permission', at: 100, taskId: 't1', requestId: 'rq-1',
         options: [{optionId: 'o1', name: '允许一次', kind: 'allow_once'},
                   {optionId: 'o2', name: '总是允许', kind: 'allow_always'}]},
        {kind: 'report', at: 90, leadId: 'ld_1', seq: 1},
    ], {}, {});
    assert.deepEqual(rows[0].primaryOptions.map(o => o.optionId), ['o1']);
    assert.deepEqual(rows[0].moreOptions.map(o => o.optionId), ['o2']);
    // 非审批行也有这两个键（空数组），模板按 kind 分支不读它们
    assert.deepEqual(rows[1].primaryOptions, []);
    assert.deepEqual(rows[1].moreOptions, []);
});

test('menuFlipUp: 向下放越界（底栏顶/下一行 ··· 顶）才翻，向上不出顶栏（M3 返工 P1/P2）', () => {
    // 390×640 实测几何：四项菜单向下放到 606；下一行 ··· 顶 540、底栏顶 580
    // ——越过更紧的 540，翻转后顶 2*410-606=214 不出顶栏（56）→ 翻
    assert.equal(menuFlipUp(410, 606, 540, 56), true);
    // P1 末行：只有底栏一条线（606 > 580）→ 翻
    assert.equal(menuFlipUp(500, 606, 580, 56), true);
    // 向下放不越界（520 ≤ 540 / 570 ≤ 580）→ 不翻
    assert.equal(menuFlipUp(410, 520, 540, 56), false);
    assert.equal(menuFlipUp(500, 570, 580, 56), false);
    // 向上翻会钻到顶栏上（翻转后顶 4 < 56）→ 不翻，保持向下（极限几何退让）
    assert.equal(menuFlipUp(200, 396, 300, 56), false);
    // 恰好贴线不翻（bottom == limit）
    assert.equal(menuFlipUp(410, 540, 540, 56), false);
    assert.equal(menuFlipUp(410, 606, 214, 56), true);
});
