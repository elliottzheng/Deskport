// 设置页「我的权限」卡片视图（frontend/assets/js/settings-capability.js 的
// view/kindText）单测。跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import capability from '../../frontend/assets/js/settings-capability.js';

const { view, kindText } = capability;

test('主体 kind 映射成本文，未知 kind 原样露出不冒充', () => {
    assert.equal(kindText('human'), '人');
    assert.equal(kindText('agent'), '智能体');
    assert.equal(kindText('device'), '设备');
    assert.equal(kindText('remote'), '联邦成员');
    assert.equal(kindText('service'), 'service');
    assert.equal(kindText(''), '未知主体');
});

test('view: 规则行带来源，示例判定转成 allow 布尔', () => {
    const v = view({
        principal: {kind: 'agent', name: '手机分享'},
        rules: [{action: 'share', resource: 'node:local', scope: 'any',
                 source: 'scope:share'}],
        decisions: [
            {action: 'task:create', resource: 'node:local', verdict: 'deny'},
            {action: 'share', resource: 'node:local', verdict: 'allow'},
        ],
    });
    assert.equal(v.kind, '智能体');
    assert.equal(v.name, '手机分享');
    assert.deepEqual(v.rules,
        [{action: 'share', resource: 'node:local', source: 'scope:share'}]);
    assert.deepEqual(v.decisions, [
        {action: 'task:create', resource: 'node:local', allow: false},
        {action: 'share', resource: 'node:local', allow: true},
    ]);
});

test('view: 缺字段不白屏（空态不是"没有权限"的结论）', () => {
    const v = view({});
    assert.equal(v.kind, '未知主体');
    assert.equal(v.name, '未命名');
    assert.deepEqual(v.rules, []);
    assert.deepEqual(v.decisions, []);
    assert.deepEqual(view(null).decisions, []);
    assert.deepEqual(view(undefined).rules, []);
});
