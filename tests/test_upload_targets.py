# -*- coding: utf-8 -*-
"""上传目标白名单（契约 2026-11-12 服务端部分）。

覆盖: targets 缺省=phone、指定目标、不存在 400 带 targets 列表、子目录、
saved[].path 不泄露绝对路径、GET /api/agent/upload/targets、login 响应带
targets、审计记 target id、目标内重名 (2)、设置页增删改（id 生成/保留、
phone 不可删、校验）、旧 phone 行为不变。全部桩化，不碰真实配置。
"""
import hashlib
import io
import json
import re
from types import SimpleNamespace

import pytest

import config as configMod
from modules import device_auth, events

PASSWORD = 'pw-123456'
TOKEN_AGENT = 'tok-agent-' + 'a' * 16


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def fakeLog(action, target='', result='ok', detail='', ip=''):
        audit.append({'action': action, 'target': target, 'result': result,
                      'detail': detail, 'ip': ip})

    extra = tmp_path / '教研资料'
    extra.mkdir()
    # CONFIG_PATH 指到临时配置: uploadTargets 走真实读写链路（含 mtime
    # 热读）, 绝不碰真实 config.json; settings CRUD 的写入也落在临时文件
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({
        'password': PASSWORD,
        'phoneUploadDir': str(tmp_path / '手机上传'),
        'uploadTargets': [{'id': 'jyzl', 'name': '教研资料',
                           'path': str(extra)}],
    }), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'agent', 'name': '全权智能体', 'tokenHash': _hash(TOKEN_AGENT)}])
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 5)
    monkeypatch.setattr(events, 'log', fakeLog)
    configMod._cache.update(mtime=None, data=None)
    device_auth._resetForTests()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    try:
        yield SimpleNamespace(client=app.test_client(), audit=audit,
                              tmp=tmp_path, extra=extra)
    finally:
        configMod._cache.update(mtime=None, data=None)


def login(client, password=PASSWORD, device='小米手机'):
    return client.post('/api/agent/device/login',
                       data=json.dumps({'password': password,
                                        'device': device}),
                       content_type='application/json')


def deviceToken(client):
    return login(client).get_json()['token']


def postUpload(client, token, parts=b'hello', name='a.txt',
               subdir=None, target=None):
    if isinstance(parts, bytes):
        parts = [(name, parts)]
    data = {'file': [(io.BytesIO(raw), n) for n, raw in parts]}
    if subdir is not None:
        data['subdir'] = subdir
    if target is not None:
        data['target'] = target
    headers = {} if token is None else {'X-Agent-Token': token}
    return client.post('/api/agent/upload', data=data, headers=headers,
                       content_type='multipart/form-data')


def auditOf(env, action):
    return [a for a in env.audit if a['action'] == action]


# ---- login / targets 端点 ----

def test_login_response_carries_targets_without_path(env):
    body = login(env.client).get_json()
    assert body['targets'] == [{'id': 'phone', 'name': '手机上传'},
                               {'id': 'jyzl', 'name': '教研资料'}]
    assert 'path' not in json.dumps(body)   # 目录不出网


def test_upload_targets_endpoint(env):
    token = deviceToken(env.client)
    body = env.client.get('/api/agent/upload/targets',
                          headers={'X-Agent-Token': token}).get_json()
    assert body['default'] == 'phone'
    assert body['targets'] == [{'id': 'phone', 'name': '手机上传'},
                               {'id': 'jyzl', 'name': '教研资料'}]
    assert 'path' not in json.dumps(body)
    assert env.client.get('/api/agent/upload/targets').status_code == 401
    resp = env.client.get('/api/agent/upload/targets',
                          headers={'X-Agent-Token': TOKEN_AGENT})
    assert resp.status_code == 200 and resp.get_json()['default'] == 'phone'


# ---- upload target ----

def test_upload_default_target_is_phone(env):
    token = deviceToken(env.client)
    body = postUpload(env.client, token, b'x').get_json()
    assert body['saved'][0]['path'] == '手机上传/a.txt'
    assert (env.tmp / '手机上传' / 'a.txt').read_bytes() == b'x'
    up = auditOf(env, 'agent.upload')[-1]
    assert 'target=phone' in up['detail'] and up['result'] == 'ok'


def test_upload_explicit_target(env):
    token = deviceToken(env.client)
    body = postUpload(env.client, token, target='jyzl').get_json()
    assert body['saved'][0]['path'] == '教研资料/a.txt'
    assert (env.extra / 'a.txt').read_bytes() == b'hello'
    assert 'target=jyzl' in auditOf(env, 'agent.upload')[-1]['detail']


def test_upload_unknown_target_400(env):
    token = deviceToken(env.client)
    resp = postUpload(env.client, token, target='nope')
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['error'] == '上传目标不存在'
    assert [t['id'] for t in body['targets']] == ['phone', 'jyzl']
    assert 'path' not in json.dumps(body)
    assert not (env.tmp / '手机上传').exists()   # 未落盘
    assert auditOf(env, 'agent.upload')[-1]['result'] == 'fail'


def test_upload_target_with_subdir(env):
    token = deviceToken(env.client)
    body = postUpload(env.client, token, target='jyzl',
                      subdir='11月/资料').get_json()
    assert body['saved'][0]['path'] == '教研资料/11月/资料/a.txt'
    assert (env.extra / '11月' / '资料' / 'a.txt').read_bytes() == b'hello'


def test_upload_response_never_leaks_paths(env):
    token = deviceToken(env.client)
    body = postUpload(env.client, token, target='jyzl', subdir='x').get_json()
    text = json.dumps(body, ensure_ascii=False)
    assert str(env.tmp) not in text
    assert ':\\' not in text and '教研资料\\' not in text


def test_upload_duplicate_name_gets_2_within_target(env):
    token = deviceToken(env.client)
    assert postUpload(env.client, token, b'one', target='jyzl').status_code == 200
    body = postUpload(env.client, token, b'two', target='jyzl').get_json()
    assert body['saved'][0]['path'] == '教研资料/a(2).txt'


# ---- 设置页管理面 ----

def _session(client):
    client.post('/login', data={'password': PASSWORD})


def test_settings_upload_targets_requires_session(env):
    assert env.client.get('/api/settings/uploadTargets').status_code == 401
    resp = env.client.post('/api/settings/uploadTargets',
                           data=json.dumps({'targets': []}),
                           content_type='application/json')
    assert resp.status_code == 401


def test_settings_upload_targets_crud(env, tmp_path):
    _session(env.client)
    listed = env.client.get('/api/settings/uploadTargets').get_json()
    assert listed['builtinId'] == 'phone'
    assert [t['id'] for t in listed['targets']] == ['phone', 'jyzl']

    spare = tmp_path / '备课目录'
    spare.mkdir()
    # 增: 无 id → 服务端生成短 slug; 已有 id 保留
    resp = env.client.post('/api/settings/uploadTargets',
                           data=json.dumps({'targets': [
                               {'id': 'jyzl', 'name': '教研资料',
                                'path': str(env.extra)},
                               {'name': '备课', 'path': str(spare)}]}),
                           content_type='application/json')
    assert resp.status_code == 200
    targets = resp.get_json()['targets']
    assert [t['name'] for t in targets[1:]] == ['教研资料', '备课']
    assert targets[1]['id'] == 'jyzl'
    assert targets[2]['id'] != 'jyzl'
    assert re.match(r'^[a-z0-9_-]{1,20}$', targets[2]['id'])

    # 改: id 不变（App 已保存的 targets 不失效）, 名称/目录可改
    resp = env.client.post('/api/settings/uploadTargets',
                           data=json.dumps({'targets': [
                               {'id': 'jyzl', 'name': '教研资料2',
                                'path': str(spare)},
                               {'id': targets[2]['id'], 'name': '备课',
                                'path': str(env.extra)}]}),
                           content_type='application/json')
    targets = resp.get_json()['targets']
    assert [t['id'] for t in targets[1:]] == ['jyzl', targets[2]['id']]
    assert [t['name'] for t in targets[1:]] == ['教研资料2', '备课']
    assert targets[1]['path'] == str(spare)

    # 删: 只留 jyzl; phone 永远在, 且不可被载荷改动
    resp = env.client.post('/api/settings/uploadTargets',
                           data=json.dumps({'targets': [
                               {'id': 'jyzl', 'name': '教研资料2',
                                'path': str(env.extra)},
                               {'id': 'phone', 'name': '改名尝试',
                                'path': 'C:\\hack'}]}),
                           content_type='application/json')
    targets = resp.get_json()['targets']
    assert targets[0]['id'] == 'phone' and targets[0]['name'] == '手机上传'
    assert [t['id'] for t in targets[1:]] == ['jyzl']


def test_settings_upload_targets_validation(env, tmp_path):
    _session(env.client)
    real = str(env.extra)
    missing = str(tmp_path / '没有这个目录')
    payloads = [
        [{'name': '', 'path': real}],                # 空名称
        [{'name': 'x' * 25, 'path': real}],          # 名称过长
        [{'name': 'a<b', 'path': real}],             # 非法字符
        [{'name': '重复', 'path': real},
         {'name': '重复', 'path': real}],             # 载荷内名称重复
        [{'name': '相对', 'path': '相对/目录'}],      # 目录不存在
        [{'name': '不存在', 'path': missing}],       # 目录不存在
        [{'id': 'PHONE', 'name': '大写', 'path': real}],   # id 不合 slug
        [{'name': '目标%d' % i, 'path': real} for i in range(20)],  # 超数量
    ]
    for payload in payloads:
        resp = env.client.post('/api/settings/uploadTargets',
                               data=json.dumps({'targets': payload}),
                               content_type='application/json')
        assert resp.status_code == 400, payload[:1]
    # 校验失败不清空已有目标
    assert [t['id'] for t in env.client.get(
        '/api/settings/uploadTargets').get_json()['targets']] == ['phone', 'jyzl']


def test_settings_upload_targets_audit(env):
    _session(env.client)
    env.client.post('/api/settings/uploadTargets',
                    data=json.dumps({'targets': []}),
                    content_type='application/json')
    entry = auditOf(env, 'settings.uploadTargets')[-1]
    assert entry['result'] == 'ok' and 'targets=0' in entry['detail']
