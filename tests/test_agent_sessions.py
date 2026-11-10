# -*- coding: utf-8 -*-
"""智能体持久会话：用真实 bash 子进程做通道桩，验证 shell 语义本身。

反馈 P0-2/P0-3/P1-5/P1-6 的回归防线——这些行为（语法预检隔离、哨兵、
EXIT trap、stdin 隔离、cd 持久化）只有跑真 bash 才测得出来。
"""
import json
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events

TOKEN = 'tok-' + 'b' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()


class LocalBashChannel:
    """真实 bash 子进程，暴露 sessions 模块需要的 paramiko 通道子集。"""

    def __init__(self):
        self.proc = None
        self.bootCmd = None
        self.out = bytearray()
        self.err = bytearray()
        self._readPos = 0
        self._errPos = 0
        self._outDone = False
        self._errDone = False
        self._closedFlag = False
        self._lock = threading.Lock()

    # ---- 通道接口 ----
    def get_transport(self):
        return SimpleNamespace(is_active=lambda: True)

    @property
    def closed(self):
        if self._closedFlag:
            return True
        if self.proc is not None and self.proc.poll() is not None:
            return self._outDone and self._errDone and self._drained()
        return False

    def _drained(self):
        with self._lock:
            return self._readPos >= len(self.out) and self._errPos >= len(self.err)

    def settimeout(self, t):
        pass

    def exec_command(self, cmd):
        self.bootCmd = cmd
        bash = shutil.which('bash')
        self.proc = subprocess.Popen(
            [bash, '-c', cmd], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.out,
                                                  '_outDone'), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.err,
                                                  '_errDone'), daemon=True).start()

    def _pump(self, pipe, buf, doneAttr):
        # read1 而不是 read：read(n) 会等满 n 字节/EOF，bash 的少量输出
        # 永远凑不够，读线程会一直卡住
        while True:
            chunk = pipe.read1(4096)
            if not chunk:
                break
            with self._lock:
                buf.extend(chunk)
        setattr(self, doneAttr, True)

    def sendall(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv_ready(self):
        with self._lock:
            return self._readPos < len(self.out)

    def recv(self, n=65536):
        with self._lock:
            chunk = bytes(self.out[self._readPos:self._readPos + n])
            self._readPos += len(chunk)
            return chunk

    def recv_stderr_ready(self):
        with self._lock:
            return self._errPos < len(self.err)

    def recv_stderr(self, n=65536):
        with self._lock:
            chunk = bytes(self.err[self._errPos:self._errPos + n])
            self._errPos += len(chunk)
            return chunk

    def close(self):
        self._closedFlag = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class FakeClient:
    def __init__(self, chan):
        self.transport = SimpleNamespace(
            is_active=lambda: True, open_session=lambda timeout=10: chan)

    def get_transport(self):
        return self.transport


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(events, 'log',
                        lambda *a, **k: audit.append(a[0] if a else ''))
    import modules.agent_sessions as sessionsMod
    chanBox = []

    def fakeGetConnection(entry):
        chan = LocalBashChannel()
        chanBox.append(chan)
        return FakeClient(chan)
    monkeypatch.setattr(sessionsMod, 'getConnection', fakeGetConnection)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    holder = SimpleNamespace(client=client, audit=audit, chanBox=chanBox)
    return holder


def openSession(env, body=None):
    payload = {'host': 'ai-xmyz'}
    payload.update(body or {})
    resp = env.client.post('/api/agent/ssh/sessions',
                           headers={'X-Agent-Token': TOKEN},
                           data=json.dumps(payload),
                           content_type='application/json')
    return resp, env.chanBox[-1] if env.chanBox else None


def execCmd(env, sid, command, **kw):
    payload = {'command': command}
    payload.update(kw)
    return env.client.post('/api/agent/ssh/sessions/' + sid,
                           headers={'X-Agent-Token': TOKEN},
                           data=json.dumps(payload),
                           content_type='application/json')


def test_open_boot_uses_noexec_bash(env):
    resp, chan = openSession(env, {'cwd': '/tmp'})
    assert resp.status_code == 200
    assert chan.bootCmd.startswith('cd /tmp && ')
    assert 'bash --norc -s' in chan.bootCmd
    assert 'exec' not in chan.bootCmd   # 内层 bash 才是读命令的进程


def test_open_cwd_not_exist_is_400(env):
    resp, _chan = openSession(env, {'cwd': '/definitely/not/exist-9f3e'})
    assert resp.status_code == 400
    assert 'cwd' in resp.get_json()['error']


def test_heredoc_multiline(env):
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    body = execCmd(env, sid, "cat <<'X'\nhello\nworld\nX").get_json()
    assert body['exit'] == 0
    assert 'hello\nworld' in body['out']
    assert '__DONE_' not in body['out'] and '__CHK_' not in body['out']


def test_syntax_error_does_not_wedge_session(env):
    """P0-3：未闭合引号在 eval 内爆掉（rc=2），会话继续可用。"""
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    bad = execCmd(env, sid, "echo 'oops").get_json()
    assert bad['exit'] == 2
    assert 'EOF' in bad['err'] or 'syntax error' in bad['err']
    assert bad['alive'] is True
    good = execCmd(env, sid, 'echo recovered').get_json()
    assert good['exit'] == 0 and 'recovered' in good['out']


def test_stdin_reader_does_not_leak_sentinel(env):
    """P0-2：read x 吃到 EOF 而不是哨兵，输出不泄露实现细节。"""
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    body = execCmd(env, sid, 'read x; echo "got=$x"').get_json()
    assert '__DONE_' not in body['out']
    assert 'printf' not in body['out']
    assert 'got=' in body['out']


def test_cd_persists_and_cwd_reported(env, tmp_path):
    """P2-23：响应的 cwd 跟随会话内 cd，且目录在下一命令真实生效。"""
    workdir = str(tmp_path / 'work').replace('\\', '/')
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    body = execCmd(env, sid, 'mkdir -p "%s" && cd "%s"' % (workdir, workdir)).get_json()
    assert body['exit'] == 0
    assert body['cwd'].replace('\\', '/').endswith('work')
    body = execCmd(env, sid, 'pwd').get_json()
    assert body['out'].strip().endswith('work')


def test_export_persists(env):
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    assert execCmd(env, sid, 'export MYVAR=hello-abc').get_json()['exit'] == 0
    body = execCmd(env, sid, 'echo "v=$MYVAR"').get_json()
    assert 'v=hello-abc' in body['out']


def test_exit_reports_code_then_410(env):
    """P1-5：exit 7 → exit:7 / alive:false；之后调用 410 JSON。"""
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    body = execCmd(env, sid, 'exit 7').get_json()
    assert body['exit'] == 7 and body['alive'] is False
    assert body['reason'] == 'shell exited'
    assert '__SHELL_EXIT_' not in body['out']   # trap 标记不漏给调用方
    resp = execCmd(env, sid, 'echo x')
    assert resp.status_code == 410
    assert 'error' in resp.get_json()   # JSON 而非 HTML


def test_set_e_false_is_confined(env):
    """`|| rc=$?` 列表抑制 errexit：set -e; false 只让本条 rc=1，
    会话存活可继续——失败被 confinement 捕获，不会炸掉会话。"""
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    body = execCmd(env, sid, 'set -e; false').get_json()
    assert body['exit'] == 1
    assert body['alive'] is True
    assert execCmd(env, sid, 'echo fine').get_json()['exit'] == 0


def test_timeout_then_busy_then_recovery(env):
    """P0-2：超时如实上报；旧命令未完时 409 busy；落地后新命令不串旧输出。"""
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    body = execCmd(env, sid, 'echo soon; sleep 4', timeoutSec=1).get_json()
    assert body['timedOut'] is True and body['exit'] is None
    assert body['reason'] == 'timeout'
    assert 'soon' in body['out']
    # 旧命令还在跑（哨兵要等 sleep 4 落地，宽限只有 2s）：busy
    busy = execCmd(env, sid, 'echo mine')
    assert busy.status_code == 409
    assert busy.get_json()['reason'] == 'busy'
    # 等旧命令落地：新命令拿到干净输出
    time.sleep(3.5)
    body = execCmd(env, sid, 'echo mine').get_json()
    assert body['exit'] == 0
    assert 'mine' in body['out'] and 'soon' not in body['out']


def test_timeout_then_stale_output_discarded(env):
    """超时命令迟到的输出整段丢弃，绝不混进下一条命令。"""
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    execCmd(env, sid, 'echo stale-marker; sleep 2', timeoutSec=1)
    time.sleep(2.2)
    body = execCmd(env, sid, 'echo fresh').get_json()
    assert 'fresh' in body['out']
    assert 'stale-marker' not in body['out']


def test_session_list_marks_dead(env):
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    execCmd(env, sid, 'exit 0')
    items = env.client.get('/api/agent/ssh/sessions',
                           headers={'X-Agent-Token': TOKEN}).get_json()['sessions']
    mine = [s for s in items if s['id'] == sid]
    assert mine and mine[0]['alive'] is False


def test_close_session(env):
    resp, _ = openSession(env, {'cwd': '/tmp'})
    sid = resp.get_json()['sessionId']
    resp = env.client.delete('/api/agent/ssh/sessions/' + sid,
                             headers={'X-Agent-Token': TOKEN})
    assert resp.status_code == 200
    assert execCmd(env, sid, 'x').status_code == 404


def test_exec_unknown_session_is_404(env):
    resp = execCmd(env, 'ghost', 'ls')
    assert resp.status_code == 404
    assert 'error' in resp.get_json()
