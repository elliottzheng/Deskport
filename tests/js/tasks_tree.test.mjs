// 指挥树（批次 U2）纯函数单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import overview from '../../frontend/assets/js/tasks-overview.js';
import tree from '../../frontend/assets/js/tasks-tree.js';

// nodeView 复用 WPTasksOverview 的文案函数（浏览器里它是 window 全局）；这里挂上
globalThis.WPTasksOverview = overview;
// 相对时间/绝对时间走 WPCommon（浏览器里 common.js 先加载）；测试用最小替身，
// 只为验「调用了共享实现、格式串正确」
globalThis.WPCommon = {
    fmtAgo: (ts) => (ts ? '1 分钟前' : ''),
    fmtTime: (ts) => (ts ? '11-15 10:00' : '-'),
};

const {
    readOpen, writeOpen, autoOpenKeys, resolveOpen, flattenTree, treeIndex,
    taskNode, withMoreTasks, nodeView,
} = tree;

function node(type, extra) {
    var e = extra || {};
    var out = Object.assign({type: type, id: '', name: '',
                             counts: {running: 0, waiting: 0, waiting_you: 0, failed: 0},
                             children: []}, e);
    if (!e.key) { out.key = type + ':' + (e.id || ''); }
    return out;
}

function task(id, extra) {
    return Object.assign({type: 'task', key: 'task:' + id, id: id, title: id,
                          status: 'waiting', node: 'local', lastActiveAt: 0}, extra || {});
}

const SAMPLE = [
    node('lead', {id: 'ld_a', name: '甲', kind: 'hosted', state: 'busy',
                  taskId: 'h0', counts: {running: 1, waiting: 1, waiting_you: 1, failed: 0},
                  children: [task('a1'), task('a2', {status: 'running'})]}),
    node('owner', {id: 'kimi-code', name: 'kimi-code',
                   counts: {running: 0, waiting: 1, waiting_you: 1, failed: 0},
                   children: [task('k1')]}),
    node('direct', {key: 'direct', name: '你直接派的', children: [task('mine')]}),
];

// ---------------------------------------------------------------- 摊平与层级

test('flattenTree: 深度优先摊平，层级/父 key/可展开都对', () => {
    const rows = flattenTree(SAMPLE, {});
    assert.deepEqual(rows.map(r => [r.key, r.depth]),
                     [['lead:ld_a', 0], ['owner:kimi-code', 0], ['direct', 0]]);
    assert.deepEqual(rows.map(r => r.parentKey), ['', '', '']);
    assert.deepEqual(rows.map(r => r.expandable), [true, true, true]);
    assert.equal(rows.every(r => r.expanded === false), true);
    // 展开后才看得到子节点（层级从 1 起）
    const open = flattenTree(SAMPLE, {'lead:ld_a': true, 'direct': true});
    assert.deepEqual(open.map(r => [r.key, r.depth]),
                     [['lead:ld_a', 0], ['task:a1', 1], ['task:a2', 1],
                      ['owner:kimi-code', 0], ['direct', 0], ['task:mine', 1]]);
    assert.deepEqual(open.map(r => r.parentKey),
                     ['', 'lead:ld_a', 'lead:ld_a', '', '', 'direct']);
    assert.deepEqual(open.filter(r => r.expandable === false).map(r => r.key),
                     ['task:a1', 'task:a2', 'task:mine']);
});

test('flattenTree: 只展开给了 key 的节点，坏数据不抛', () => {
    const rows = flattenTree(SAMPLE, {'lead:ld_a': true, 'owner:kimi-code': true});
    assert.deepEqual(rows.map(r => r.key),
                     ['lead:ld_a', 'task:a1', 'task:a2', 'owner:kimi-code', 'task:k1',
                      'direct']);
    assert.deepEqual(flattenTree(null, null), []);
    assert.deepEqual(flattenTree([], {}), []);
    // 子节点缺 key/children 也不炸
    assert.deepEqual(flattenTree([{type: 'task', id: 'x'}], {'': true}).map(r => r.key),
                     ['']);
});

// ---------------------------------------------------------------- 默认展开

test('autoOpenKeys: 有等你或在跑后代的节点默认展开，其余折叠', () => {
    const keys = autoOpenKeys(SAMPLE);
    assert.deepEqual(Object.keys(keys).sort(), ['lead:ld_a', 'owner:kimi-code']);
    assert.equal(keys['lead:ld_a'], true);
    // 全睡着的一棵树：一个都不展开（「你直接派的」计数为 0）
    assert.equal(keys['direct'], undefined);
    const sleepy = [node('lead', {id: 'ld_x', name: '睡', children: [task('t1')]})];
    assert.deepEqual(autoOpenKeys(sleepy), {});
    assert.deepEqual(autoOpenKeys(null), {});
});

test('resolveOpen: 用户显式选择覆盖默认（存过就用存过的）', () => {
    const open = resolveOpen(SAMPLE, {'lead:ld_a': false, 'direct': true});
    assert.equal(open['lead:ld_a'], false);      // 用户收起，盖掉默认展开
    assert.equal(open['direct'], true);          // 用户展开，盖掉默认折叠
    assert.equal(open['owner:kimi-code'], true); // 没存过 → 按默认
    assert.deepEqual(resolveOpen(null, null), {});
});

// ---------------------------------------------------------------- 索引与路径

test('treeIndex: taskId → 路径与祖先 key，例 3 的「A › a2」', () => {
    const index = treeIndex(SAMPLE);
    assert.deepEqual(index['a2'].path, ['甲', 'a2']);
    assert.deepEqual(index['a2'].keys, ['lead:ld_a', 'task:a2']);
    assert.deepEqual(index['mine'].path, ['你直接派的', 'mine']);
    assert.deepEqual(index['mine'].keys, ['direct', 'task:mine']);
    assert.equal(index['nope'], undefined);
    assert.deepEqual(treeIndex(null), {});
    // 主管节点本身不是任务，不进索引
    assert.equal(index['ld_a'], undefined);
});

// ---------------------------------------------------------------- 更多

test('taskNode: brief → 任务节点（与后端 _taskNode 同形）', () => {
    const n = taskNode({id: 't9', title: '整理简历', status: 'running'});
    assert.equal(n.type, 'task');
    assert.equal(n.key, 'task:t9');
    assert.equal(n.title, '整理简历');
    assert.deepEqual(taskNode(null), {type: 'task', key: 'task:'});
});

test('withMoreTasks: 「更多」的 brief 并进对应节点，按 id 去重', () => {
    const out = withMoreTasks(SAMPLE, {'lead:ld_a': [task('a1'), task('a3')]});
    const kids = out[0].children;
    assert.deepEqual(kids.map(k => k.id), ['a1', 'a2', 'a3']);   // a1 不重复
    assert.equal(kids.every(k => k.type === 'task' && k.key), true);
    // 不影响别的节点
    assert.deepEqual(out[1].children.map(k => k.id), ['k1']);
    // 任务子项仍排在子主管前面
    const mixed = [node('lead', {id: 'ld_m', name: '甲',
                                 children: [task('m1'),
                                            node('lead', {id: 'ld_sub', name: '子'})]})];
    const merged = withMoreTasks(mixed, {'lead:ld_m': [task('m2')]});
    assert.deepEqual(merged[0].children.map(k => [k.type, k.id]),
                     [['task', 'm1'], ['task', 'm2'], ['lead', 'ld_sub']]);
    // 没拉过 / 空列表：原样拷一份，不改入参
    const untouched = withMoreTasks(SAMPLE, {});
    assert.deepEqual(untouched.map(n => n.key), SAMPLE.map(n => n.key));
    assert.deepEqual(withMoreTasks(null, null), []);
});

// ---------------------------------------------------------------- 行视图

test('nodeView: 主管节点带类型/状态徽标与计数，任务节点只带状态点与标题', () => {
    const lead = nodeView(SAMPLE[0], 0, '', {});
    assert.equal(lead.type, 'lead');
    assert.equal(lead.name, '甲');
    assert.equal(lead.countsText, '在跑 1 · 等你 1 · 出错 0');
    assert.equal(lead.stateText, '在忙');
    assert.equal(lead.taskId, 'h0');
    const tk = nodeView(SAMPLE[0].children[1], 1, '3 分钟前', {});
    assert.equal(tk.type, 'task');
    assert.equal(tk.dotClass, 'task-dot is-running');
    assert.equal(tk.statusText, '运行中');
    assert.equal(tk.sub.includes('3 分钟前'), true);
    assert.equal(tk.countsText, '在跑 0 · 等你 0 · 出错 0');
});

test('nodeView: 未登记指挥者给登记提示，已停用主管给停用提示', () => {
    const owner = nodeView(SAMPLE[1], 0, '', {});
    assert.equal(owner.badge, '未登记');
    assert.equal(owner.hint.includes('deskport.py lead --register'), true);
    const orphan = nodeView(node('orphan', {id: 'ld_old', name: '老甲'}), 0, '', {});
    assert.equal(orphan.badge, '已停用');
    assert.equal(orphan.hint.includes('已停用或不存在'), true);
    const direct = nodeView(SAMPLE[2], 0, '', {});
    assert.equal(direct.badge, '');
    assert.equal(direct.hint, '');
});

test('nodeView: 有待办的任务行补「等你」标（例 3），closedHidden 决定更多/提示', () => {
    const waiting = nodeView(task('a2'), 1, '', {a2: true});
    assert.equal(waiting.waitingYou, true);
    assert.equal(nodeView(task('a2'), 1, '', {}).waitingYou, false);
    // 主管节点能按 lead 拉全量 → 给「更多」按钮；其它组只给提示
    const lead = nodeView(node('lead', {closedHidden: 3}), 0, '', {});
    assert.equal(lead.canMore, true);
    assert.equal(lead.moreHint, false);
    const group = nodeView(node('direct', {closedHidden: 3}), 0, '', {});
    assert.equal(group.canMore, false);
    assert.equal(group.moreHint, true);
    // 没有裁剪掉的：两个都不出
    assert.equal(nodeView(node('lead', {closedHidden: 0}), 0, '', {}).canMore, false);
});

test('nodeView: 缺字段不抛（老任务无 lastActiveAt/无 counts）', () => {
    const v = nodeView({type: 'task', id: 'x'}, 2, '', null);
    assert.equal(v.name, '');
    assert.equal(v.depth, 2);
    assert.equal(v.countsText, '在跑 0 · 等你 0 · 出错 0');
    assert.deepEqual(nodeView(null, 0, '', null).type, '');
});

// ---------------------------------------------------------------- 直接活动（U2 追加）

const ACTIVITY = {
    exec: 3, sftp: 2, writes: 1, task: 0, session: 0,
    lastAt: 1794709596, lastExec: 'ls -la /tmp',
    recent: [
        {at: 1794709596, action: 'agent.exec', summary: 'ls -la /tmp'},
        {at: 1794709566, action: 'agent.sftp.put', summary: '/tmp/a.txt'},
        {at: 1794709536, action: 'agent.task.create', summary: '整理简历'},
    ],
};

test('nodeView: 指挥者节点带直接活动一行（执行 N 次、写文件 M 次 · X 前）', () => {
    const lead = nodeView(node('lead', {id: 'ld_a', name: '甲', activity: ACTIVITY}), 0, '', {});
    assert.equal(lead.activityText, '直接操作：执行 3 次、写文件 1 次 · 1 分钟前');
    assert.equal(lead.activity, ACTIVITY);
    // 明细：时间/动作/摘要各就位
    assert.deepEqual(lead.activityRecent.map(r => r.action),
                     ['agent.exec', 'agent.sftp.put', 'agent.task.create']);
    assert.deepEqual(lead.activityRecent.map(r => r.summary),
                     ['ls -la /tmp', '/tmp/a.txt', '整理简历']);
    assert.equal(lead.activityRecent[0].time, '11-15 10:00');
});

test('nodeView: 未登记令牌也有直接活动；没有活动时不加那行', () => {
    const owner = nodeView(node('owner', {id: 'kimi-code', name: 'kimi-code',
                                         activity: ACTIVITY}), 0, '', {});
    assert.equal(owner.activityText.includes('执行 3 次'), true);
    const quiet = nodeView(node('lead', {id: 'ld_b', name: '睡'}), 0, '', {});
    assert.equal(quiet.activityText, '');
    assert.equal(quiet.activity, null);
    assert.deepEqual(quiet.activityRecent, []);
    // 任务节点不显示直接活动
    const tk = nodeView(task('t1', {activity: ACTIVITY}), 1, '', {});
    assert.equal(tk.activityText, '');
});

test('nodeView: 没有 lastAt 时只报次数不报时间', () => {
    const slot = Object.assign({}, ACTIVITY, {lastAt: 0, recent: []});
    const v = nodeView(node('owner', {id: 'x', name: 'x', activity: slot}), 0, '', {});
    assert.equal(v.activityText, '直接操作：执行 3 次、写文件 1 次');
    assert.deepEqual(v.activityRecent, []);
});
