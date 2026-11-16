// 命令执行页「最近执行」（批次 M3 #6）纯函数单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import command from '../../frontend/assets/js/command.js';

const { RECENT_KEY, RECENT_MAX, recentList } = command;

test('recentList: 新命令置顶、去重、封顶 5 条', () => {
    let list = [];
    for (const cmd of ['ipconfig', 'dir', 'ipconfig', 'whoami', 'ping 127.0.0.1', 'git status', 'ls']) {
        list = recentList(list, cmd);
    }
    assert.equal(list.length, RECENT_MAX);
    assert.equal(RECENT_MAX, 5);
    // 最新的在前；重复执行的 ipconfig 只留一份并提到最前；第 6 条把最老的 dir 挤出
    assert.deepEqual(list, ['ls', 'git status', 'ping 127.0.0.1', 'whoami', 'ipconfig']);
});

test('recentList: 空命令/坏数据原样带回，不写坏状态', () => {
    assert.deepEqual(recentList(['a', 'b'], ''), ['a', 'b']);
    assert.deepEqual(recentList(['a', 'b'], '   '), ['a', 'b']);
    assert.deepEqual(recentList(null, 'x'), ['x']);
    assert.deepEqual(recentList('x', 'y'), ['y']);
    // 非字符串条目被滤掉
    assert.deepEqual(recentList(['a', 1, null, 'b'], ''), ['a', 'b']);
    // 超过上限的存量也只留最近 5 条
    assert.deepEqual(recentList(['1', '2', '3', '4', '5', '6', '7'], ''),
                     ['1', '2', '3', '4', '5']);
});

test('recentList: 去重置顶不制造重复条目（未触顶时的直接断言，S3 补测）', () => {
    // 封顶会掩没重复（验收实验 4），下面用未超上限的短列表直接锁死去重
    assert.deepEqual(recentList(['a', 'b'], 'a'), ['a', 'b']);
    assert.deepEqual(recentList(['a', 'b', 'c'], 'b'), ['b', 'a', 'c']);
    assert.deepEqual(recentList(['x', 'a', 'b'], 'x'), ['x', 'a', 'b']);
    // 同一命令连续执行两次也不叠加
    assert.deepEqual(recentList(recentList([], 'go'), 'go'), ['go']);
});

test('recentList: 存储键名与上限符合规格（wp.cmd.recent / 5，S2 对齐 wp.* 惯例）', () => {
    assert.equal(RECENT_KEY, 'wp.cmd.recent');
    assert.equal(RECENT_MAX, 5);
});
