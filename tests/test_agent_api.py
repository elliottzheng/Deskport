# -*- coding: utf-8 -*-
"""智能体 API：令牌认证、远程 exec、SFTP list/get/put（全部桩化，不碰真实 SSH）。"""
import base64
import hashlib
import json
from types import SimpleNamespace

import paramiko
import pytest
import stat as statMod

import config as configMod
from modules import agent_api, events


TOKEN = 'tok-' + 'a' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """公共夹具: 令牌配置 + 审计捕获 + 装配好的 app/client。"""
    audit = []
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    return SimpleNamespace(client=client, audit=audit, monkeypatch=monkeypatch)


def makeFakeHostsModule(entries, client=None):
    """构造 agent_api.hosts 的替身模块 (findHost / repo.all)。"""
    import types
    repo = SimpleNamespace(all=lambda: entries)
    mod = types.SimpleNamespace(findHost=lambda hostId: next(
        (e for e in entries if e['id'] == hostId), None), repo=repo,
        getConnection=lambda entry: client)
    return mod


def installExecSpy(monkeypatch, calls):
    """替身执行器: 记录调用并返回固定结果。"""
    def fakeExec(entry, command, timeoutSec):
        calls.append({'host': entry['name'], 'command': command,
                      'timeoutSec': timeoutSec})
        return {'exitCode': 0, 'stdout': 'ok-out\n', 'stderr': '',
                'timedOut': False, 'truncated': False}
    monkeypatch.setattr(agent_api, '_execOnHost', fakeExec)


def test_agent_token_rejections(env, monkeypatch):
    """无令牌与错令牌都 401。"""
    installExecSpy(env.monkeypatch, [])
    for headers in ({}, {'X-Agent-Token': 'bad'}):
        resp = env.client.post('/api/agent/ssh/exec', headers=headers,
                               data=json.dumps({'host': 'x', 'command': 'ls'}),
                               content_type='application/json')
        assert resp.status_code == 401


def test_exec_success_by_host_name(env, monkeypatch):
    entries = [{'id': 'h1', 'name': 'ai-xmyz', 'host': '10.0.0.9', 'port': 22}]
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))
    installExecSpy(monkeypatch, calls)

    resp = env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'ai-xmyz', 'command': 'uptime'}),
                           content_type='application/json')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['exit'] == 0 and 'ok-out' in body['out']
    assert calls[0]['command'] == 'uptime'
    assert any(a['action'] == 'agent.exec' for a in env.audit)


def test_exec_rejects_empty_command(env, monkeypatch):
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule([]))
    installExecSpy(env.monkeypatch, [])
    resp = env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'x', 'command': '   '}),
                           content_type='application/json')
    assert resp.status_code == 400


def test_exec_timeout_clamped_to_600(env, monkeypatch):
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))
    installExecSpy(monkeypatch, calls)

    env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                    data=json.dumps({'host': 'h1', 'command': 'ls', 'timeoutSec': 99999}),
                    content_type='application/json')
    assert calls[0]['timeoutSec'] == 600


def test_unknown_host_is_404(env, monkeypatch):
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule([]))
    installExecSpy(env.monkeypatch, [])
    resp = env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'ghost', 'command': 'ls'}),
                           content_type='application/json')
    assert resp.status_code == 404


def test_hosts_listing_has_no_secrets(env, monkeypatch):
    entries = [{'id': 'h1', 'name': 'ai-xmyz', 'host': '10.0.0.9', 'port': 22,
                'privateKey': 'SECRET-KEY', 'password': 'hunter2'}]
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))

    resp = env.client.get('/api/agent/hosts', headers={'X-Agent-Token': TOKEN})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'SECRET-KEY' not in body and 'hunter2' not in body
    assert resp.get_json()['hosts'][0]['name'] == 'ai-xmyz'


# ---- 持久会话: open / exec / close ----

class FakeSessionChannel:
    def __init__(self):
        self.out = bytearray()
        self.err = bytearray()
        self.readPos = 0
        self.errPos = 0
        self.pendingIn = bytearray()
        self.closed = False
        self.bootCmd = None
        self.transport = SimpleNamespace(is_active=lambda: True)

    def get_transport(self):
        return self.transport

    def settimeout(self, t):
        pass

    def exec_command(self, cmd):
        self.bootCmd = cmd

    def sendall(self, data):
        self.pendingIn += data
        while True:
            idx = self.pendingIn.find(b'\n')
            if idx < 0:
                break
            line = bytes(self.pendingIn[:idx])
            self.pendingIn = bytearray(self.pendingIn[idx + 1:])
            self._onLine(line)

    def _onLine(self, line):
        if line.startswith(b'printf "'):
            start = line.find(b'"') + 1
            end = line.find(b'%s', start)
            marker = line[start:end]
            self.out += marker + b'0\n'
        elif line.startswith(b'cd '):
            pass
        else:
            self.out += (b'ran: ' + line.strip() + b'\n')

    def recv_ready(self):
        return self.readPos < len(self.out)

    def recv(self, n=65536):
        chunk = self.out[self.readPos:self.readPos + n]
        self.readPos += len(chunk)
        return chunk

    def recv_stderr_ready(self):
        return self.errPos < len(self.err)

    def recv_stderr(self, n=65536):
        chunk = self.err[self.errPos:self.errPos + n]
        self.errPos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, chan):
        self.transport = SimpleNamespace(
            is_active=lambda: True,
            open_session=lambda timeout=10: chan)

    def get_transport(self):
        return self.transport


def test_session_open_exec_close(tmp_path, monkeypatch):
    entries = [{'id': 'h1', 'name': 'ai-xmyz', 'host': '10.0.0.9', 'port': 22}]
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    chan = FakeSessionChannel()
    import modules.agent_sessions as agentSessionsMod
    monkeypatch.setattr(agentSessionsMod, 'getConnection', lambda e: FakeClient(chan))
    fakeHosts = makeFakeHostsModule(entries, client=FakeClient(chan))
    monkeypatch.setattr(agent_api, 'hosts', fakeHosts)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    H = {'X-Agent-Token': TOKEN}

    resp = client.post('/api/agent/ssh/sessions', headers=H,
                       data=json.dumps({'host': 'ai-xmyz', 'cwd': '/work'}),
                       content_type='application/json')
    assert resp.status_code == 200
    sid = resp.get_json()['sessionId']
    assert chan.bootCmd == 'cd /work && exec bash -s'

    resp = client.post('/api/agent/ssh/sessions/' + sid, headers=H,
                       data=json.dumps({'command': 'echo hi'}),
                       content_type='application/json')
    body = resp.get_json()
    assert body['exit'] == 0
    assert 'ran: echo hi' in body['out']
    assert body['cwd'] == '/work'

    resp = client.delete('/api/agent/ssh/sessions/' + sid, headers=H)
    assert resp.status_code == 200
    resp = client.post('/api/agent/ssh/sessions/' + sid, headers=H,
                       data=json.dumps({'command': 'x'}),
                       content_type='application/json')
    assert resp.status_code == 404   # 已关闭的会话按不存在处理


def test_session_exec_unknown_session_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'tokenHash': TOKEN_HASH}])
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    resp = client.post('/api/agent/ssh/sessions/ghost', headers={'X-Agent-Token': TOKEN},
                       data=json.dumps({'command': 'ls'}),
                       content_type='application/json')
    assert resp.status_code == 404


# ---- SFTP: list / get / put ----

class FakeSftp:
    def __init__(self, files=None):
        self.files = files or {}
        self.dirs = {'/'}
        self.written = {}

    def normalize(self, path):
        return '/' + path.strip('/') if path.strip('/') not in ('', '.') else '/'

    def _attr(self, path):
        attr = paramiko.SFTPAttributes()
        attr.filename = posixname(path)
        attr.st_size = len(self.files.get(path, b''))
        attr.st_mtime = 0
        attr.st_mode = (statMod.S_IFDIR if path in self.dirs else statMod.S_IFREG) | 0o644
        return attr

    def stat(self, path):
        if path in self.dirs or path in self.files:
            return self._attr(path)
        raise IOError('no such file')

    def listdir_attr(self, path):
        import posixpath
        base = self.normalize(path)
        out = []
        for p in sorted(set(self.dirs) | set(self.files)):
            if p != base and posixpath.dirname(p) == base:
                out.append(self._attr(p))
        return out

    class _WriteHandle:
        def __init__(self, store, path):
            self.store, self.path, self.buf = store, path, bytearray()

        def write(self, data):
            self.buf.extend(data)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.store[self.path] = bytes(self.buf)

    class _ReadHandle:
        def __init__(self, data):
            self.data = data

        def read(self, size=-1):
            return self.data

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open(self, path, mode='rb'):
        if 'w' in mode:
            return self._WriteHandle(self.files, path)
        if path not in self.files:
            raise IOError('no such file: ' + path)
        return self._ReadHandle(self.files[path])

    def __repr__(self):
        return '<FakeSftp>'


def posixname(path):
    return path.rstrip('/').rsplit('/', 1)[-1] or path


def installSftp(monkeypatch, fake):
    import modules.ssh_sftp as sshSftpMod
    monkeypatch.setattr(sshSftpMod, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_api, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_api, '_resolveHost',
                        lambda nameOrId: {'id': 'office', 'name': nameOrId})


def test_sftp_list_and_read(env, monkeypatch):
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/report.txt'] = b'report-body'
    installSftp(monkeypatch, fake)

    resp = env.client.post('/api/agent/sftp/list', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office', 'path': '/srv'}),
                           content_type='application/json')
    assert resp.status_code == 200
    entries = resp.get_json()['entries']
    assert {'name': 'report.txt', 'size': 11, 'dir': False} in [
        {'name': e['name'], 'size': e['size'], 'dir': e['dir']} for e in entries]

    resp = env.client.post('/api/agent/sftp/get', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office', 'path': '/srv/report.txt'}),
                           content_type='application/json')
    body = resp.get_json()
    assert body['size'] == 11
    import base64 as b64mod
    assert b64mod.b64decode(body['contentB64']) == b'report-body'
    assert any(a['action'] == 'agent.sftp.get' for a in env.audit)


def test_sftp_put_writes_and_audits(env, monkeypatch):
    fake = FakeSftp()
    fake.dirs.add('/srv')
    import modules.ssh_sftp as sshSftpMod
    monkeypatch.setattr(sshSftpMod, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_api, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_api, '_resolveHost',
                        lambda nameOrId: {'id': 'office', 'name': nameOrId})

    payload = json.dumps({'host': 'office', 'path': '/srv/new.txt',
                          'contentB64': base64.b64encode(b'new-body').decode()})
    resp = env.client.post('/api/agent/sftp/put', headers={'X-Agent-Token': TOKEN},
                           data=payload, content_type='application/json')
    assert resp.status_code == 200
    assert fake.files['/srv/new.txt'] == b'new-body'
    assert any(a['action'] == 'agent.sftp.put' for a in env.audit)


def test_sftp_get_oversize_is_413(env, monkeypatch):
    fake = FakeSftp()
    fake.files['/srv/big.txt'] = b'x' * (agent_api.MAX_FILE_BYTES + 1)
    fake.dirs.add('/srv')
    import modules.ssh_sftp as sshSftpMod
    monkeypatch.setattr(sshSftpMod, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_api, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_api, '_resolveHost',
                        lambda nameOrId: {'id': 'office', 'name': nameOrId})

    resp = env.client.post('/api/agent/sftp/get', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office', 'path': '/srv/big.txt'}),
                           content_type='application/json')
    assert resp.status_code == 413


def test_audit_written_for_exec(env, monkeypatch):
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))
    installExecSpy(monkeypatch, [])
    env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                    data=json.dumps({'host': 'h1', 'command': 'uptime'}),
                    content_type='application/json')
    actions = [a['action'] for a in env.audit]
    assert 'agent.exec' in actions
