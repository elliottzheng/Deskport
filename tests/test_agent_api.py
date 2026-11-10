# -*- coding: utf-8 -*-
"""智能体 API：令牌认证、远程 exec、SFTP list/get/put（全部桩化，不碰真实 SSH）。"""
import base64
import hashlib
import json
import re
from types import SimpleNamespace

import paramiko
import pytest
import stat as statMod

import config as configMod
from modules import agent_api, agent_sftp, events


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
    def fakeExec(entry, command, timeoutSec, cwd=''):
        calls.append({'host': entry['name'], 'command': command,
                      'timeoutSec': timeoutSec, 'cwd': cwd})
        return {'exit': 0, 'out': b'ok-out\n', 'err': b'',
                'timedOut': False, 'reason': '', 'pid': None, 'truncated': False}
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


def test_exec_timeout_out_of_range_is_400(env, monkeypatch):
    """timeoutSec 不再静默夹紧：0 / 601 / 'abc' 一律 400（反馈 §3.9）。"""
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))
    installExecSpy(monkeypatch, calls)
    H = {'X-Agent-Token': TOKEN}
    for bad in (0, -5, 601, 'abc', '1.5'):
        resp = env.client.post('/api/agent/ssh/exec', headers=H,
                               data=json.dumps({'host': 'h1', 'command': 'ls',
                                                'timeoutSec': bad}),
                               content_type='application/json')
        assert resp.status_code == 400, bad
    assert calls == []   # 校验失败不碰执行器
    # 合法值原样通过
    env.client.post('/api/agent/ssh/exec', headers=H,
                    data=json.dumps({'host': 'h1', 'command': 'ls',
                                     'timeoutSec': '600'}),
                    content_type='application/json')
    assert calls[0]['timeoutSec'] == 600


def test_exec_rejects_nul_command(env, monkeypatch):
    """命令含 NUL 时 400，而不是 paramiko 炸 500（反馈 §3.9）。"""
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule([]))
    installExecSpy(env.monkeypatch, [])
    resp = env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'x', 'command': 'echo a\x00b'}),
                           content_type='application/json')
    assert resp.status_code == 400
    assert 'NUL' in resp.get_json()['error']


def test_exec_cwd_goes_to_wrapper_not_command(env, monkeypatch):
    """cwd 由外壳处理，用户命令原文透传（heredoc 不再被破坏，反馈 §3.9）。"""
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    calls = []
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))
    installExecSpy(monkeypatch, calls)
    heredoc = "cat <<'X'\nhello\nX"
    env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                    data=json.dumps({'host': 'h1', 'command': heredoc,
                                     'cwd': '/tmp'}),
                    content_type='application/json')
    assert calls[0]['cwd'] == '/tmp'
    assert calls[0]['command'] == heredoc   # 命令本体零改动


def test_exec_wraps_script_without_touching_command():
    """外壳是固定语法：命令只以 base64 出现在 eval 里，heredoc/引号不影响外壳。"""
    script = agent_api._buildExecScript("cat <<'X'\nhello\nX", '/tmp/work')
    assert script.endswith('fi')
    assert 'cd -- /tmp/work || exit' in script
    assert 'setsid -w bash -c' in script
    assert 'cat <<' not in script      # 用户命令不以明文进外壳语法层
    # 解码回原命令，验证逐字一致
    m = re.search(r'eval "\$\(printf %s ([A-Za-z0-9+/=]+) \| base64 -d\)"', script)
    assert m, '找不到 base64 传递点'
    assert base64.b64decode(m.group(1)).decode('utf-8') == "cat <<'X'\nhello\nX"


def test_exec_reports_timeout_and_pid(env, monkeypatch):
    """超时：exit=null + reason=timeout + pid（反馈 §2.2/§3.9 的 exit:-1 歧义）。"""
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))

    def fakeExec(entry, command, timeoutSec, cwd=''):
        return {'exit': None, 'out': b'__PID__4321\npartial', 'err': b'',
                'timedOut': True, 'reason': 'timeout', 'pid': 4321,
                'truncated': False}
    monkeypatch.setattr(agent_api, '_execOnHost', fakeExec)
    resp = env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'x', 'command': 'sleep 9',
                                            'timeoutSec': 1}),
                           content_type='application/json')
    body = resp.get_json()
    assert resp.status_code == 200
    assert body['exit'] is None and body['timedOut'] is True
    assert body['reason'] == 'timeout' and body['pid'] == 4321


def test_exec_encoding_base64(env, monkeypatch):
    """encoding=base64：非 UTF-8 字节逐字返回，不丢内容（反馈 §4）。"""
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    monkeypatch.setattr(agent_api, 'hosts', makeFakeHostsModule(entries))

    def fakeExec(entry, command, timeoutSec, cwd=''):
        return {'exit': 0, 'out': b'\xd6\xd0', 'err': b'\xa7\xe2',
                'timedOut': False, 'reason': '', 'pid': None, 'truncated': False}
    monkeypatch.setattr(agent_api, '_execOnHost', fakeExec)
    resp = env.client.post('/api/agent/ssh/exec', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'x', 'command': 'x',
                                            'encoding': 'base64'}),
                           content_type='application/json')
    body = resp.get_json()
    assert base64.b64decode(body['outB64']) == b'\xd6\xd0'
    assert base64.b64decode(body['errB64']) == b'\xa7\xe2'
    assert 'out' not in body   # 二进制模式不给文本字段


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


# ---- 持久会话: 测试迁移至 tests/test_agent_sessions.py（真实 bash 通道桩） ----


# ---- SFTP: list / get / put ----

class FakeSftp:
    def __init__(self, files=None):
        self.files = files or {}
        self.dirs = {'/'}
        self.symlinks = {}      # path -> target
        self.written = {}
        self.renamed = []
        self.chmodCalls = []

    def normalize(self, path):
        p = path if path.startswith('/') else '/' + path
        if p == '/~':
            return '/home/user'
        if p.startswith('/~/'):
            return '/home/user/' + p[3:]
        return '/' + p.strip('/') if p.strip('/') else '/'

    def _attr(self, path):
        attr = paramiko.SFTPAttributes()
        attr.filename = posixname(path)
        attr.st_size = len(self.files.get(path, b''))
        attr.st_mtime = 0
        if path in self.symlinks:
            mode = statMod.S_IFLNK
        elif path in self.dirs:
            mode = statMod.S_IFDIR
        else:
            mode = statMod.S_IFREG
        attr.st_mode = mode | 0o644
        return attr

    def stat(self, path):
        if path in self.symlinks:
            target = self.symlinks[path]
            return self.stat(target)
        if path in self.dirs or path in self.files:
            return self._attr(path)
        raise IOError(errnoErr('missing'))

    def lstat(self, path):
        if path in self.dirs or path in self.files or path in self.symlinks:
            return self._attr(path)
        raise IOError(errnoErr('missing'))

    def readlink(self, path):
        return self.symlinks.get(path, '')

    def listdir_attr(self, path):
        import posixpath
        base = self.normalize(path)
        out = []
        for p in sorted(set(self.dirs) | set(self.files) | set(self.symlinks)):
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
            self.pos = 0

        def seek(self, offset):
            self.pos = offset

        def read(self, size=-1):
            if size < 0:
                chunk = self.data[self.pos:]
            else:
                chunk = self.data[self.pos:self.pos + size]
            self.pos += len(chunk)
            return chunk

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
            raise IOError(errnoErr('missing'))
        return self._ReadHandle(self.files[path])

    def posix_rename(self, old, new):
        if old not in self.files:
            raise IOError(errnoErr('missing'))
        self.files[new] = self.files.pop(old)
        self.renamed.append((old, new))

    def chmod(self, path, mode):
        self.chmodCalls.append((path, mode))

    def remove(self, path):
        self.files.pop(path, None)

    def __repr__(self):
        return '<FakeSftp>'


def errnoErr(kind):
    """构造带 errno 的 IOError，验证错误码映射。"""
    import errno as errnoMod
    num = errnoMod.EACCES if kind == 'denied' else errnoMod.ENOENT
    return IOError(num, 'Permission denied' if kind == 'denied' else 'No such file')


def posixname(path):
    return path.rstrip('/').rsplit('/', 1)[-1] or path


def installSftp(monkeypatch, fake):
    import modules.ssh_sftp as sshSftpMod
    monkeypatch.setattr(sshSftpMod, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_sftp, '_openSftp', lambda hostId: fake)
    monkeypatch.setattr(agent_sftp, '_resolveHost',
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
    installSftp(monkeypatch, fake)

    payload = json.dumps({'host': 'office', 'path': '/srv/new.txt',
                          'contentB64': base64.b64encode(b'new-body').decode()})
    resp = env.client.post('/api/agent/sftp/put', headers={'X-Agent-Token': TOKEN},
                           data=payload, content_type='application/json')
    assert resp.status_code == 200
    assert fake.files['/srv/new.txt'] == b'new-body'
    assert any(a['action'] == 'agent.sftp.put' for a in env.audit)


def test_sftp_get_oversize_is_413(env, monkeypatch):
    fake = FakeSftp()
    fake.files['/srv/big.txt'] = b'x' * (agent_sftp.MAX_FILE_BYTES + 1)
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)

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


# ---- 反馈专项: SFTP 语义（§2.1 / §3.10 / §4）----

def test_sftp_put_rejects_bad_base64_without_touching_file(env, monkeypatch):
    """P0-1：坏 base64 必须 400，且绝不写空文件覆盖原内容。"""
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/keep.txt'] = b'original-body'
    installSftp(monkeypatch, fake)
    H = {'X-Agent-Token': TOKEN}
    for bad in ('!!!', '', 'ab@cd', 'a'):
        resp = env.client.post('/api/agent/sftp/put', headers=H,
                               data=json.dumps({'host': 'office',
                                                'path': '/srv/keep.txt',
                                                'contentB64': bad}),
                               content_type='application/json')
        assert resp.status_code == 400, bad
    assert fake.files['/srv/keep.txt'] == b'original-body'


def test_sftp_put_strips_whitespace_and_verifies_sha256(env, monkeypatch):
    """带换行的合法 base64 可写入（智能体常按 76 列折行）；sha256 可选校验。"""
    fake = FakeSftp()
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)
    raw = b'body' * 100
    b64 = base64.b64encode(raw).decode()
    folded = '\n'.join(b64[i:i + 76] for i in range(0, len(b64), 76))
    resp = env.client.post('/api/agent/sftp/put', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/a.bin',
                                            'contentB64': folded}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert fake.files['/srv/a.bin'] == raw
    assert resp.get_json()['sha256'] == hashlib.sha256(raw).hexdigest()
    # 摘要不符 → 400
    resp = env.client.post('/api/agent/sftp/put', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/a.bin',
                                            'contentB64': base64.b64encode(b'x').decode(),
                                            'sha256': '0' * 64}),
                           content_type='application/json')
    assert resp.status_code == 400


def test_sftp_put_is_atomic_and_preserves_mode(env, monkeypatch):
    """写入走临时文件+rename，覆盖时保留原权限（§5 保留项不能丢）。"""
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/x.sh'] = b'old'
    installSftp(monkeypatch, fake)
    fake._attr = fake._attr   # noop
    # 直接造一个 755 的属性：stat 返回的 mode 用 lstat 分支
    realStat = fake.stat

    def stat755(path):
        attr = realStat(path)
        if path == '/srv/x.sh':
            attr.st_mode = statMod.S_IFREG | 0o755
        return attr
    fake.stat = stat755
    resp = env.client.post('/api/agent/sftp/put', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office', 'path': '/srv/x.sh',
                                            'contentB64': base64.b64encode(b'new').decode()}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert fake.files['/srv/x.sh'] == b'new'
    assert fake.renamed, '应当经临时文件 rename 原子落盘'
    assert fake.chmodCalls and fake.chmodCalls[-1][1] == 0o755


def test_sftp_get_dir_400_and_list_file_400(env, monkeypatch):
    """§3.10：get 目录 400；list 文件 400（不再冒充 404 No such file）。"""
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/file.txt'] = b'x'
    installSftp(monkeypatch, fake)
    H = {'X-Agent-Token': TOKEN}
    resp = env.client.post('/api/agent/sftp/get', headers=H,
                           data=json.dumps({'host': 'office', 'path': '/srv'}),
                           content_type='application/json')
    assert resp.status_code == 400 and '目录' in resp.get_json()['error']
    resp = env.client.post('/api/agent/sftp/list', headers=H,
                           data=json.dumps({'host': 'office', 'path': '/srv/file.txt'}),
                           content_type='application/json')
    assert resp.status_code == 400 and '不是目录' in resp.get_json()['error']


def test_sftp_permission_is_403(env, monkeypatch):
    """§3.10：无权限 403，不再误导为不存在。"""
    fake = FakeSftp()
    installSftp(monkeypatch, fake)

    def denied(path):
        raise errnoErr('denied')
    fake.stat = denied
    resp = env.client.post('/api/agent/sftp/list', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office', 'path': '/root'}),
                           content_type='application/json')
    assert resp.status_code == 403


def test_sftp_list_types_and_home_normalize(env, monkeypatch):
    """§4：type/mode/target 字段；~ 解析；symlink 的 dir 跟随目标。"""
    fake = FakeSftp()
    fake.dirs.update({'/', '/home', '/home/user', '/srv'})
    fake.files['/srv/notes.txt'] = b'hi'
    fake.symlinks['/srv/ln-notes'] = '/srv/notes.txt'
    installSftp(monkeypatch, fake)
    H = {'X-Agent-Token': TOKEN}
    resp = env.client.post('/api/agent/sftp/list', headers=H,
                           data=json.dumps({'host': 'office', 'path': '~'}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert resp.get_json()['path'] == '/home/user'   # ~ 展开为绝对路径
    resp = env.client.post('/api/agent/sftp/list', headers=H,
                           data=json.dumps({'host': 'office', 'path': '/srv'}),
                           content_type='application/json')
    entries = {e['name']: e for e in resp.get_json()['entries']}
    assert entries['notes.txt']['type'] == 'file'
    assert entries['notes.txt']['mode'] == '0644'
    lnk = entries['ln-notes']
    assert lnk['type'] == 'symlink' and lnk['target'] == '/srv/notes.txt'
    assert lnk['dir'] is False and lnk['size'] == 2   # 跟随目标属性


def test_sftp_get_chunked_offset_length(env, monkeypatch):
    """§4：offset/length 分块读可用；超限响应带 total/chunkLimit 提示分块。"""
    fake = FakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/big.bin'] = bytes(range(256)) * 40   # 10240 bytes
    installSftp(monkeypatch, fake)
    H = {'X-Agent-Token': TOKEN}
    # 收紧整读上限到 4KB，模拟大文件：整读 413 且带分块提示
    monkeypatch.setattr(agent_sftp, 'MAX_FILE_BYTES', 4096)
    resp = env.client.post('/api/agent/sftp/get', headers=H,
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/big.bin'}),
                           content_type='application/json')
    assert resp.status_code == 413
    body = resp.get_json()
    assert body['total'] == 10240 and body['chunkLimit'] == 4096
    # 分块读（length 上限校验用同一收紧值）
    resp = env.client.post('/api/agent/sftp/get', headers=H,
                           data=json.dumps({'host': 'office', 'path': '/srv/big.bin',
                                            'offset': 100, 'length': 50}),
                           content_type='application/json')
    body = resp.get_json()
    assert resp.status_code == 200
    assert body['offset'] == 100 and body['size'] == 50 and body['total'] == 10240
    assert base64.b64decode(body['contentB64']) == fake.files['/srv/big.bin'][100:150]
    # 从 offset 读到底（不带 length）
    resp = env.client.post('/api/agent/sftp/get', headers=H,
                           data=json.dumps({'host': 'office', 'path': '/srv/big.bin',
                                            'offset': 10200}),
                           content_type='application/json')
    body = resp.get_json()
    assert base64.b64decode(body['contentB64']) == fake.files['/srv/big.bin'][10200:]
    assert body['offset'] == 10200


def test_sftp_put_rejects_oversized_body_early(env, monkeypatch):
    """§4：Content-Length 超限时收到请求即拒，不等整个上传。"""
    fake = FakeSftp()
    installSftp(monkeypatch, fake)
    big = base64.b64encode(b'x' * (agent_sftp.MAX_FILE_BYTES + 1024)).decode()
    resp = env.client.post('/api/agent/sftp/put', headers={'X-Agent-Token': TOKEN},
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/big.bin',
                                            'contentB64': big}),
                           content_type='application/json')
    assert resp.status_code == 413
    assert '/srv/big.bin' not in fake.files
