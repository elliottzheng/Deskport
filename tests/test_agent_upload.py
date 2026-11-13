# -*- coding: utf-8 -*-
"""手机分享上传 + 令牌 scope。

覆盖: scope 越权 403（upload 令牌调 exec / tasks）、旧令牌（无 scopes）
不受影响、上传成功/多文件/重名 (2)/坏 subdir 400/超限 413/无令牌 401、
phoneUploadDir 并入收件箱（上传出现在 /api/inbox/recent）、审计记令牌名
与 scope。全部桩化，不碰真实主机与真实配置。
"""
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_api, events

TOKEN_FULL = 'tok-full-' + 'a' * 16          # 旧式令牌: 没写 scopes = 全权
TOKEN_UPLOAD = 'tok-up-' + 'b' * 16          # 手机令牌: scopes ["upload"]
TOKEN_EXPLICIT_FULL = 'tok-exp-' + 'c' * 16  # 显式 scopes ["full"]


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """三种令牌 + 1MB 上传上限（压低便于测 413）+ 审计捕获 + 装配好的 client。

    statePath 指到临时目录: 任务守卫用例不得读写真实 %APPDATA% 状态
    （prod 的 agent_tasks.json 正在被占用）。"""
    audit = []

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'full', 'name': '全权智能体', 'tokenHash': _hash(TOKEN_FULL)},
        {'id': 'phone', 'name': '手机', 'tokenHash': _hash(TOKEN_UPLOAD),
         'scopes': ['upload']},
        {'id': 'expfull', 'name': '显式full', 'tokenHash': _hash(TOKEN_EXPLICIT_FULL),
         'scopes': ['full']},
    ])
    monkeypatch.setattr(configMod, 'getPhoneUploadDir',
                        lambda: str(tmp_path / '手机上传'))
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 1)
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit, tmp=tmp_path,
                           monkeypatch=monkeypatch)


def postUpload(client, token, parts, subdir=None):
    """parts: bytes（名 a.txt）或 [(name, bytes)]；file 字段可重复。"""
    if isinstance(parts, bytes):
        parts = [('a.txt', parts)]
    data = {'file': [(io.BytesIO(raw), name) for name, raw in parts]}
    if subdir is not None:
        data['subdir'] = subdir
    headers = {} if token is None else {'X-Agent-Token': token}
    return client.post('/api/agent/upload', data=data, headers=headers,
                       content_type='multipart/form-data')


# ---- scope: 越权 403 / 旧令牌不受影响 ----

def test_no_token_and_bad_token_401(env):
    assert postUpload(env.client, None, b'x').status_code == 401
    assert postUpload(env.client, 'wrong' + '0' * 20, b'x').status_code == 401


def test_upload_token_exec_is_403(env):
    """评估 §1 的核心场景: 手机令牌不能远程执行。"""
    resp = env.client.post('/api/agent/ssh/exec',
                           headers={'X-Agent-Token': TOKEN_UPLOAD},
                           data=json.dumps({'host': 'local', 'command': 'ls'}),
                           content_type='application/json')
    assert resp.status_code == 403
    body = resp.get_json()
    assert body['error'] == '令牌无此权限' and body['need'] == 'full'
    denies = [a for a in env.audit if a['action'] == 'agent.scope']
    assert denies and denies[-1]['target'] == '手机'
    assert 'need=full' in denies[-1]['detail'] and denies[-1]['result'] == 'deny'


def test_upload_token_tasks_entry_is_403(env):
    """/api/tasks* 入口统一要求 full：建任务与读任务都被拒。"""
    resp = env.client.post('/api/tasks', headers={'X-Agent-Token': TOKEN_UPLOAD},
                           data=json.dumps({'prompt': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 403 and resp.get_json()['need'] == 'full'
    resp = env.client.get('/api/tasks/no-such-task',
                          headers={'X-Agent-Token': TOKEN_UPLOAD})
    assert resp.status_code == 403


def test_upload_token_sftp_is_403(env):
    resp = env.client.post('/api/agent/sftp/list',
                           headers={'X-Agent-Token': TOKEN_UPLOAD},
                           data=json.dumps({'host': 'local', 'path': '/'}),
                           content_type='application/json')
    assert resp.status_code == 403 and resp.get_json()['need'] == 'full'


def test_legacy_token_unaffected(env, monkeypatch):
    """没写 scopes 的旧令牌 = 全权：exec / tasks / upload 都放行（向后兼容）。"""
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    repo = SimpleNamespace(all=lambda: entries)
    monkeypatch.setattr(agent_api, 'hosts', SimpleNamespace(
        findHost=lambda hostId: next((e for e in entries if e['id'] == hostId), None),
        repo=repo,
        getConnection=lambda entry: None))
    monkeypatch.setattr(agent_api, '_execOnHost',
                        lambda entry, command, timeoutSec, cwd='':
                        {'exit': 0, 'out': b'', 'err': b'', 'timedOut': False,
                         'reason': '', 'pid': None, 'truncated': False})
    resp = env.client.post('/api/agent/ssh/exec',
                           headers={'X-Agent-Token': TOKEN_FULL},
                           data=json.dumps({'host': 'h1', 'command': 'uptime'}),
                           content_type='application/json')
    assert resp.status_code == 200
    resp = env.client.get('/api/tasks/no-such-task',
                          headers={'X-Agent-Token': TOKEN_FULL})
    assert resp.status_code == 404   # 守卫放行后才落到端点的 404
    assert postUpload(env.client, TOKEN_FULL, b'legacy').status_code == 200


def test_explicit_full_scope_equals_full(env):
    """scopes: ["full"] 显式全权：上传也放行（full 蕴含一切 scope）。"""
    assert postUpload(env.client, TOKEN_EXPLICIT_FULL, b'x').status_code == 200


def test_upload_by_full_scope_token_exec_ok(env, monkeypatch):
    entries = [{'id': 'h1', 'name': 'x', 'host': '10.0.0.1', 'port': 22}]
    repo = SimpleNamespace(all=lambda: entries)
    monkeypatch.setattr(agent_api, 'hosts', SimpleNamespace(
        findHost=lambda hostId: next((e for e in entries if e['id'] == hostId), None),
        repo=repo,
        getConnection=lambda entry: None))
    monkeypatch.setattr(agent_api, '_execOnHost',
                        lambda entry, command, timeoutSec, cwd='':
                        {'exit': 0, 'out': b'', 'err': b'', 'timedOut': False,
                         'reason': '', 'pid': None, 'truncated': False})
    resp = env.client.post('/api/agent/ssh/exec',
                           headers={'X-Agent-Token': TOKEN_EXPLICIT_FULL},
                           data=json.dumps({'host': 'h1', 'command': 'uptime'}),
                           content_type='application/json')
    assert resp.status_code == 200


# ---- 上传端点本体 ----

def test_upload_success_multi_files_with_subdir(env):
    body = postUpload(env.client, TOKEN_UPLOAD,
                      [('报告.pdf', b'%PDF-body'), ('notes.txt', '中文'.encode())],
                      subdir='照片/2026-11').get_json()
    assert body['ok'] is True and body['failed'] == []
    root = env.tmp / '手机上传' / '照片' / '2026-11'
    assert (root / '报告.pdf').read_bytes() == b'%PDF-body'
    saved = {e['path']: e for e in body['saved']}
    entry = saved['手机上传/照片/2026-11/报告.pdf']   # 目标名/相对路径, 不暴露绝对路径
    assert entry['size'] == len(b'%PDF-body')
    assert entry['sha256'] == _digest(b'%PDF-body')
    assert str(env.tmp) not in json.dumps(body)
    up = [a for a in env.audit if a['action'] == 'agent.upload']
    assert up and up[-1]['target'] == '手机' and up[-1]['result'] == 'ok'
    assert 'scope=upload' in up[-1]['detail'] and 'target=phone' in up[-1]['detail']


def test_upload_folder_name_creates_subdir(env):
    """v3.1 验收：只上传 + 文件夹名 → 目标目录下真的出现该文件夹。"""
    body = postUpload(env.client, TOKEN_UPLOAD,
                      [('a.pdf', b'A'), ('b.pdf', b'B')],
                      subdir='周测卷').get_json()
    assert body['ok'] is True and len(body['saved']) == 2
    folder = env.tmp / '手机上传' / '周测卷'
    assert folder.is_dir()
    assert sorted(p.name for p in folder.iterdir()) == ['a.pdf', 'b.pdf']
    assert (folder / 'a.pdf').read_bytes() == b'A'
    assert {e['path'] for e in body['saved']} == {'手机上传/周测卷/a.pdf',
                                                  '手机上传/周测卷/b.pdf'}


def test_upload_duplicate_name_gets_2(env):
    first = postUpload(env.client, TOKEN_UPLOAD, b'one')
    second = postUpload(env.client, TOKEN_UPLOAD, b'two')
    assert first.status_code == 200 and second.status_code == 200
    body = second.get_json()
    assert body['saved'][0]['path'].endswith('a(2).txt')   # 仓库约定: (2) 在扩展名前
    assert (env.tmp / '手机上传' / 'a.txt').read_bytes() == b'one'   # 原件不动
    assert body['saved'][0]['sha256'] == _digest(b'two')


@pytest.mark.parametrize('bad', ['C:\\x\\y', 'C:/x', '/abs', '\\abs',
                                 'a/../b', '..', 'x<b', 'a"b', 'con:trol'])
def test_upload_bad_subdir_400(env, bad):
    resp = postUpload(env.client, TOKEN_UPLOAD, b'x', subdir=bad)
    assert resp.status_code == 400, bad
    assert 'subdir' in resp.get_json()['error']
    assert not (env.tmp / '手机上传').exists()   # 未落任何盘


def test_upload_subdir_normalizes_trailing_and_dot(env):
    resp = postUpload(env.client, TOKEN_UPLOAD, b'x', subdir='docs/./sub/')
    assert resp.status_code == 200
    assert (env.tmp / '手机上传' / 'docs' / 'sub' / 'a.txt').exists()


def test_upload_over_limit_413_no_partial_file(env):
    """夹具把上限压到 1MB：超限文件 413，半成品不落盘。"""
    payload = b'x' * (1024 * 1024 + 100)
    resp = postUpload(env.client, TOKEN_UPLOAD, [('big.bin', payload)])
    assert resp.status_code == 413
    body = resp.get_json()
    assert body['ok'] is False and body['saved'] == []
    assert '超过单文件上限' in body['failed'][0]['error']
    assert body['failed'][0]['name'] == 'big.bin'
    root = env.tmp / '手机上传'
    assert root.is_dir() and list(root.iterdir()) == []
    assert all(a['result'] == 'fail'
               for a in env.audit if a['action'] == 'agent.upload')


def test_upload_exact_limit_ok(env):
    payload = b'y' * (1024 * 1024)   # 恰好等于上限 = 放行
    resp = postUpload(env.client, TOKEN_UPLOAD, [('exact.bin', payload)])
    assert resp.status_code == 200
    assert (env.tmp / '手机上传' / 'exact.bin').read_bytes() == payload


def test_upload_missing_file_field_400(env):
    resp = env.client.post('/api/agent/upload', data={'subdir': 'x'},
                           headers={'X-Agent-Token': TOKEN_UPLOAD},
                           content_type='multipart/form-data')
    assert resp.status_code == 400


def test_upload_with_legacy_host_field_still_200(env):
    """批次 J：App 发的 host 是旧名；upload 没有机器参数，带 host 照常 200。"""
    data = {'file': [(io.BytesIO(b'x'), 'a.txt')], 'host': 'local'}
    resp = env.client.post('/api/agent/upload', data=data,
                           headers={'X-Agent-Token': TOKEN_UPLOAD},
                           content_type='multipart/form-data')
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True
    assert (env.tmp / '手机上传' / 'a.txt').read_bytes() == b'x'


def test_upload_bad_filename_all_failed_is_400(env):
    """文件名带路径分隔符 = 校验失败；全部失败按原因 400，不一律 500。"""
    resp = postUpload(env.client, TOKEN_UPLOAD, [('sub/evil.txt', b'x')])
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False and body['saved'] == []
    assert '文件名不合法' in body['failed'][0]['error']


def test_upload_mixed_partial_success_is_200(env):
    resp = postUpload(env.client, TOKEN_UPLOAD,
                      [('good.txt', b'ok'), ('bad/name.txt', b'no')])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True and len(body['saved']) == 1
    assert len(body['failed']) == 1 and body['failed'][0]['name'] == 'bad/name.txt'


# ---- 收件箱打通 ----

def test_upload_appears_in_inbox_recent(env, monkeypatch):
    """phoneUploadDir 自动并入 inboxDirs：上传即出现在 /api/inbox/recent。"""
    other = env.tmp / 'other-inbox'
    other.mkdir()
    cfgfile = env.tmp / 'config.json'
    cfgfile.write_text(json.dumps({
        'password': 'x', 'inboxEnabled': True, 'inboxDirs': [str(other)],
    }), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath',
                        lambda filename: str(env.tmp / 'state' / filename))
    configMod._cache.update(mtime=None, data=None)
    try:
        assert postUpload(env.client, TOKEN_UPLOAD,
                          [('hello.txt', b'from phone')]).status_code == 200
        with env.client.session_transaction() as sess:
            sess['authenticated'] = True
        body = env.client.get('/api/inbox/recent?limit=50').get_json()
        assert body['enabled'] is True
        phoneDir = str(env.tmp / '手机上传')
        assert any(d['path'] == phoneDir and d['source'] == '手机'
                   for d in body['dirs'])
        hit = [it for it in body['items'] if it['name'] == 'hello.txt']
        assert hit and hit[0]['source'] == '手机'
    finally:
        configMod._cache.update(mtime=None, data=None)


# ---- config 层：缺省与并入逻辑 ----

@pytest.fixture()
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'port': 6788, 'password': 'test'}),
                    encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
    configMod._cache.update(mtime=None, data=None)
    yield path
    configMod._cache.update(mtime=None, data=None)


def _setCfg(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')
    configMod._cache.update(mtime=None, data=None)


def test_phone_upload_dir_defaults_to_fallback(tmp_path, monkeypatch, cfgfile):
    monkeypatch.setattr(configMod, 'getFallbackDir', lambda: str(tmp_path / 'fb'))
    assert configMod.getPhoneUploadDir() == str(tmp_path / 'fb' / '手机上传')


def test_phone_upload_dir_explicit(cfgfile):
    _setCfg(cfgfile, {'phoneUploadDir': 'D:\\收件\\手机'})
    assert configMod.getPhoneUploadDir() == 'D:\\收件\\手机'


def test_phone_upload_max_mb_default_and_recovery(cfgfile):
    assert configMod.getPhoneUploadMaxMB() == 2048
    for raw in (0, -3, 'abc', [], {}):
        _setCfg(cfgfile, {'phoneUploadMaxMB': raw})
        assert configMod.getPhoneUploadMaxMB() == 2048, raw
    _setCfg(cfgfile, {'phoneUploadMaxMB': 512})
    assert configMod.getPhoneUploadMaxMB() == 512


def test_inbox_dirs_merge_phone(cfgfile, tmp_path, monkeypatch):
    other = tmp_path / 'ib'
    other.mkdir()
    phone = tmp_path / '手机上传'
    phone.mkdir()
    monkeypatch.setattr(configMod, 'getPhoneUploadDir', lambda: str(phone))
    _setCfg(cfgfile, {'inboxEnabled': True, 'inboxDirs': [str(other)]})
    assert configMod.getInboxDirs() == [str(other), str(phone)]
    # 手机目录未创建时不出现
    ghost = tmp_path / 'ghost'
    monkeypatch.setattr(configMod, 'getPhoneUploadDir', lambda: str(ghost))
    assert configMod.getInboxDirs() == [str(other)]
    # 关闭收件箱 → 一律空
    monkeypatch.setattr(configMod, 'getPhoneUploadDir', lambda: str(phone))
    _setCfg(cfgfile, {'inboxEnabled': False, 'inboxDirs': [str(other)]})
    assert configMod.getInboxDirs() == []
