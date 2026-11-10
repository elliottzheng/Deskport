# -*- coding: utf-8 -*-
"""本机主机全等价（§7b）：host='local' 的 exec / sessions / sftp / 文件投递。

exec/sessions 用真实 Git Bash 子进程（本机语义只有真跑才测得出）；
PowerShell 用系统自带 powershell.exe。
"""
import base64
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events

TOKEN = 'tok-' + 'd' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(events, 'log', lambda *a, **k: audit.append(a[0] if a else ''))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit, tmp=tmp_path)


def post(env, path, body):
    return env.client.post(path, headers=H, data=json.dumps(body),
                           content_type='application/json')


def postRaw(env, path, body, headers=None):
    return env.client.post(path, headers=headers or H, data=json.dumps(body),
                           content_type='application/json')


# ---- hosts ----

def test_hosts_includes_local(env):
    resp = env.client.get('/api/agent/hosts', headers=H).get_json()
    local = [h for h in resp['hosts'] if h['id'] == 'local']
    assert local and local[0]['os'] == 'windows'
    assert local[0]['defaultShell'] == 'bash'
    assert 'powershell' in local[0]['shells'] and 'pwsh' in local[0]['shells']


def test_hosts_local_hidden_when_disabled(env, monkeypatch):
    import modules.agent_local as localMod
    monkeypatch.setattr(localMod, 'localEnabled', lambda: False)
    resp = env.client.get('/api/agent/hosts', headers=H).get_json()
    assert all(h['id'] != 'local' for h in resp['hosts'])
    assert post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'echo x'}).status_code == 404


# ---- exec ----

def test_local_exec_bash(env):
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'echo hi-local; echo err-1 >&2'}).get_json()
    assert body['exit'] == 0
    assert 'hi-local' in body['out'] and 'err-1' in body['err']
    assert body['pid'] > 0


def test_local_exec_cwd_posix_form(env, tmp_path):
    """路径双写法：/e/... 与 E:\\... 都接受（§7b.2 服务端抹平）。"""
    posix = str(tmp_path).replace('\\', '/')
    posix = '/' + posix[0].lower() + posix[2:]   # E:/x → /e/x
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'pwd', 'cwd': posix}).get_json()
    assert body['exit'] == 0
    assert body['out'].strip().replace('\\', '/').endswith(tmp_path.name)


def test_local_exec_cwd_missing_is_error(env):
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'pwd',
                 'cwd': 'E:\\definitely-not-exist-xyz'}).get_json()
    assert body['exit'] != 0
    assert 'No such file' in body['err'] or '不存在' in body['err']


def test_local_exec_stdin_eof(env):
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'read x; echo "got=$x"'}).get_json()
    assert 'got=' in body['out']          # EOF 立刻回来，不挂死
    assert body['timedOut'] is False


def test_local_exec_heredoc(env):
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': "cat <<'X'\nhello-local\nX"}).get_json()
    assert 'hello-local' in body['out']


def test_local_exec_powershell_exit_code(env):
    """§7b.2 退出码统一：PS 里 $LASTEXITCODE 收敛为进程退出码。"""
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'shell': 'powershell',
                 'command': 'Write-Output ps-hi; cmd /c exit 5'}).get_json()
    assert body['exit'] == 5
    assert 'ps-hi' in body['out']


def test_local_exec_powershell_chinese_output(env):
    """编码统一：PS 输出（含中文）不出现乱码替换符。"""
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'shell': 'powershell',
                 'command': 'Write-Output "中文输出测试"'}).get_json()
    assert '中文输出测试' in body['out']
    assert '\ufffd' not in body['out']


def test_local_exec_shell_whitelist(env):
    resp = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'shell': 'zsh', 'command': 'x'})
    assert resp.status_code == 400
    assert resp.get_json()['shells'] == ['bash', 'powershell', 'pwsh']


def test_local_exec_timeout_kills_tree(env):
    body = post(env, '/api/agent/ssh/exec',
                {'host': 'local', 'command': 'sleep 30',
                 'timeoutSec': 1}).get_json()
    assert body['exit'] is None and body['reason'] == 'timeout'
    time.sleep(0.5)


# ---- sessions ----

def test_local_session_lifecycle(env, tmp_path):
    workdir = str(tmp_path / 'work').replace('\\', '/')
    resp = post(env, '/api/agent/ssh/sessions',
                {'host': 'local', 'cwd': str(tmp_path).replace('\\', '/')})
    assert resp.status_code == 200
    sid = resp.get_json()['sessionId']
    body = post(env, '/api/agent/ssh/sessions/' + sid,
                {'command': 'mkdir -p "%s" && cd "%s" && echo in-work'
                 % (workdir, workdir)}).get_json()
    assert body['exit'] == 0
    assert body['cwd'].replace('\\', '/').endswith('work')   # PWD 实时回报
    body = post(env, '/api/agent/ssh/sessions/' + sid,
                {'command': 'pwd'}).get_json()
    assert body['out'].strip().replace('\\', '/').endswith('work')
    resp = env.client.delete('/api/agent/ssh/sessions/' + sid, headers=H)
    assert resp.status_code == 200


def test_local_session_bad_cwd_400(env):
    resp = post(env, '/api/agent/ssh/sessions',
                {'host': 'local', 'cwd': 'E:\\no-such-dir-xyz'})
    assert resp.status_code == 400


def test_local_session_powershell_unsupported(env):
    resp = post(env, '/api/agent/ssh/sessions',
                {'host': 'local', 'shell': 'pwsh'})
    assert resp.status_code == 400
    assert 'bash' in resp.get_json()['error']


# ---- sftp ----

def test_local_sftp_roundtrip(env, tmp_path):
    target = tmp_path / '本地文件.txt'
    raw = '内容-content'.encode('utf-8')
    b64 = base64.b64encode(raw).decode()
    resp = post(env, '/api/agent/sftp/put',
                {'host': 'local', 'path': str(target), 'contentB64': b64})
    assert resp.status_code == 200
    assert target.read_bytes() == raw
    assert resp.get_json()['sha256'] == hashlib.sha256(raw).hexdigest()

    body = post(env, '/api/agent/sftp/get',
                {'host': 'local', 'path': str(target)}).get_json()
    assert base64.b64decode(body['contentB64']) == raw

    body = post(env, '/api/agent/sftp/list',
                {'host': 'local', 'path': str(tmp_path)}).get_json()
    entry = [e for e in body['entries'] if e['name'] == '本地文件.txt'][0]
    assert entry['type'] == 'file' and entry['dir'] is False and entry['size'] > 0


def test_local_sftp_home_expand_and_errors(env, tmp_path, monkeypatch):
    import modules.agent_local as localMod
    monkeypatch.setattr(localMod.os.path, 'expanduser',
                        lambda p: str(tmp_path) if p == '~' else p)
    body = post(env, '/api/agent/sftp/list', {'host': 'local', 'path': '~'}).get_json()
    assert body['path'] == str(tmp_path)
    assert post(env, '/api/agent/sftp/get',
                {'host': 'local', 'path': str(tmp_path)}).status_code == 400
    assert post(env, '/api/agent/sftp/list',
                {'host': 'local', 'path': str(tmp_path / 'ghost.txt')}).status_code == 404
    assert post(env, '/api/agent/sftp/put',
                {'host': 'local', 'path': str(tmp_path / 'x'),
                 'contentB64': '!!!'}).status_code == 400


# ---- 任务文件投递（§7b 过渡） ----

def test_task_files_upload_then_paths(env):
    raw = b'uploaded-body-123'
    resp = post(env, '/api/tasks/files',
                {'name': '材料.txt', 'contentB64': base64.b64encode(raw).decode()})
    assert resp.status_code == 200
    uploaded = resp.get_json()['path']
    with open(uploaded, 'rb') as f:
        assert f.read() == raw
    # 塞进任务 paths
    resp = post(env, '/api/tasks', {'prompt': '处理这份材料', 'paths': [uploaded]})
    assert resp.status_code == 202
    tid = resp.get_json()['task']['id']
    detail = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()['task']
    assert any('材料.txt' in s['input'] for s in detail['sources'])


def test_task_attachments_inline(env):
    raw = b'inline-attachment'
    resp = post(env, '/api/tasks', {'prompt': 'x', 'attachments': [
        {'name': 'note.md', 'contentB64': base64.b64encode(raw).decode()}]})
    assert resp.status_code == 202
    detail = env.client.get('/api/tasks/%s' % resp.get_json()['task']['id'],
                            headers=H).get_json()['task']
    assert any('note.md' in s['input'] and s['source'] == '(智能体上传)'
               for s in detail['sources'])


def test_task_attachment_bad_base64_rejected(env):
    resp = post(env, '/api/tasks', {'prompt': 'x', 'attachments': [
        {'name': 'a.txt', 'contentB64': '!!!'}]})
    assert resp.status_code == 400
    resp = post(env, '/api/tasks', {'prompt': 'x', 'attachments': [
        {'name': '../evil.txt', 'contentB64': base64.b64encode(b'x').decode()}]})
    assert resp.status_code == 400
