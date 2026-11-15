// 指挥树（批次 U2）— 把 GET /api/command/overview 的 tree 字段渲染成一层层节点。
// 纯函数经 module.exports 供 Node 单测（tests/js/tasks_tree.test.mjs）；页面侧
// 挂进 window.WPTasksOverview.tree（不为一个 mixin 新增 window 全局——登记表
// ALLOWED_WINDOW 在本批边界外），由 tasks.js 用 app.mixin 装进页面。
// 跨文件复用不复制实现：行内文案/计数/状态文案调 WPTasksOverview 的同名纯函数，
// 「更多」的去重合并调 WPTasksOverview.mergeMoreTasks，主管类型/最近活跃文案调
// WPSettingsLeads，相对时间走 WPCommon.fmtAgo（模板侧经 mixin 的 fmtAgo）。
(function () {
    'use strict';

    var STORE_KEY = 'wp.commandTree.open';   // R16：wp.<domain>.<name>
    var HIGHLIGHT_MS = 2600;                 // 定位后高亮多久（毫秒）
    // 未登记的指挥者 / 已停用的主管：行内提示文案（规格 U2「登记为主管」入口；
    // 自助登记接口在并行批次 SR，本批只给提示，不新增写接口）
    var REGISTER_HINT = '未登记的指挥者：让它运行 deskport.py lead --register <名字> 即可登记';
    var ORPHAN_HINT = '主管记录已停用或不存在；这些任务仍按原归属列在这里';

    // ---- 展开状态（本地存储，刷新后保持；例 4）----
    function readOpen() {
        try {
            var raw = localStorage.getItem(STORE_KEY);
            var obj = raw ? JSON.parse(raw) : null;
            return (obj && typeof obj === 'object' && !Array.isArray(obj)) ? obj : {};
        } catch (err) {
            return {};                        // 隐私模式/坏 JSON：按没存过处理
        }
    }

    function writeOpen(map) {
        try {
            localStorage.setItem(STORE_KEY, JSON.stringify(map || {}));
        } catch (err) { /* 存不下不影响当场展开 */ }
    }

    // ---- 默认展开：子树里有「等你」或在跑的后代就展开，其余折叠 ----
    // 后端 counts 是逐级汇总的，看节点自己的 counts 就够，不用再递归。
    function autoOpenKeys(nodes) {
        var keys = {};
        (Array.isArray(nodes) ? nodes : []).forEach(function (n) { walk(n); });
        function walk(node) {
            var c = node.counts || {};
            if ((Number(c.running) || 0) > 0 || (Number(c.waiting_you) || 0) > 0) {
                keys[node.key] = true;
            }
            (node.children || []).forEach(walk);
        }
        return keys;
    }

    // 用户显式选择覆盖默认（存过就用存过的，没存过按默认）
    function resolveOpen(nodes, stored) {
        var out = autoOpenKeys(nodes);
        Object.keys(stored || {}).forEach(function (k) { out[k] = !!stored[k]; });
        return out;
    }

    // ---- 深度优先摊平：一行一个节点，带层级与父 key ----
    // 模板一层 v-for 渲染，不写递归组件（Vue 全局构建没有递归组件注册）。
    function flattenTree(nodes, open) {
        var rows = [];
        var opened = open || {};
        (Array.isArray(nodes) ? nodes : []).forEach(function (n) { walk(n, 0, ''); });
        function walk(node, depth, parentKey) {
            var kids = node.children || [];
            rows.push({node: node, key: node.key || '', depth: depth,
                       parentKey: parentKey, expandable: kids.length > 0,
                       expanded: !!opened[node.key]});
            if (opened[node.key]) {
                kids.forEach(function (k) { walk(k, depth + 1, node.key || ''); });
            }
        }
        return rows;
    }

    // ---- taskId → {path: [节点名…], keys: [祖先 key…]} ----
    // 待办行显示路径（"主管 A › 任务 a2"）与点击定位都要它。
    function treeIndex(nodes) {
        var byTask = {};
        (Array.isArray(nodes) ? nodes : []).forEach(function (n) { walk(n, [], []); });
        function walk(node, names, keys) {
            var mine = names.concat([node.name || node.title || '']);
            var mineKeys = keys.concat([node.key || '']);
            if (node.type === 'task' && node.id) { byTask[node.id] = {path: mine, keys: mineKeys}; }
            (node.children || []).forEach(function (k) { walk(k, mine, mineKeys); });
        }
        return byTask;
    }

    // 「更多」拉回来的 brief → 任务节点（与后端 _taskNode 同形：brief + type/key）
    function taskNode(brief) {
        var b = brief || {};
        return Object.assign({}, b, {type: 'task', key: 'task:' + (b.id || '')});
    }

    // ---- 「更多」：把 /api/tasks?full=1&lead= 拉回来的 briefs 并进对应节点 ----
    // 去重合并复用 WPTasksOverview.mergeMoreTasks（R20 不复制第二份）；任务子项
    // 排在子主管前面，保持树里的分组顺序。
    function withMoreTasks(nodes, moreTasks) {
        var more = moreTasks || {};
        var merge = (typeof WPTasksOverview !== 'undefined'
                     && WPTasksOverview.mergeMoreTasks) || null;
        return (Array.isArray(nodes) ? nodes : []).map(function (n) {
            var kids = withMoreTasks(n.children, more);
            var extra = more[n.key || ''];
            if (!extra || !extra.length || !merge) {
                return Object.assign({}, n, {children: kids});
            }
            var tasks = kids.filter(function (k) { return k.type === 'task'; });
            var rest = kids.filter(function (k) { return k.type !== 'task'; });
            var merged = merge(tasks, extra).map(taskNode);
            return Object.assign({}, n, {children: merged.concat(rest)});
        });
    }

    // ---- 节点 → 行视图：文案/徽标/计数都在这里出，模板只摆位置 ----
    function nodeView(node, depth, agoText, waitingIds) {
        var ov = (typeof WPTasksOverview !== 'undefined') ? WPTasksOverview : {};
        var n = node || {};
        var type = n.type || '';
        var view = {
            key: n.key || '', type: type, id: n.id || '', depth: depth || 0,
            name: n.name || n.title || '',
            countsText: ov.countsText ? ov.countsText(n.counts) : '',
            closedHidden: Number(n.closedHidden) || 0,
            canMore: false, moreHint: false, hint: '', badge: '',
            kindText: '', state: '', stateText: '', waitingYou: false,
            dotClass: '', statusText: '', sub: '', lastReportText: '',
            lastSeenText: '', unreadReports: 0, taskId: '', paused: false,
            activityText: '', activity: null, activityRecent: [],
        };
        if (type === 'lead') {
            view.kindText = kindTextOf(n.kind);
            view.state = n.state || 'asleep';
            view.stateText = ov.stateText ? ov.stateText(view.state) : '';
            view.taskId = n.taskId || '';
            view.paused = n.status === 'paused';
            view.unreadReports = Number(n.unreadReports) || 0;
            view.lastSeenText = lastSeenTextOf(n.lastSeenAt);
            view.lastReportText = n.lastReport && ov.firstLine
                ? ov.firstLine(n.lastReport.text, 60) : '';
        } else if (type === 'task') {
            var row = ov.taskRowView ? ov.taskRowView(n, agoText) : {};
            view.dotClass = row.dotClass || ('task-dot is-' + (n.status || ''));
            view.statusText = row.statusText || '';
            view.sub = row.sub || '';
            view.waitingYou = !!(waitingIds && waitingIds[n.id]);
        } else if (type === 'owner') {
            view.badge = '未登记';
            view.hint = REGISTER_HINT;
        } else if (type === 'orphan') {
            view.badge = '已停用';
            view.hint = ORPHAN_HINT;
        }
        // 指挥者的直接活动（U2 追加）：主管与派活令牌不派任务也会自己动手
        var act = n.activity;
        if (act && (type === 'lead' || type === 'owner')) {
            view.activity = act;
            view.activityText = '直接操作：执行 ' + (Number(act.exec) || 0)
                + ' 次、写文件 ' + (Number(act.writes) || 0) + ' 次'
                + (act.lastAt ? ' · ' + agoTextOf(act.lastAt) : '');
            view.activityRecent = (act.recent || []).map(function (r) {
                return {at: r.at || 0, time: timeText(r.at), action: r.action || '',
                        summary: r.summary || ''};
            });
        }
        if (view.closedHidden > 0) {
            // 只有主管节点能按 lead 拉全量（/api/tasks?lead=）；其它组只提示
            view.canMore = type === 'lead';
            view.moreHint = type !== 'lead';
        }
        return view;
    }

    function agoTextOf(ts) {
        return (typeof WPCommon !== 'undefined' && WPCommon.fmtAgo)
            ? WPCommon.fmtAgo(ts) : '';
    }

    function timeText(ts) {
        return (typeof WPCommon !== 'undefined' && WPCommon.fmtTime)
            ? WPCommon.fmtTime(ts) : '';
    }

    function kindTextOf(kind) {
        return (typeof WPSettingsLeads !== 'undefined' && WPSettingsLeads.kindText)
            ? WPSettingsLeads.kindText(kind) : (kind || '');
    }

    function lastSeenTextOf(lastSeenAt) {
        return (typeof WPSettingsLeads !== 'undefined'
                && WPSettingsLeads.lastSeenText)
            ? WPSettingsLeads.lastSeenText(lastSeenAt, Date.now() / 1000) : '';
    }

    function scrollToKey(key) {
        var nodes = document.querySelectorAll('.cmd-node');
        for (var i = 0; i < nodes.length; i += 1) {
            if (nodes[i].getAttribute('data-key') === key) {
                if (nodes[i].scrollIntoView) { nodes[i].scrollIntoView({block: 'center'}); }
                return;
            }
        }
    }

    function mixin() {
        return {
            data() {
                return {
                    treeOpen: readOpen(),   // key → 用户显式的展开/折叠（本地存储）
                    moreTasks: {},          // 节点 key → 「更多」拉回来的 briefs
                    moreCapped: {},         // 节点 key → 拉满 50 条被截断
                    highlightKey: '',       // 待办点击后要高亮的节点 key
                    highlightTimer: null,
                    treeErrors: {},
                    activityKey: '',        // 哪个节点的直接活动明细展开着
                };
            },
            computed: {
                // 默认展开 + 用户选择合并后的展开表（树渲染与定位都读它）
                openMap() {
                    return resolveOpen(this.overview && this.overview.tree,
                                       this.treeOpen);
                },
                treeIndexMap() {
                    return treeIndex(this.overview && this.overview.tree);
                },
                // 有待办的任务 id → 树里的任务行补「等你」标（例 3）
                pendingTaskIds() {
                    var ids = {};
                    ((this.overview && this.overview.pending) || [])
                        .forEach(function (p) {
                            if (p.taskId) { ids[p.taskId] = true; }
                        });
                    return ids;
                },
                treeRows() {
                    var self = this;
                    var nodes = withMoreTasks(this.overview && this.overview.tree,
                                              this.moreTasks);
                    return flattenTree(nodes, this.openMap).map(function (row) {
                        var ago = row.node.type === 'task'
                            ? self.fmtAgo(row.node.lastActiveAt || 0) : '';
                        var view = nodeView(row.node, row.depth, ago,
                                            self.pendingTaskIds);
                        view.expanded = row.expanded;
                        view.expandable = row.expandable;
                        view.parentKey = row.parentKey;
                        view.moreCapped = !!self.moreCapped[row.key];
                        view.error = self.treeErrors[row.key] || '';
                        view.activityOpen = self.activityKey === row.key;
                        return view;
                    });
                },
                // 待办行 + 它在树上的路径（"主管 A › 任务 a2"）+ 人话字段
                pendingWithPath() {
                    var index = this.treeIndexMap;
                    var view = (typeof WPTasksOverview !== 'undefined'
                                && WPTasksOverview.events
                                && WPTasksOverview.events.pendingView) || null;
                    return (this.pendingRows || []).map(function (p) {
                        if (!view) { return p; }
                        return view(p, index, this.leadsById,
                                    (this.overview && this.overview.retiredLeads) || {});
                    }, this);
                },
            },
            mounted() {
                // 展开状态在 created 时已从本地存储读进 data（readOpen）
            },
            beforeUnmount() {
                if (this.highlightTimer) { clearTimeout(this.highlightTimer); }
            },
            methods: {
                toggleNode(row) {
                    var open = Object.assign({}, this.treeOpen);
                    open[row.key] = !row.expanded;
                    this.treeOpen = open;
                    writeOpen(open);
                },
                expandKey(key) {
                    var open = Object.assign({}, this.treeOpen);
                    open[key] = true;
                    this.treeOpen = open;
                    writeOpen(open);
                },
                // 点节点主体：任务节点打开它的详情；托管主管打开它的会话；
                // 其余（分组节点）切换展开
                openNode(row) {
                    if (row.type === 'task') {
                        this.openTask(row.id, row);
                        return;
                    }
                    if (row.type === 'lead' && row.taskId) {
                        this.openTask(row.taskId);
                        return;
                    }
                    if (row.expandable) { this.toggleNode(row); }
                },
                // 直接活动明细：点节点第二行那行字展开/收起最近 10 条
                toggleActivity(row) {
                    this.activityKey = this.activityKey === row.key ? '' : row.key;
                },
                // 待办点击：展开到该节点、滚到可见、高亮几秒（例 3）
                locatePending(row) {
                    var self = this;
                    var hit = this.treeIndexMap[row.taskId || ''];
                    var keys = hit ? hit.keys : (row.leadId ? ['lead:' + row.leadId] : []);
                    if (!keys.length) { return; }
                    keys.forEach(function (k) { self.expandKey(k); });
                    this.highlightKey = keys[keys.length - 1];
                    if (this.highlightTimer) { clearTimeout(this.highlightTimer); }
                    this.highlightTimer = setTimeout(function () {
                        self.highlightKey = '';
                        self.highlightTimer = null;
                    }, HIGHLIGHT_MS);
                    this.$nextTick(function () {
                        scrollToKey(self.highlightKey);
                    });
                },
                // 详情面板「所属主管」链接：在树里展开并定位到该主管
                gotoLead(leadId) {
                    var key = 'lead:' + (leadId || '');
                    this.expandKey(key);
                    this.backToList(false);
                    this.$nextTick(function () { scrollToKey(key); });
                },
                async loadMoreTasks(row) {
                    if (row.type !== 'lead' || this.moreTasks[row.key]) { return; }
                    try {
                        const r = await fetch('/api/tasks?full=1&lead='
                                              + encodeURIComponent(row.id));
                        const data = await r.json();
                        if (!r.ok) {
                            throw new Error(data.error || ('HTTP ' + r.status));
                        }
                        this.moreTasks[row.key] = data.tasks || [];
                        // 拉满 50 条被截断：按钮换成置灰提示，不静默消失
                        this.moreCapped[row.key] = !!data.truncated;
                        WPCommon.clearError(this, 'treeErrors', row.key);
                    } catch (err) {
                        WPCommon.flashError(this, 'treeErrors', row.key, err.message);
                    }
                },
            },
        };
    }

    if (typeof window !== 'undefined' && window.WPTasksOverview) {
        window.WPTasksOverview.tree = {
            readOpen: readOpen, writeOpen: writeOpen, autoOpenKeys: autoOpenKeys,
            resolveOpen: resolveOpen, flattenTree: flattenTree,
            treeIndex: treeIndex, taskNode: taskNode, withMoreTasks: withMoreTasks,
            nodeView: nodeView, mixin: mixin,
        };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            readOpen: readOpen, writeOpen: writeOpen, autoOpenKeys: autoOpenKeys,
            resolveOpen: resolveOpen, flattenTree: flattenTree,
            treeIndex: treeIndex, taskNode: taskNode, withMoreTasks: withMoreTasks,
            nodeView: nodeView, mixin: mixin,
        };
    }
})();
