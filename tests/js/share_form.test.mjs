// 分享表单提交体构造（frontend/assets/js/share-form.js 的 buildBody）单测。
// 这条路 E3 用"改坏实验"证明过是裸的：把 if (cwd) 改成无条件赋值、把
// isLocalRun 改成恒 true，全量 670 passed 照样绿。跑法：node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import shareForm from '../../frontend/assets/js/share-form.js';

const { buildBody } = shareForm;

test('本机留空不发 cwd 键，接到任务留空不发 taskId 键', () => {
    const local = buildBody({ticket: 'tk', way: 'new', node: 'local', cwd: '  '});
    assert.equal('cwd' in local, false);
    const append = buildBody({ticket: 'tk', way: 'append', taskId: ''});
    assert.equal('taskId' in append, false);
});

test('远端执行点必须带 cwd 键', () => {
    const body = buildBody({ticket: 'tk', way: 'new', node: 'srv-1', cwd: ' /srv/work '});
    assert.equal(body.cwd, '/srv/work');
    // 远端留空也发键（空串）：服务端据此 400「执行点需要 cwd」，不静默按本机跑
    const empty = buildBody({ticket: 'tk', way: 'new', node: 'srv-1', cwd: ''});
    assert.equal('cwd' in empty, true);
    assert.equal(empty.cwd, '');
});

test('只上传必须带 node + target', () => {
    const remote = buildBody({ticket: 'tk', way: 'upload', node: 'srv-1', target: 'phone'});
    assert.equal(remote.node, 'srv-1');
    assert.equal(remote.target, 'phone');
    const local = buildBody({ticket: 'tk', way: 'upload', target: 'phone'});
    assert.equal(local.node, 'local');   // 缺省本机
});

test('三种去向的键互不串门（E3 改坏实验的回归网）', () => {
    const body = buildBody({
        ticket: 'tk', way: 'new', prompt: '看下', node: 'local', mode: 'auto',
        cwd: '', taskId: 't-1', target: 'phone', subdir: 'x'
    });
    assert.equal(body.way, 'new');
    assert.equal(body.organize, 'none');
    assert.equal('cwd' in body, false);
    assert.equal('taskId' in body, false);
    assert.equal('target' in body, false);
    assert.equal('subdir' in body, false);
});
