// 任务详情事件流（批次 U3）纯函数单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import events from '../../frontend/assets/js/tasks-events.js';

const {isPartial, parseArgs, toolView, resultTitle, mergeStatus, pendingView} = events;

// ---------------------------------------------------------------- 参数解析与残片

test('isPartial: 认得出流式残片，完整 JSON 与 dict 不算', () => {
    assert.equal(isPartial('{'), true);
    assert.equal(isPartial('['), true);
    assert.equal(isPartial('{"command": "ls'), true);
    assert.equal(isPartial('{"command": "ls"}'), false);
    assert.equal(isPartial('[]'), false);
    assert.equal(isPartial(''), false);
    assert.equal(isPartial(undefined), false);
    assert.equal(isPartial(null), false);
    assert.equal(isPartial({command: 'ls'}), false);
    assert.equal(isPartial('ls -la'), false);       // 纯字符串不是残片
});

test('parseArgs: dict / JSON 串 / 残片 / 坏数据', () => {
    assert.deepEqual(parseArgs({command: 'ls'}), {command: 'ls'});
    assert.deepEqual(parseArgs('{"command": "ls"}'), {command: 'ls'});
    assert.equal(parseArgs('{'), null);              // 残片 → null（显示参数加载中）
    assert.equal(parseArgs('{"a": '), null);
    assert.equal(parseArgs('not json'), null);
    assert.equal(parseArgs(''), null);
    assert.equal(parseArgs(null), null);
    assert.equal(parseArgs('"just a string"'), null);   // 标量不算参数对象
});

// ---------------------------------------------------------------- 工具调用说人话

test('toolView: Bash 显示命令本身，多行默认前 3 行 + 展开全部', () => {
    const one = toolView('Bash', '{"command": "ls -la /tmp"}');
    assert.equal(one.kind, 'command');
    assert.equal(one.headline, 'ls -la /tmp');
    assert.deepEqual(one.preview, ['ls -la /tmp']);
    assert.equal(one.totalLines, 1);
    assert.equal(one.loading, false);
    const many = toolView('Bash', JSON.stringify({
        command: 'cd /tmp\nline2\nline3\nline4\nline5'}));
    assert.equal(many.totalLines, 5);
    assert.deepEqual(many.preview, ['cd /tmp', 'line2', 'line3']);
    assert.equal(many.headline, 'cd /tmp');
});

test('toolView: Read/Edit/Write 显示文件路径，Edit 带改动片段', () => {
    const read = toolView('Read', '{"path": "E:/a/b.md", "line_offset": 18}');
    assert.equal(read.kind, 'file');
    assert.equal(read.headline, 'E:/a/b.md');
    assert.equal(read.diff, null);
    const edit = toolView('Edit', JSON.stringify({
        path: 'E:/a/b.md', old_string: '旧的一行\n旧的二行',
        new_string: '新的一行\n新的二行'}));
    assert.equal(edit.headline, 'E:/a/b.md');
    assert.deepEqual(edit.diff.oldLines, ['旧的一行', '旧的二行']);
    assert.deepEqual(edit.diff.newLines, ['新的一行', '新的二行']);
    assert.equal(edit.diff.oldTotal, 2);
    const write = toolView('Write', JSON.stringify({
        path: 'E:/a/c.md', content: '1\n2\n3\n4'}));
    assert.equal(write.headline, 'E:/a/c.md');
    assert.equal(write.totalLines, 4);
});

test('toolView: 其它工具显示 工具名 · 关键参数；description 当用途', () => {
    const g = toolView('Grep', JSON.stringify({
        pattern: 'lead', path: 'modules', output_mode: 'content',
        description: '找主管相关代码'}));
    assert.equal(g.kind, 'params');
    assert.equal(g.headline.indexOf('Grep · pattern=lead'), 0);
    assert.equal(g.headline.indexOf('path=modules') > 0, true);
    assert.equal(g.purpose, '找主管相关代码');
    // 参数是数组/对象时也不抛，摘要仍是一行
    const t = toolView('TodoList', JSON.stringify({todos: [{status: 'pending'}]}));
    assert.equal(t.headline.indexOf('TodoList · todos='), 0);
});

test('toolView: 残片显示「参数加载中」，到了自动替换', () => {
    const loading = toolView('Bash', '{');
    assert.equal(loading.kind, 'loading');
    assert.equal(loading.headline, '参数加载中…');
    assert.equal(loading.loading, true);
    const done = toolView('Bash', '{"command": "ls"}');
    assert.equal(done.loading, false);
    assert.equal(done.headline, 'ls');
});

test('toolView: 空/坏参数不抛，退回工具名', () => {
    assert.equal(toolView('Bash', '').headline, 'Bash');
    assert.equal(toolView('Bash', null).headline, 'Bash');
    assert.equal(toolView('', '{}').headline, '工具');
});

// ---------------------------------------------------------------- 结果标题

test('resultTitle: 说几行，不说「工具结果」', () => {
    assert.equal(resultTitle('a\nb\nc', false), '结果 · 3 行');
    assert.equal(resultTitle('a', false), '结果 · 1 行');
    assert.equal(resultTitle('', false), '结果 · 0 行');
    assert.equal(resultTitle('a\nb', true), '结果 · 2 行（已截断）');
});

// ---------------------------------------------------------------- status 合并

function ev(type, seq, text) {
    return {type: type, seq: seq, at: seq * 10, text: text};
}

test('mergeStatus: 连续的 status 合成一行，非 status 打断', () => {
    const rows = mergeStatus([
        ev('status', 1, '任务已创建'),
        ev('status', 2, '开始执行'),
        ev('tool', 3, ''),
        ev('status', 4, '这轮结束'),
        ev('status', 5, '又一条'),
    ]);
    assert.deepEqual(rows.map(r => r.type),
                     ['status-run', 'tool', 'status-run']);
    assert.equal(rows[0].text, '任务已创建 · 开始执行');
    assert.equal(rows[0].items.length, 2);
    assert.equal(rows[0].key, 'status-1');
    assert.equal(rows[2].text, '这轮结束 · 又一条');
    assert.equal(rows[2].key, 'status-4');
});

test('mergeStatus: 空/坏数据不抛，单条 status 也成一行', () => {
    assert.deepEqual(mergeStatus(null), []);
    assert.deepEqual(mergeStatus([]), []);
    const rows = mergeStatus([ev('status', 7, '只有一条')]);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].text, '只有一条');
    assert.equal(rows[0].count, undefined);      // count 由 mixin 补
});

// ---------------------------------------------------------------- 待办行人话

test('pendingView: 审批行带任务名、命令、用途、派活者', () => {
    const index = {'t1': {path: ['甲', '整理简历'], keys: ['lead:ld_a', 'task:t1']}};
    const row = {
        key: 'permission:t1:rq-1', kind: 'permission', kindText: '审批',
        title: 'Bash', detail: 'ls -la /tmp', purpose: '看看目录',
        taskId: 't1', leadId: 'ld_a', owner: 'kimi-code', at: 100,
    };
    const v = pendingView(row, index, {ld_a: {id: 'ld_a', name: '甲'}}, {});
    assert.equal(v.taskTitle, '整理简历');
    assert.equal(v.leadName, '甲');
    assert.equal(v.detail, 'ls -la /tmp');
    assert.equal(v.purpose, '看看目录');
    assert.equal(v.path, '甲 › 整理简历');
    assert.equal(v.unregistered, false);
});

test('pendingView: 派活者未登记成主管时标出来并附一句', () => {
    const v = pendingView({key: 'k', kind: 'permission', detail: 'ls',
                           taskId: 't9', leadId: '', owner: 'kimi-code'},
                          {}, {}, {});
    assert.equal(v.leadName, '未登记的指挥者：kimi-code');
    assert.equal(v.unregistered, true);
    assert.equal(v.registerHint.indexOf('登记成主管') > 0, true);
    assert.equal(v.taskTitle, '');            // 树上没有这个任务（被裁掉）
});

test('pendingView: 停用主管的名字从 retiredById 带回；空数据不抛', () => {
    const v = pendingView({key: 'k', kind: 'problem', detail: '崩了',
                           leadId: 'ld_old'}, {}, {}, {ld_old: '老甲'});
    assert.equal(v.leadName, '老甲');
    assert.equal(v.unregistered, false);
    assert.deepEqual(pendingView(null, null, null, null).kind, '');
});

test('pendingView: 汇报行不重复挂主管名（标题已经是主管名）', () => {
    const row = {key: 'report:ld_a:1', kind: 'report', kindText: '汇报',
                 title: 'Claude', detail: '跑完了', leadId: 'ld_a', at: 5};
    const v = pendingView(row, {}, {ld_a: {id: 'ld_a', name: 'Claude'}}, {});
    assert.equal(v.title, 'Claude');
    assert.equal(v.leadName, 'Claude');
    assert.equal(v.showLead, false);          // 不再显示成「Claude · Claude」
    // 任务行照挂主管名
    const index = {t1: {path: ['Claude', '批次 L 返工'], keys: []}};
    const w = pendingView({key: 'k', kind: 'problem', title: '坏任务',
                           taskId: 't1', leadId: 'ld_a'}, index,
                          {ld_a: {id: 'ld_a', name: 'Claude'}}, {});
    assert.equal(w.taskTitle, '批次 L 返工');
    assert.equal(w.showLead, true);
});
