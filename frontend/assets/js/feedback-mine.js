// 反馈面板「我的反馈」页签（批次 F1，规格 §6.3）：只列自己的 + 未读红点 +
// 评论线程 + 提交者确认/不认账。从 feedback-panel.js 拆出：面板管采集与画布，
// 本模块管提交者视角的闭环（追问能看到、能回复、能认账）。
// 纯函数（statusText/unreadCount/cardView/replyTarget）经 module.exports 供
// tests/js/ 的 node 单测；页面侧挂 window.WPFeedbackMine。
(function () {
    'use strict';

    var STATUS_TEXT = {
        submitted: '已提交', acknowledged: '已收到', processing: '处理中',
        resolved: '已解决', rejected: '未采纳', user_confirmed: '已确认'
    };
    var POLL_MS = 20000;                  // 红点轮询：处理者一追问，20 秒内看得到
    var listEl = null;
    var badgeEl = null;
    var timer = null;
    var lastUnread = -1;
    var items = [];

    function statusText(status) {
        return STATUS_TEXT[status] || status || '已提交';
    }

    function unreadCount(items) {
        return (items || []).filter(function (i) { return i && i.unread; }).length;
    }

    // 端点 JSON → 卡片视图。缺字段兜底（老服务端/坏数据不白屏），但内容全是
    // textContent 渲染：注入的 <img onerror> / Markdown / "忽略之前的指令"
    // 都按纯文本显示（规格 §11.12、红线三）。
    function cardView(item) {
        var src = item || {};
        var comments = Array.isArray(src.comments) ? src.comments : [];
        return {
            id: src.feedback_id || '',
            status: src.status || 'submitted',
            statusText: statusText(src.status),
            route: src.route || '',
            submittedAt: (src.submitted_at || '').slice(0, 16).replace('T', ' '),
            message: src.message || '（无文字）',
            note: src.status_note || '',
            unread: !!src.unread,
            canConfirm: src.status === 'resolved',
            attachments: Array.isArray(src.attachments) ? src.attachments : [],
            history: Array.isArray(src.history) ? src.history : [],
            comments: comments.map(function (c) {
                var author = c.author || {};
                return {
                    id: c.comment_id || '',
                    at: (c.at || '').slice(0, 16).replace('T', ' '),
                    author: author.display_name || author.id || '未知',
                    // 网页会话只有一个身份（主人）：作者 kind=human 就是我自己发的
                    mine: author.kind === 'human',
                    body: c.body || ''
                };
            })
        };
    }

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) { node.className = cls; }
        if (text) { node.textContent = text; }
        return node;
    }

    function findList() {
        if (listEl && document.body.contains(listEl)) { return listEl; }
        listEl = document.querySelector('.fb-mine-list');
        return listEl;
    }

    // ---- 红点：悬浮按钮上的未读数 ----
    function ensureBadge() {
        if (badgeEl && document.body.contains(badgeEl)) { return badgeEl; }
        var fab = document.querySelector('.fb-fab');
        if (!fab) { return null; }
        badgeEl = el('span', 'fb-fab-badge');
        badgeEl.hidden = true;
        fab.appendChild(badgeEl);
        return badgeEl;
    }

    function paintBadge(count) {
        var badge = ensureBadge();
        if (!badge) { return; }
        badge.hidden = !(count > 0);
        badge.textContent = count > 99 ? '99+' : String(count);
    }

    function refreshUnread() {
        return fetch('/api/feedback/mine', { credentials: 'same-origin' })
            .then(function (res) { return res.ok ? res.json() : Promise.reject(res.status); })
            .then(function (data) {
                items = (data && data.items) || [];
                lastUnread = (data && data.unread) || 0;
                paintBadge(lastUnread);
                var body = document.querySelector('.fb-mine');
                if (body && !body.hidden && findList()) {
                    render(items);       // 页签开着：顺手刷新列表（输入框内容不重填）
                }
            })
            .catch(function () { /* 网络波动：红点维持上次的值，不假报清零 */ });
    }

    // ---- 列表 ----
    function render(list) {
        var host = findList();
        if (!host) { return; }
        host.textContent = '';
        var views = (list || []).map(cardView);
        if (!views.length) {
            host.appendChild(el('div', 'fb-empty', '还没有提交过反馈。'));
            return;
        }
        views.forEach(function (card) { host.appendChild(cardNode(card)); });
    }

    function cardNode(card) {
        var node = el('div', 'fb-card');
        if (card.unread) { node.classList.add('is-unread'); }
        var top = el('div', 'fb-card-top');
        var badge = el('span', 'fb-badge', card.statusText);
        badge.setAttribute('data-status', card.status);
        top.appendChild(badge);
        top.appendChild(el('span', 'fb-card-route', card.route || ''));
        top.appendChild(el('span', 'fb-card-time', card.submittedAt));
        if (card.unread) { top.appendChild(el('span', 'fb-card-unread', '有新进展')); }
        node.appendChild(top);
        node.appendChild(el('div', 'fb-card-msg', card.message));
        if (card.note) {
            node.appendChild(el('div', 'fb-card-note', '处理说明：' + card.note));
        }
        card.attachments.forEach(function (att) {
            var img = document.createElement('img');
            img.className = 'fb-card-shot';
            img.loading = 'lazy';
            img.src = att.url;
            img.alt = '截图';
            node.appendChild(img);
        });
        node.appendChild(historyNode(card.history));
        node.appendChild(commentsNode(card));
        if (card.canConfirm) { node.appendChild(actionsNode(card)); }
        return node;
    }

    function historyNode(history) {
        var box = el('div', 'fb-history');
        history.slice(-6).forEach(function (h) {
            var from = h.from || '';
            var to = h.to || h.status || '';
            var who = h.actor === 'system' ? '系统' : (h.actor === 'handler' ? '处理者' : '提交者');
            box.appendChild(el('div', 'fb-history-row',
                (h.at || '').slice(0, 16).replace('T', ' ') + ' · ' + who +
                (from ? '：' + from + ' → ' + to : '：' + to) +
                (h.reason ? '（' + h.reason + '）' : '')));
        });
        return box;
    }

    function commentsNode(card) {
        var box = el('div', 'fb-comments');
        card.comments.forEach(function (c) {
            var row = el('div', 'fb-comment');
            var head = el('div', 'fb-comment-head');
            head.appendChild(el('span', 'fb-comment-author', c.author + (c.mine ? '（我）' : '')));
            head.appendChild(el('span', 'fb-comment-at', c.at));
            row.appendChild(head);
            row.appendChild(el('div', 'fb-comment-body', c.body));
            box.appendChild(row);
        });
        var reply = el('div', 'fb-reply');
        var input = document.createElement('textarea');
        input.className = 'fb-reply-input';
        input.rows = 2;
        input.placeholder = '回复处理者 / 补充情况…';
        var btn = el('button', 'fb-reply-send', '回复');
        btn.type = 'button';
        btn.addEventListener('click', function () { send(card.id, input, btn); });
        reply.appendChild(input);
        reply.appendChild(btn);
        box.appendChild(reply);
        return box;
    }

    function send(feedbackId, input, btn) {
        var text = (input.value || '').trim();
        if (!text) { input.focus(); return; }
        btn.disabled = true;
        fetch('/api/feedback/' + encodeURIComponent(feedbackId) + '/comments', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ body: text })
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (body) {
                if (!res.ok) { throw new Error(body.error || ('HTTP ' + res.status)); }
                return body;
            });
        }).then(function () {
            input.value = '';
            return refreshUnread();
        }).catch(function (err) {
            btn.disabled = false;
            if (window.WPFeedbackPanel) { window.WPFeedbackPanel.setHint('回复失败：' + err.message, true); }
        });
    }

    function actionsNode(card) {
        var box = el('div', 'fb-actions');
        var ok = el('button', 'fb-btn fb-btn-ok', '确认解决');
        ok.type = 'button';
        ok.addEventListener('click', function () { act(card.id, 'confirm', null, ok); });
        var no = el('button', 'fb-btn', '没解决，重开');
        no.type = 'button';
        no.addEventListener('click', function () {
            var reason = window.prompt('说一下哪里还没解决？', '');
            if (reason && reason.trim()) { act(card.id, 'reopen', reason.trim(), no); }
        });
        box.appendChild(ok);
        box.appendChild(no);
        return box;
    }

    function act(feedbackId, action, reason, btn) {
        btn.disabled = true;
        var body = action === 'reopen' ? { reason: reason } : {};
        return fetch('/api/feedback/' + encodeURIComponent(feedbackId) + '/' + action, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body)
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) {
                if (!res.ok) { throw new Error(data.error || ('HTTP ' + res.status)); }
                return data;
            });
        }).then(function () {
            return refreshUnread();
        }).catch(function (err) {
            btn.disabled = false;
            if (window.WPFeedbackPanel) { window.WPFeedbackPanel.setHint('操作失败：' + err.message, true); }
        });
    }

    // ---- 打开页签：拉列表 + 清未读（规格 §6.3「打开即清」）----
    function show() {
        if (!findList()) { return; }
        fetch('/api/feedback/mine', { credentials: 'same-origin' })
            .then(function (res) { return res.ok ? res.json() : Promise.reject(res.status); })
            .then(function (data) {
                items = (data && data.items) || [];
                render(items);
                paintBadge((data && data.unread) || 0);
                if ((data && data.unread) || 0) {
                    return fetch('/api/feedback/mine/read', {
                        method: 'POST', credentials: 'same-origin'
                    }).then(function () { paintBadge(0); })
                        .catch(function () { /* 清失败不堵渲染：红点下次打开再清 */ });
                }
                return null;
            })
            .catch(function () {
                var host = findList();
                if (host) {
                    host.textContent = '';
                    host.appendChild(el('div', 'fb-empty', '读取失败，稍后重开一下。'));
                }
            });
    }

    function init() {
        ensureBadge();
        refreshUnread();
        if (timer) { clearInterval(timer); }
        timer = setInterval(refreshUnread, POLL_MS);
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) { refreshUnread(); }
        });
    }

    if (typeof window !== 'undefined') {
        window.WPFeedbackMine = { init: init, show: show, refresh: refreshUnread,
                                  statusText: statusText, unreadCount: unreadCount,
                                  cardView: cardView };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { statusText: statusText, unreadCount: unreadCount,
                           cardView: cardView };
    }
})();
