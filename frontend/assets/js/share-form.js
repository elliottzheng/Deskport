// 分享表单 /m/share 的提交体构造 — 纯函数模块：Node 可直接 import 跑单测
// （tests/js/share_form.test.mjs），页面经 window.WPShareForm 调用
// （m-share.js 只负责收 DOM 值）。字段规则与后端
// modules/share_core.parseShareOptions 对齐：本机 cwd 留空就不发键、
// 远端执行点必须带 cwd、只上传必须带 node + target。
(function () {
    'use strict';

    function buildBody(form) {
        var input = form || {};
        var way = input.way || 'new';
        var body = {ticket: input.ticket || '', way: way,
                    prompt: input.prompt || ''};
        if (way === 'append') {
            // 留空就不发 taskId 键（后端按"没给"处理，不会拿空串建任务）
            if (input.taskId) { body.taskId = input.taskId; }
            return body;
        }
        if (way === 'upload') {
            // 只上传：node + target 必带（后端据此决定落到哪台机器的哪个目录）
            body.node = input.node || 'local';
            body.target = input.target || '';
            var subdir = (input.subdir || '').trim();
            if (subdir) { body.subdir = subdir; }
            return body;
        }
        // way === 'new'：交给 AI 新建任务
        var node = input.node || 'local';
        body.node = node;
        body.mode = input.mode || 'default';
        body.model = input.model || '';
        var cwd = (input.cwd || '').trim();
        // 本机留空不发 cwd 键，让服务端用任务目录；远端执行点必须带 cwd
        // （空串也发，服务端据此给出"执行点需要 cwd"，不静默按本机跑）
        if (cwd || node !== 'local') { body.cwd = cwd; }
        body.organize = input.organize ? 'folder' : 'none';
        body.folderName = (input.folderName || '').trim();
        return body;
    }

    if (typeof window !== 'undefined') {
        window.WPShareForm = {buildBody: buildBody};
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {buildBody: buildBody};
    }
})();
