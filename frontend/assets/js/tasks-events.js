// 任务详情的事件流（批次 U3）：工具调用说人话、流式参数残片兜底、tool_result
// 折叠、连续 status 合并、审批卡片带命令与用途、任何宽度都不横向滚动。
// 纯函数经 module.exports 供 Node 单测（tests/js/tasks_events.test.mjs）；页面侧
// 经 window.WPTasksOverview.events 的 mixin 挂进 tasks.js（照 tasks-tree.js 的
// 形状：挂在已登记的全局下，不新增 window 全局）。跨文件复用不复制实现：状态/
// 模式文案调 WPTasksOverview，相对时间走 WPCommon.fmtAgo。
(function () {
    'use strict';

    var BASH_MAX_LINES = 3;        // 多行命令默认显示前 3 行 + 「展开全部」
    var DIFF_MAX_LINES = 6;        // Edit 改动片段默认显示行数
    var PARAM_MAX = 60;            // 其它工具的关键参数摘要上限
    var PATH_KEYS = ['path', 'file_path', 'notebook_path', 'filename'];
    var FILE_TOOLS = {Read: 1, Edit: 1, Write: 1, NotebookEdit: 1, MultiEdit: 1};
    var STATUS_JOIN = ' · ';

    // ---- 参数解析：事件里的 args 可能是 dict、JSON 串，或还没到齐的残片 ----
    function isPartial(args) {
        /**流式残片：'{' / '{"a": ' / '[' 这类到不齐的 JSON 前缀。*/
        if (args && typeof args === 'object') { return false; }
        var raw = String(args === null || args === undefined ? '' : args).trim();
        if (!raw) { return false; }
        if (raw[0] !== '{' && raw[0] !== '[') { return false; }
        try {
            JSON.parse(raw);
            return false;
        } catch (err) {
            return true;
        }
    }

    function parseArgs(args) {
        /**args → dict；残片/坏数据/空返回 null（调用方显示「参数加载中」）。*/
        if (args && typeof args === 'object') { return args; }
        var raw = String(args === null || args === undefined ? '' : args).trim();
        if (!raw) { return null; }
        try {
            var value = JSON.parse(raw);
            return (value && typeof value === 'object') ? value : null;
        } catch (err) {
            return null;
        }
    }

    function firstPath(obj) {
        for (var i = 0; i < PATH_KEYS.length; i += 1) {
            if (obj && obj[PATH_KEYS[i]]) { return String(obj[PATH_KEYS[i]]); }
        }
        return '';
    }

    function firstLine(text, max) {
        var raw = String(text === null || text === undefined ? '' : text);
        var line = raw.split('\n')[0].trim();
        var limit = Number(max) || 0;
        return limit && line.length > limit ? line.slice(0, limit) + '…' : line;
    }

    function lineCount(text) {
        var raw = String(text === null || text === undefined ? '' : text);
        if (!raw) { return 0; }
        return raw.split('\n').length;
    }

    // ---- 工具调用 → 人话的一行 ----
    function toolView(name, args) {
        /**返回 {kind, headline, preview, totalLines, purpose, diff, loading}。*/
        var tool = String(name || '工具');
        if (isPartial(args)) {
            return {kind: 'loading', headline: '参数加载中…', preview: [],
                    totalLines: 0, purpose: '', diff: null, loading: true};
        }
        var obj = parseArgs(args);
        if (!obj) {
            return {kind: 'params', headline: tool, preview: [], totalLines: 0,
                    purpose: '', diff: null, loading: false};
        }
        var purpose = obj.description ? firstLine(obj.description, PARAM_MAX) : '';
        if (FILE_TOOLS[tool]) {
            var path = firstPath(obj);
            var view = {kind: 'file', headline: path || tool, preview: [],
                        totalLines: 0, purpose: purpose, diff: null,
                        loading: false};
            if (tool === 'Edit' || tool === 'MultiEdit') {
                var old = String(obj.old_string || '');
                var now = String(obj.new_string || obj.edits || '');
                view.diff = {oldText: old, newText: now,
                             oldLines: old ? old.split('\n').slice(0, DIFF_MAX_LINES) : [],
                             newLines: now ? now.split('\n').slice(0, DIFF_MAX_LINES) : [],
                             oldTotal: lineCount(old), newTotal: lineCount(now)};
            }
            if (tool === 'Write' && obj.content) {
                view.totalLines = lineCount(obj.content);
            }
            return view;
        }
        var command = obj.command ? String(obj.command) : '';
        if (command) {
            var lines = command.split('\n');
            return {kind: 'command', headline: firstLine(command, PARAM_MAX),
                    preview: lines.slice(0, BASH_MAX_LINES),
                    totalLines: lines.length, purpose: purpose, diff: null,
                    loading: false};
        }
        // 其它工具：工具名 · 关键参数（第一个像样的标量/短值）
        var keys = Object.keys(obj).filter(function (k) { return k !== 'description'; });
        var bits = [];
        for (var i = 0; i < keys.length && bits.length < 2; i += 1) {
            var value = obj[keys[i]];
            var text = (value && typeof value === 'object')
                ? JSON.stringify(value) : String(value);
            if (text) { bits.push(keys[i] + '=' + firstLine(text, PARAM_MAX)); }
        }
        return {kind: 'params', headline: tool + (bits.length ? ' · ' + bits.join(' · ') : ''),
                preview: [], totalLines: 0, purpose: purpose, diff: null,
                loading: false};
    }

    // ---- tool_result 标题：说几行，不说「工具结果」 ----
    function resultTitle(text, truncated) {
        var n = lineCount(text);
        return '结果 · ' + n + ' 行' + (truncated ? '（已截断）' : '');
    }

    // ---- 连续 status 合并成一行淡色时间线 ----
    function mergeStatus(events) {
        /**events → 渲染行；相邻的 status 合成一行（type='status-run'）。*/
        var rows = [];
        var run = null;
        (Array.isArray(events) ? events : []).forEach(function (e) {
            if (!e || e.type !== 'status') {
                run = null;
                rows.push(e);
                return;
            }
            if (!run) {
                run = {type: 'status-run', key: 'status-' + (e.seq || rows.length),
                       seq: e.seq, at: e.at, items: [], text: ''};
                rows.push(run);
            }
            run.items.push(e);
            run.text = run.items.map(function (x) {
                return String(x.text || '').trim();
            }).filter(Boolean).join(STATUS_JOIN);
        });
        return rows;
    }

    // ---- 「等你处理」一行该说的人话 ----
    function pendingView(row, index, leadsById, retiredById) {
        /**后端 pending 行 + 树上索引 → 显示字段（谁的活、干什么、要我做什么）。*/
        var r = row || {};
        var hit = index && r.taskId ? index[r.taskId] : null;
        var path = hit ? hit.path : [];
        var lead = (leadsById || {})[r.leadId || ''];
        var who = lead ? (lead.name || '主管')
                       : ((retiredById || {})[r.leadId || ''] || '');
        var owner = String(r.owner || '');
        if (!who && owner) {
            who = '未登记的指挥者：' + owner;
        }
        var title = r.title || '';
        var taskTitle = hit ? path[path.length - 1] : (r.taskTitle || '');
        return {
            key: r.key || '',
            kind: r.kind || '',
            kindText: r.kindText || '',
            title: title,
            detail: r.detail || '',
            purpose: r.purpose || '',
            taskTitle: taskTitle,
            leadName: who,
            // 标题已经是主管名（汇报行）时不再重复挂一次
            showLead: !!who && who !== title && who !== taskTitle,
            unregistered: !lead && !!owner,
            registerHint: '它登记成主管后，这类审批会先交给它',
            path: path.join(' › '),
            at: r.at || 0,
            ago: '',
        };
    }

    function agoText(ts) {
        return (typeof WPCommon !== 'undefined' && WPCommon.fmtAgo)
            ? WPCommon.fmtAgo(ts) : '';
    }

    function mixin() {
        return {
            data() {
                return {
                    toolOpen: {},      // 事件 key → 多行命令/Edit 改动是否展开
                    resultOpen: {},    // 事件 key → tool_result 是否展开
                };
            },
            computed: {
                // tasks.js 的 events → 渲染行（连续 status 合并、工具说人话）
                eventRows() {
                    var self = this;
                    return mergeStatus(this.events || []).map(function (e) {
                        var key = (e.type || 'ev') + '-' + (e.seq || 0);
                        var row = {key: key, type: e.type || '', seq: e.seq || 0,
                                   at: e.at || 0, raw: e, text: e.text || '',
                                   truncated: !!e.truncated};
                        if (e.type === 'status-run') {
                            row.text = e.text;
                            row.count = e.items.length;
                            return row;
                        }
                        if (e.type === 'tool') {
                            var view = toolView(e.name, e.args);
                            row.tool = e.name || '工具';
                            row.kind = view.kind;
                            row.headline = view.headline;
                            row.preview = view.preview;
                            row.totalLines = view.totalLines;
                            row.purpose = view.purpose;
                            row.diff = view.diff;
                            row.loading = view.loading;
                            row.expandable = view.totalLines > BASH_MAX_LINES
                                || !!view.diff;
                            row.open = !!self.toolOpen[key];
                            return row;
                        }
                        if (e.type === 'tool_result') {
                            row.title = resultTitle(e.text, e.truncated);
                            row.open = !!self.resultOpen[key];
                            row.full = self.fullEvents[e.seq];
                            return row;
                        }
                        if (e.type === 'permission_request') {
                            var obj = parseArgs(e.args) || {};
                            row.tool = e.name || '工具调用';
                            row.command = obj.command
                                ? firstLine(obj.command, PARAM_MAX) : '';
                            row.path = firstPath(obj);
                            row.purpose = obj.description
                                ? firstLine(obj.description, PARAM_MAX) : '';
                            row.options = Array.isArray(e.options) ? e.options : [];
                            row.resolved = self.permissionResolved(e);
                            row.resultText = self.permissionResultText(e);
                            return row;
                        }
                        if (e.type === 'auto_approved' || e.type === 'auto_denied') {
                            row.text = (e.type === 'auto_approved' ? '已自动放行' : '已自动拒绝')
                                + '：' + (e.name || '') + (e.rule ? '（' + e.rule + '）' : '');
                            return row;
                        }
                        return row;
                    });
                },
            },
            methods: {
                toggleTool(row) {
                    var open = Object.assign({}, this.toolOpen);
                    open[row.key] = !open[row.key];
                    this.toolOpen = open;
                },
                toggleResult(row) {
                    var open = Object.assign({}, this.resultOpen);
                    open[row.key] = !open[row.key];
                    this.resultOpen = open;
                },
                // 多行命令展开后的完整文本（前 3 行之外的也看得到）
                toolFullText(row) {
                    var args = row.raw && row.raw.args;
                    var obj = parseArgs(args);
                    return (obj && obj.command) ? String(obj.command) : '';
                },
                loadResultFull(row) {
                    this.loadFull(row.raw);
                },
                agoOf(ts) {
                    return agoText(ts);
                },
            },
        };
    }

    // 挂在已登记的 window.WPTasksOverview 下（.events），不新增 window 全局
    if (typeof window !== 'undefined' && window.WPTasksOverview) {
        window.WPTasksOverview.events = {
            isPartial: isPartial, parseArgs: parseArgs, toolView: toolView,
            resultTitle: resultTitle, mergeStatus: mergeStatus,
            pendingView: pendingView, mixin: mixin,
        };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            isPartial: isPartial, parseArgs: parseArgs, toolView: toolView,
            resultTitle: resultTitle, mergeStatus: mergeStatus,
            pendingView: pendingView,
        };
    }
})();
