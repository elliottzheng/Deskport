// SSH 模块前端 — 主机/凭据管理 + 认证提示 + SFTP 面板
// 依赖: term-features.js 导出的 createSshSession(hostId, label) 与 xterm 页面结构
const SSHUI = (function () {
    'use strict';

    let hosts = [];

    let authSession = null;

    const $ = SSHUiCtx.$;
    const api = SSHUiCtx.api;
    SSHUiCtx.getHosts = function () { return SSHHostStore.hosts; };
    const escapeHtml = WPCommon.escapeHtml;

    function init() {
        $('ssh-host-add').addEventListener('click', function () { openHostForm(); });
        $('ssh-host-save').addEventListener('click', saveHost);
        $('ssh-host-delete').addEventListener('click', deleteHost);
        $('ssh-host-cancel').addEventListener('click', () => { $('ssh-host-modal').style.display = 'none'; });
        $('ssh-f-authtype').addEventListener('change', function () {
            $('ssh-f-passwrap').style.display = this.value === 'key' ? 'none' : '';
            $('ssh-f-keywrap').style.display = this.value === 'key' ? '' : 'none';
            $('ssh-f-passphrasewrap').style.display = this.value === 'key' ? '' : 'none';
        });
        $('ssh-auth-submit').addEventListener('click', submitAuthAnswers);
        SSHSftpUI.bind();
        renderHosts().catch(function (err) { console.error('主机列表加载失败:', err); });
    }


    // ------------------------------------------------------------------
    // 主机列表 / 表单
    // ------------------------------------------------------------------
    async function renderHosts() {
        const data = await api('/api/ssh/hosts');
        hosts = data.hosts || [];
        SSHHostStore.setHosts(hosts);
        const box = $('ssh-hosts');
        box.innerHTML = '';
        hosts.forEach(h => {
            const btn = document.createElement('button');
            btn.className = 'ssh-host';
            btn.innerHTML =
                Icons.svg('terminal') +
                '<span class="host-name">' + escapeHtml(h.name) + '</span>' +
                (h.favorite ? '<span class="host-fav">' + Icons.svg('star') + '</span>' : '');
            btn.addEventListener('click', () => connectHost(h));
            btn.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                editHost(h);
            });
            box.appendChild(btn);
        });
        if (!hosts.length) {
            const tip = document.createElement('div');
            tip.className = 'empty-tip';
            tip.style.padding = '8px 4px';
            tip.textContent = '右键主机可编辑';
            box.appendChild(tip);
        }
        renderSnippets();
    }

    // ---- Snippets (localStorage, 点击发送到前台 SSH 会话) ----
    function getSnippets() {
        try { return JSON.parse(localStorage.getItem('sshSnippets') || '[]'); }
        catch (err) { return []; }
    }

    function saveSnippets(list) {
        localStorage.setItem('sshSnippets', JSON.stringify(list));
    }

    function renderSnippets() {
        const box = $('snippets');
        if (!box) { return; }
        box.innerHTML = '';
        getSnippets().forEach(sn => {
            const btn = document.createElement('button');
            btn.className = 'ssh-host';
            btn.title = sn.command;
            btn.innerHTML = '<span class="host-name">' + escapeHtml(sn.name) + '</span>' +
                '<span class="sftp-row-meta">' + escapeHtml(sn.command.slice(0, 20)) + '</span>';
            btn.addEventListener('click', () => sendSnippet(sn.command));
            btn.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                saveSnippets(getSnippets().filter(x => x !== sn));
                renderSnippets();
            });
            box.appendChild(btn);
        });
        if (!getSnippets().length) {
            const tip = document.createElement('div');
            tip.className = 'empty-tip';
            tip.style.padding = '6px 4px';
            tip.textContent = '下方输入保存常用命令';
            box.appendChild(tip);
        }
    }

    function sendSnippet(command) {
        const s = window.termManager && window.termManager.getActive();
        if (!s || s.kind !== 'ssh' || !s.ws || s.ws.readyState !== 1) {
            alert('请先连接一个 SSH 会话');
            return;
        }
        s.ws.send(command + '\r');
    }

    async function deleteHost() {
        const id = $('ssh-host-id').value;
        if (!id) { return; }
        const h = hosts.find(x => x.id === id);
        if (!window.confirm('确定删除连接「' + (h ? h.name : id) + '」？活跃连接将被断开。')) { return; }
        try {
            await api('/api/ssh/hosts/' + id, { method: 'DELETE' });
            $('ssh-host-modal').style.display = 'none';
            await renderHosts();
        } catch (err) {
            $('ssh-host-error').textContent = err.message;
            $('ssh-host-error').style.display = '';
        }
    }

    // snippets 表单绑定 (一次性)
    document.addEventListener('DOMContentLoaded', () => {
        const nameEl = $('snippet-name');
        const cmdEl = $('snippet-cmd');
        if (!nameEl) { return; }
        $('snippet-add').addEventListener('click', () => {
            const name = nameEl.value.trim();
            const command = cmdEl.value.trim();
            if (!name || !command) { return; }
            const list = getSnippets();
            list.push({ name: name, command: command });
            saveSnippets(list);
            nameEl.value = '';
            cmdEl.value = '';
            renderSnippets();
        });
        renderSnippets();
    });

    function connectHost(h) {
        // TOFU 预检: 首次连接展示指纹确认, 密钥变更强告警
        fetch('/api/ssh/hostkey/check?hostId=' + encodeURIComponent(h.id))
            .then(r => r.json())
            .then(data => {
                if (data.status === 'known') {
                    createSshSession(h.id, h.name);
                    setSidebar(true);
                    return;
                }
                const changed = data.status === 'changed';
                const msg = (changed
                    ? '警告：主机密钥与之前不符！\n可能存在中间人攻击。\n\n'
                    : '首次连接该主机。\n\n') +
                    '主机: ' + data.host + ':' + data.port + '\n' +
                    '指纹: ' + data.fingerprint + '\n\n' +
                    (changed ? '确认无中间人攻击并继续？(将更新已存指纹)' : '信任并保存该指纹？');
                if (window.confirm(msg)) {
                    api('/api/ssh/hostkey/trust', {
                        method: 'POST',
                        body: JSON.stringify({ host: data.host, port: data.port,
                                               fingerprint: data.fingerprint }),
                    }).then(() => {
                        createSshSession(h.id, h.name);
                        setSidebar(true);
                    });
                }
            })
            .catch(err => alert('主机密钥检查失败: ' + err.message));
    }

    function setSidebar(open) {
        // 侧栏开合的唯一实现在 term-features (含 localStorage 记忆); 此处委托
        if (window.termUI) { window.termUI.setSidebar(open); return; }
        const sidebar = document.getElementById('sidebar');
        if (sidebar) { sidebar.classList.toggle('open', open); }
    }

    function editHost(h) {
        openHostForm(h);
    }

    function openHostForm(h) {
        $('ssh-host-form-title').textContent = h ? '编辑连接 — ' + h.name : '新建连接';
        $('ssh-host-id').value = h ? h.id : '';
        $('ssh-f-name').value = h ? h.name : '';
        $('ssh-f-host').value = h ? h.host : '';
        $('ssh-f-port').value = h ? h.port : 22;
        $('ssh-f-user').value = h ? h.user : 'root';
        $('ssh-f-authtype').value = h ? h.authType : 'password';
        $('ssh-f-authtype').dispatchEvent(new Event('change'));
        $('ssh-f-password').value = '';
        $('ssh-f-key').value = '';
        $('ssh-f-passphrase').value = '';
        $('ssh-f-group').value = h ? (h.group || '') : '';
        $('ssh-f-notes').value = h ? (h.notes || '') : '';
        $('ssh-f-favorite').checked = h ? !!h.favorite : false;
        // 删除入口仅编辑态提供 (新建无 id 可删)
        $('ssh-host-delete').style.display = h ? '' : 'none';
        $('ssh-host-error').style.display = 'none';
        $('ssh-host-modal').style.display = 'flex';
    }

    async function saveHost() {
        const payload = {
            id: $('ssh-host-id').value || undefined,
            name: $('ssh-f-name').value,
            host: $('ssh-f-host').value,
            port: $('ssh-f-port').value,
            user: $('ssh-f-user').value,
            authType: $('ssh-f-authtype').value,
            password: $('ssh-f-password').value || undefined,
            privateKey: $('ssh-f-key').value || undefined,
            passphrase: $('ssh-f-passphrase').value || undefined,
            group: $('ssh-f-group').value,
            notes: $('ssh-f-notes').value,
            favorite: $('ssh-f-favorite').checked,
        };
        try {
            const data = await api('/api/ssh/hosts', {
                method: 'POST', body: JSON.stringify(payload),
            });
            $('ssh-host-modal').style.display = 'none';
            await renderHosts();
            // 保存并连接
            createSshSession(data.id, payload.name);
            setSidebar(false);
        } catch (err) {
            $('ssh-host-error').textContent = err.message;
            $('ssh-host-error').style.display = '';
        }
    }

    // ------------------------------------------------------------------
    // keyboard-interactive 认证提示
    // ------------------------------------------------------------------
    // 会话中途密钥变更告警 (WS 帧 ssh-hostkey-changed; 核心已停止自动重连)。
    // 帧只带 msg 文本, 新指纹经 hostkey/check 端点取回后再显式 trust
    function showHostKeyChanged(session, obj) {
        const h = hosts.find(x => x.id === (session && session.hostId));
        const name = h ? (h.host + ':' + (h.port || 22)) : ((session && session.label) || '主机');
        if (!window.confirm('安全告警：' + name + '\n' + ((obj && obj.msg) || '主机密钥与已存指纹不符') +
            '\n可能存在中间人攻击，已停止自动重连。\n\n确认无误并重新信任该主机当前指纹？')) {
            return;
        }
        if (!h) { alert('未找到主机条目，无法更新指纹'); return; }
        fetch('/api/ssh/hostkey/check?hostId=' + encodeURIComponent(h.id))
            .then(r => r.json())
            .then(data => api('/api/ssh/hostkey/trust', {
                method: 'POST',
                body: JSON.stringify({ host: data.host, port: data.port, fingerprint: data.fingerprint }),
            }))
            .then(() => alert('已更新指纹，可从主机列表重新连接'))
            .catch(err => alert('更新指纹失败: ' + err.message));
    }

    function showAuthPrompt(session, frame) {
        authSession = session;
        $('ssh-auth-title').textContent = frame.title || 'SSH 认证';
        $('ssh-auth-instructions').textContent = frame.instructions || '';
        const box = $('ssh-auth-prompts');
        box.innerHTML = '';
        (frame.prompts || []).forEach((p, i) => {
            const label = document.createElement('label');
            label.className = 'field-label';
            label.textContent = p.text;
            box.appendChild(label);
            const input = document.createElement('input');
            input.className = 'input';
            input.style.width = '100%';
            input.dataset.idx = i;
            if (!p.echo) { input.type = 'password'; }
            box.appendChild(input);
        });
        $('ssh-auth-modal').style.display = 'flex';
        const first = box.querySelector('input');
        if (first) { first.focus(); }
    }

    function submitAuthAnswers() {
        if (!authSession) { return; }
        const answers = Array.from($('ssh-auth-prompts').querySelectorAll('input'))
            .map(el => el.value);
        authSession.ws.send(JSON.stringify({ type: 'ssh-auth-answer', answers: answers }));
        $('ssh-auth-modal').style.display = 'none';
        authSession = null;
    }

    // ------------------------------------------------------------------
    // SFTP 面板
    // ------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', init);
    return {
        showAuthPrompt: showAuthPrompt,
        showHostKeyChanged: showHostKeyChanged,
        openHostForm: function () { openHostForm(null); },
        refresh: renderHosts,
    };
})();

// 显式挂载: term-client/ssh-page 经 window.SSHUI 调用认证与密钥告警钩子
window.SSHUI = SSHUI;
