// 设置页「主管」卡片 + 任务页按主管筛（批次 S）纯函数单测。
// 跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import leads from '../../frontend/assets/js/settings-leads.js';
import tasksLeads from '../../frontend/assets/js/tasks-leads.js';

const {statusText, kindText, lastSeenText, view, bindable} = leads;
const {leadName, filterItems, taskListUrl} = tasksLeads;

test('statusText: 四态映射，未知值原样露出', () => {
    assert.equal(statusText('active'), '在用');
    assert.equal(statusText('paused'), '已暂停');
    assert.equal(statusText('retired'), '已停用');
    assert.equal(statusText('weird'), 'weird');
    assert.equal(statusText(''), '未知状态');
});

test('kindText: 两类映射，未知原样', () => {
    assert.equal(kindText('external'), '外部接入');
    assert.equal(kindText('hosted'), '托管');
    assert.equal(kindText('x'), 'x');
    assert.equal(kindText(''), '未知类型');
});

test('lastSeenText: 规格例子表逐档（0/59s/61s/3601s/90000s）', () => {
    const now = 1000000;
    assert.equal(lastSeenText(0, now), '从未活跃');
    assert.equal(lastSeenText(undefined, now), '从未活跃');
    assert.equal(lastSeenText(now - 59, now), '刚刚');
    assert.equal(lastSeenText(now - 61, now), '1 分钟前');
    assert.equal(lastSeenText(now - 3601, now), '1 小时前');
    assert.equal(lastSeenText(now - 90000, now), '1 天前');
});

test('lastSeenText: 时钟跳到未来的记录不当负数显示', () => {
    assert.equal(lastSeenText(2000, 1000), '刚刚');
});

test('view: 行字段齐全，缺字段兜底不白屏', () => {
    const rows = view({leads: [
        {id: 'ld_1', name: 'talent', project: '拔尖', kind: 'external',
         status: 'active', tokens: ['kimi-code'], lastSeenAt: 100,
         tasks: {total: 3, running: 1, waiting: 2}},
        {},
    ]}, 1000);
    assert.equal(rows.length, 2);
    assert.equal(rows[0].name, 'talent');
    assert.equal(rows[0].statusText, '在用');
    assert.equal(rows[0].kind, '外部接入');
    assert.deepEqual(rows[0].tokens, ['kimi-code']);
    assert.equal(rows[0].total, 3);
    assert.equal(rows[0].running, 1);
    assert.equal(rows[0].waiting, 2);
    assert.equal(rows[0].lastSeenText, '15 分钟前');
    // 缺字段行：空值兜底，不抛
    assert.equal(rows[1].name, '未命名');
    assert.equal(rows[1].statusText, '未知状态');
    assert.equal(rows[1].total, 0);
});

test('view: 空数据/坏数据不抛', () => {
    assert.deepEqual(view(null, 1), []);
    assert.deepEqual(view({}, 1), []);
    assert.deepEqual(view({leads: 'x'}, 1), []);
});

test('bindable: 只挑没绑任何主管的令牌', () => {
    const tokens = [
        {id: 't1', name: 'kimi-code', lead: ''},
        {id: 't2', name: 'claude', lead: 'ld_1'},
        {id: 't3', name: 'external-2'},
    ];
    const got = bindable(tokens, 'ld_1');
    assert.deepEqual(got.map(t => t.id), ['t1', 't3']);
    assert.deepEqual(bindable(null, 'ld_1'), []);
});

test('leadName: 有名字用名字，找不到退回 id，没有主管返回空', () => {
    const list = [{id: 'ld_1', name: 'talent'}, {id: 'ld_2', name: ''}];
    assert.equal(leadName(list, 'ld_1'), 'talent');
    assert.equal(leadName(list, 'ld_9'), 'ld_9');
    assert.equal(leadName(list, ''), '');
    assert.equal(leadName(null, 'ld_1'), 'ld_1');
});

test('filterItems: 停用的主管不进筛选排', () => {
    const list = [
        {id: 'ld_1', name: 'talent', status: 'active'},
        {id: 'ld_2', name: 'old', status: 'retired'},
        {id: 'ld_3', name: 'paused-one', status: 'paused'},
    ];
    assert.deepEqual(filterItems(list).map(l => l.id), ['ld_1', 'ld_3']);
    assert.deepEqual(filterItems(null), []);
});

test('taskListUrl: 不带筛选逐字不变，带筛选才加参数并编码', () => {
    assert.equal(taskListUrl('/api/tasks?full=1', ''), '/api/tasks?full=1');
    assert.equal(taskListUrl('/api/tasks?full=1', null), '/api/tasks?full=1');
    assert.equal(taskListUrl('/api/tasks?full=1', 'ld_1'),
                 '/api/tasks?full=1&lead=ld_1');
    assert.equal(taskListUrl('/api/tasks?full=1', 'a b&c'),
                 '/api/tasks?full=1&lead=a%20b%26c');
});
