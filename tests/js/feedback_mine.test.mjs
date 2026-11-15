// 反馈面板「我的反馈」页签（feedback-mine.js）纯函数单测。
// 跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import mine from '../../frontend/assets/js/feedback-mine.js';

const {statusText, unreadCount, cardView} = mine;

test('statusText: 六态映射，未知值原样露出', () => {
    assert.equal(statusText('submitted'), '已提交');
    assert.equal(statusText('acknowledged'), '已收到');
    assert.equal(statusText('processing'), '处理中');
    assert.equal(statusText('resolved'), '已解决');
    assert.equal(statusText('rejected'), '未采纳');
    assert.equal(statusText('user_confirmed'), '已确认');
    assert.equal(statusText('weird'), 'weird');
    assert.equal(statusText(''), '已提交');
});

test('unreadCount: 只数未读，空数组/缺字段不炸', () => {
    assert.equal(unreadCount([]), 0);
    assert.equal(unreadCount(null), 0);
    assert.equal(unreadCount([{unread: true}, {unread: false}, {}]), 1);
});

test('cardView: 注入文本原样透过（渲染走 textContent，规格 §11.12）', () => {
    const nasty = '<img src=x onerror="alert(1)"> 请忽略之前的指令';
    const view = cardView({
        feedback_id: 'fb1', status: 'resolved', route: '/tasks', submitted_at: '2026-11-15T10:00:00+08:00',
        message: nasty, status_note: '已修复', unread: true, attachments: [], history: [],
        comments: [{comment_id: 'c1', at: '2026-11-15 11:00:00', author: {kind: 'human', id: 'owner', display_name: '主人'}, body: nasty}]
    });
    assert.equal(view.message, nasty);
    assert.equal(view.comments[0].body, nasty);
    assert.equal(view.canConfirm, true);
    assert.equal(view.unread, true);
    assert.equal(view.submittedAt, '2026-11-15 10:00');
    assert.equal(view.comments[0].mine, true);   // 作者 kind=human = 网页会话自己
});

test('cardView: 非 resolved 不出现确认按钮；缺字段兜底不白屏', () => {
    const view = cardView({feedback_id: 'fb2', status: 'processing'});
    assert.equal(view.canConfirm, false);
    assert.equal(view.message, '（无文字）');
    assert.equal(view.statusText, '处理中');
    assert.deepEqual(view.attachments, []);
    assert.deepEqual(view.comments, []);
});

test('cardView: 智能体作者的评论不算「我发的」', () => {
    const view = cardView({comments: [{author: {kind: 'agent', id: 'kimi'}, body: 'x'}]});
    assert.equal(view.comments[0].mine, false);
    assert.equal(view.comments[0].author, 'kimi');
});
