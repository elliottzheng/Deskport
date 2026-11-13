// 分享表单 /m/share — 读 ticket 里的文件，三选一提交，同页出结果。
// 无框架：原生 DOM + fetch；只有 Cookie 会话能调 /api/share/*。
(function () {
    'use strict';

    var ticket = new URLSearchParams(location.search).get('ticket') || '';
    var state = {
        files: [],
        way: 'new',
        expiresAt: 0,
        busy: false,
        done: false,
        loadedNodes: false,
        loadedTasks: false,
        loadedTargets: false,
        targets: [],
        nodeRows: []
    };

    function byId(id) { return document.getElementById(id); }
    function show(el, visible) { if (el) { el.hidden = !visible; } }
    function setText(el, value) { if (el) { el.textContent = value; } }

    function request(url, options) {
        return fetch(url, options || {}).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (body) {
                if (!resp.ok) {
                    throw new Error(body && body.error
                        ? body.error : '请求失败（' + resp.status + '）');
                }
                return body;
            });
        });
    }

    function showError(message) {
        var el = byId('share-error');
        setText(el, message || '');
        show(el, !!message);
    }

    function fatal(message) {
        setText(byId('share-fatal'), message);
        show(byId('share-fatal'), true);
        byId('share-submit').disabled = true;
        show(byId('share-result'), false);
    }

    function renderFiles() {
        var list = byId('share-files');
        list.innerHTML = '';
        state.files.forEach(function (file) {
            var li = document.createElement('li');
            li.className = 'share-file';
            var name = document.createElement('span');
            name.className = 'share-file-name';
            name.textContent = file.name;
            var size = document.createElement('span');
            size.className = 'share-file-size';
            size.textContent = WPCommon.fmtSize(file.size);
            li.appendChild(name);
            li.appendChild(size);
            list.appendChild(li);
        });
        setText(byId('share-count'), state.files.length
            ? state.files.length + ' 个' : '');
        show(byId('share-no-files'), state.files.length === 0);
    }

    function tickExpire() {
        if (state.done || !state.expiresAt) { return; }
        var left = Math.floor(state.expiresAt - Date.now() / 1000);
        if (left <= 0) {
            setText(byId('share-expire'), '已过期');
            state.done = true;
            fatal('这个分享已过期，请重新分享文件。');
            return;
        }
        var minutes = Math.floor(left / 60);
        var seconds = left % 60;
        setText(byId('share-expire'), '链接 ' + minutes + ':'
            + (seconds < 10 ? '0' + seconds : seconds) + ' 后失效');
    }

    function loadNodes() {
        if (state.loadedNodes) { return; }
        state.loadedNodes = true;
        request('/api/share/nodes').then(function (data) {
            var select = byId('share-node');
            select.innerHTML = '';
            (data.nodes || []).forEach(function (node) {
                var opt = document.createElement('option');
                opt.value = node.name;
                opt.textContent = node.label || node.name;
                opt.setAttribute('data-models', JSON.stringify(node.models || []));
                opt.setAttribute('data-default', node.defaultModel || '');
                opt.setAttribute('data-os', node.os || '');
                select.appendChild(opt);
            });
            select.value = data.default || 'local';
            syncNode();
        }).catch(function (e) {
            state.loadedNodes = false;
            showError(e.message);
        });
    }

    // 执行点换人：远端要 cwd，有模型清单才显示模型选择；
    // cwd 占位符跟着执行点平台变（windows → C:\work，linux → /home/me/project）
    function syncNode() {
        var select = byId('share-node');
        var opt = select.options[select.selectedIndex];
        var nodeName = select.value || 'local';
        show(byId('share-cwd-field'), nodeName !== 'local');

        var models = [];
        var def = '';
        var node = null;
        if (opt) {
            try {
                models = JSON.parse(opt.getAttribute('data-models') || '[]');
            } catch (err) {
                models = [];
            }
            def = opt.getAttribute('data-default') || '';
            node = {os: opt.getAttribute('data-os') || ''};
        }
        byId('share-cwd').placeholder = WPCommon.cwdExample(node);
        var modelSelect = byId('share-model');
        modelSelect.innerHTML = '';
        if (models.length) {
            var auto = document.createElement('option');
            auto.value = '';
            auto.textContent = def ? '默认（' + def + '）' : '默认';
            modelSelect.appendChild(auto);
            models.forEach(function (model) {
                var item = document.createElement('option');
                item.value = model;
                item.textContent = model;
                modelSelect.appendChild(item);
            });
            modelSelect.value = '';
        }
        show(byId('share-model-field'), models.length > 0);
    }

    function loadTasks() {
        if (state.loadedTasks) { return; }
        state.loadedTasks = true;
        request('/api/share/tasks').then(function (data) {
            var select = byId('share-task');
            select.innerHTML = '';
            (data.tasks || []).forEach(function (task) {
                var opt = document.createElement('option');
                opt.value = task.id;
                opt.textContent = (task.title || task.id) + ' · ' + task.status;
                select.appendChild(opt);
            });
            var empty = !(data.tasks || []).length;
            show(byId('share-task-empty'), empty);
            select.disabled = empty;
        }).catch(function (e) {
            state.loadedTasks = false;
            showError(e.message);
        });
    }

    function selectWay(way) {
        state.way = way;
        ['new', 'append', 'upload'].forEach(function (name) {
            var btn = byId('way-' + name);
            var active = name === way;
            if (btn) {
                btn.classList.toggle('is-active', active);
                btn.setAttribute('aria-checked', active ? 'true' : 'false');
            }
            show(byId('panel-' + name), active);
        });
        if (way === 'new') { loadNodes(); }
        if (way === 'append') { loadTasks(); }
        if (way === 'upload') { loadTargets(); }
        showError('');
    }

    function nodeLabel(name) {
        var rows = state.nodeRows || [];
        for (var i = 0; i < rows.length; i += 1) {
            if (rows[i].name === (name || 'local')) {
                return rows[i].label || rows[i].name;
            }
        }
        return name || '本机';
    }

    function loadTargets() {
        if (state.loadedTargets) { return; }
        state.loadedTargets = true;
        request('/api/share/targets').then(function (data) {
            state.targets = data.targets || [];
            state.nodeRows = data.nodes || [{name: 'local', label: '本机',
                                             status: 'online'}];
            var select = byId('share-upload-node');
            select.innerHTML = '';
            state.nodeRows.forEach(function (node) {
                var opt = document.createElement('option');
                opt.value = node.name;
                opt.textContent = (node.label || node.name)
                    + (node.status === 'offline' ? '（离线）' : '');
                opt.disabled = node.status === 'offline';
                select.appendChild(opt);
            });
            var def = data['default'] || {};
            select.value = def.node || 'local';
            syncUploadTarget(def.id);
        }).catch(function (e) {
            state.loadedTargets = false;
            showError(e.message);
        });
    }

    // 机器换人：目标下拉只列这台机器声明的；离线项保留但不可选
    function syncUploadTarget(preferredId) {
        var node = byId('share-upload-node').value || 'local';
        var select = byId('share-upload-target');
        select.innerHTML = '';
        var usable = 0;
        (state.targets || []).forEach(function (item) {
            if (item.node !== node) { return; }
            var opt = document.createElement('option');
            opt.value = item.id;
            opt.textContent = item.name
                + (item.status === 'offline' ? '（不可用）' : '');
            opt.disabled = item.status === 'offline';
            select.appendChild(opt);
            if (item.status !== 'offline') { usable += 1; }
        });
        if (preferredId) {
            for (var i = 0; i < select.options.length; i += 1) {
                if (select.options[i].value === preferredId
                        && !select.options[i].disabled) {
                    select.value = preferredId;
                    break;
                }
            }
        }
        show(byId('share-upload-target-empty'), usable === 0);
        select.disabled = usable === 0;
    }

    function buildBody() {
        // 字段规则在 share-form.js（纯函数，tests/js/ 有 node 单测）；这里只收 DOM 值
        var way = state.way;
        return WPShareForm.buildBody({
            ticket: ticket,
            way: way,
            prompt: byId('share-prompt').value,
            node: way === 'upload'
                ? (byId('share-upload-node').value || 'local')
                : (byId('share-node').value || 'local'),
            mode: byId('share-mode').value,
            model: byId('share-model').value || '',
            cwd: byId('share-cwd').value,
            organize: byId('share-organize').checked,
            folderName: byId('share-folder').value,
            taskId: byId('share-task').value,
            target: byId('share-upload-target').value || '',
            subdir: byId('share-subdir').value
        });
    }

    function renderResult(body) {
        var title = byId('share-result-title');
        var text = byId('share-result-text');
        var link = byId('share-task-link');
        if (body.uploaded) {
            var where = body.node ? ('到 ' + nodeLabel(body.node)) : '';
            setText(title, '已上传 ' + body.uploaded.length + ' 个文件' + where);
            setText(text, body.uploaded.map(function (item) {
                return item.path;
            }).join('\n'));
            show(link, false);
        } else {
            setText(title, body.mode === 'appended' ? '已接到任务' : '任务已创建');
            setText(text, '任务号：' + (body.taskId || '')
                + '\n回任务页可以看进度和结果。');
            show(link, true);
        }
        show(byId('share-result'), true);
        byId('share-result').scrollIntoView({behavior: 'smooth', block: 'nearest'});
    }

    function submit() {
        if (state.busy) { return; }
        state.busy = true;
        var button = byId('share-submit');
        button.disabled = true;
        setText(button, '提交中…');
        showError('');
        request('/api/share/submit', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(buildBody())
        }).then(function (body) {
            renderResult(body);
            state.done = true;
            button.hidden = true;
            setText(byId('share-expire'), '已提交');
        }).catch(function (e) {
            showError(e.message);
            button.disabled = false;
            setText(button, '提交');
        }).then(function () {
            state.busy = false;
        });
    }

    function init() {
        ['new', 'append', 'upload'].forEach(function (name) {
            var btn = byId('way-' + name);
            if (btn) {
                btn.addEventListener('click', function () { selectWay(name); });
            }
        });
        byId('share-node').addEventListener('change', syncNode);
        byId('share-upload-node').addEventListener('change', function () {
            syncUploadTarget();
        });
        byId('share-organize').addEventListener('change', function () {
            show(byId('share-folder-field'), byId('share-organize').checked);
        });
        byId('share-submit').addEventListener('click', submit);

        selectWay('new');
        if (!ticket) {
            fatal('这个链接缺少 ticket，请从 App 重新分享文件。');
            return;
        }
        request('/api/share/stage/' + encodeURIComponent(ticket)).then(function (data) {
            state.files = data.files || [];
            state.expiresAt = data.expiresAt || 0;
            renderFiles();
            byId('share-prompt').value = data.prompt || '';
            tickExpire();
            setInterval(tickExpire, 1000);
        }).catch(function (e) {
            fatal(e.message);
        });
    }

    init();
})();