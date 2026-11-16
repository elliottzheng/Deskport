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

test('recentList: 存储键名与上限符合规格（cmd.recent / 5）', () => {
    assert.equal(RECENT_KEY, 'cmd.recent');
    assert.equal(RECENT_MAX, 5);
});
