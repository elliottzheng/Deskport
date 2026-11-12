# -*- coding: utf-8 -*-
"""设备登录 — App 密码换 upload 设备令牌。

覆盖: 登录 ok / 错密码 401 / 坏设备名 400 不计失败 / 每 IP 限速 429 /
全局锁 20 次/10 分钟（触发记 device.lockout, 网页登录不受影响）/
设备令牌上传 200 + exec 与 tasks 403 / 同设备名重登替换旧令牌 /
logout 自吊销后旧令牌 401 / 智能体令牌不能 logout / 设置页列表(无哈希)
与按 id 吊销 / touch 节流写盘 / 令牌签发后任何接口不回显 /
旧 agentTokens 不受影响。全部桩化, 不碰真实主机与真实配置。
"""
import datetime
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import device_auth, events
from modules import state as stateModule

PASSWORD = 'pw-123456'
TOKEN_AGENT = 'tok-agent-' + 'a' * 16   # 旧式全权令牌（config.json agentTokens）


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _auditOf(audit, action):
    return [a for a in audit if a['action'] == action]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """密码/状态目录/令牌/审计全部桩化 + 装配好的 client。

    statePath 指到临时目录: 用例不得读写真实 %APPDATA% 状态（devices.json
    与审计都经 statePath）。限速计数是模块级状态, 每个用例前清零。"""
    audit = []

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def fakeLog(action, target='', result='ok', detail='', ip=''):
        audit.append({'action': action, 'target': target, 'result': result,
                      'detail': detail, 'ip': ip})

    monkeypatch.setattr(configMod, 'getPassword', lambda: PASSWORD)
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'agent', 'name': '全权智能体', 'tokenHash': _hash(TOKEN_AGENT)}])
    monkeypatch.setattr(configMod, 'getPhoneUploadDir',
                        lambda: str(tmp_path / '手机上传'))
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 1)
    monkeypatch.setattr(events, 'log', fakeLog)
    device_auth._resetForTests()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit,
                           tmp=tmp_path, monkeypatch=monkeypatch)


def login(client, password=PASSWORD, device='小米手机', ip=None):
    kwargs = {'data': json.dumps({'password': password, 'device': device}),
              'content_type': 'application/json'}
    if ip:
        kwargs['environ_overrides'] = {'REMOTE_ADDR': ip}
    return client.post('/api/agent/device/login', **kwargs)


def upload(client, token, raw=b'hello', name='a.txt'):
    data = {'file': [(io.BytesIO(raw), name)]}
    headers = {} if token is None else {'X-Agent-Token': token}
    return client.post('/api/agent/upload', data=data, headers=headers,
                       content_type='multipart/form-data')


def devicesFile():
    with open(device_auth.devicesPath(), 'rb') as f:
        return json.loads(f.read())


# ---- 登录: ok / 401 / 坏设备名 ----

def test_login_ok_returns_token_once(env):
    resp = login(env.client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['name'] == '小米手机' and body['scopes'] == ['upload']
    assert len(body['token']) >= 40 and body['createdAt']
    stored = devicesFile()
    assert len(stored) == 1
    assert stored[0]['tokenHash'] == _hash(body['token'])  # 只存哈希
    assert body['token'] not in json.dumps(stored)         # 明文不落盘
    oks = _auditOf(env.audit, 'device.login')
    assert oks and oks[-1]['result'] == 'ok' and oks[-1]['target'] == '小米手机'


def test_login_bad_password_401(env):
    resp = login(env.client, password='wrong')
    assert resp.status_code == 401 and resp.get_json()['error'] == '密码错误'
    fails = _auditOf(env.audit, 'device.login')
    assert fails and fails[-1]['result'] == 'fail'


def test_login_no_password_configured_401(env, monkeypatch):
    monkeypatch.setattr(configMod, 'getPassword', lambda: '')
    resp = login(env.client, password='')
    assert resp.status_code == 401


def test_bad_device_name_400_not_counted(env):
    for bad in ('', 'x' * 25, '小<米', '手"机', 'a\nb'):
        resp = login(env.client, device=bad)
        assert resp.status_code == 400, bad
    # 坏设备名不进失败计数: 再错 4 次密码仍是 401(若计数早已 429)
    for _ in range(4):
        assert login(env.client, password='wrong').status_code == 401


def test_same_device_relogin_replaces_token(env):
    old = login(env.client).get_json()['token']
    new = login(env.client).get_json()['token']
    assert old != new
    assert len(devicesFile()) == 1
    assert upload(env.client, old).status_code == 401   # 旧令牌即刻失效
    assert upload(env.client, new).status_code == 200


# ---- 防爆破: 每 IP / 全局 ----

def test_per_ip_lockout(env):
    for _ in range(5):
        assert login(env.client, password='wrong').status_code == 401
    limited = login(env.client, password='wrong')
    assert limited.status_code == 429
    assert limited.get_json()['error'] == '尝试次数过多, 请稍后再试'
    # 锁定期间正确密码也 429
    assert login(env.client).status_code == 429
    assert _auditOf(env.audit, 'device.login')[-1]['result'] == 'locked'


def test_global_lockout_and_web_login_unaffected(env):
    # 5 个 IP 各错 4 次 = 全局 20 次失败
    for n in range(5):
        ip = '10.0.0.%d' % n
        for _ in range(4):
            assert login(env.client, password='wrong', ip=ip).status_code == 401
    # 第 20 次失败已触发全局锁: 任何 IP(含全新 IP)正确密码也 429
    resp = login(env.client, ip='10.9.9.9')
    assert resp.status_code == 429
    lockouts = _auditOf(env.audit, 'device.lockout')
    assert len(lockouts) == 1 and lockouts[0]['result'] == 'on'
    # 再试仍 429, 不重复写 lockout
    assert login(env.client).status_code == 429
    assert len(_auditOf(env.audit, 'device.lockout')) == 1
    # 网页登录计数独立: 正确密码照常 302
    resp = env.client.post('/login', data={'password': PASSWORD})
    assert resp.status_code == 302


# ---- 设备令牌走同一鉴权入口 ----

def test_device_token_upload_ok_and_full_denied(env):
    token = login(env.client).get_json()['token']
    assert upload(env.client, token, b'x' * 10).status_code == 200
    resp = env.client.post('/api/agent/ssh/exec',
                           headers={'X-Agent-Token': token},
                           data=json.dumps({'host': 'local', 'command': 'ls'}),
                           content_type='application/json')
    assert resp.status_code == 403 and resp.get_json()['need'] == 'full'
    resp = env.client.post('/api/tasks', headers={'X-Agent-Token': token},
                           data=json.dumps({'prompt': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 403 and resp.get_json()['need'] == 'full'
    # 上传审计记设备名
    ups = _auditOf(env.audit, 'agent.upload')
    assert ups and ups[-1]['target'] == '小米手机'


def test_legacy_agent_token_unaffected(env):
    resp = env.client.get('/api/agent/hosts',
                          headers={'X-Agent-Token': TOKEN_AGENT})
    assert resp.status_code == 200
    assert resp.get_json()['tokenName'] == '全权智能体'


# ---- 自吊销与管理面 ----

def test_logout_self_revokes(env):
    token = login(env.client).get_json()['token']
    assert upload(env.client, token).status_code == 200
    resp = env.client.post('/api/agent/device/logout',
                           headers={'X-Agent-Token': token})
    assert resp.status_code == 200 and resp.get_json() == {'ok': True}
    assert devicesFile() == []
    assert upload(env.client, token).status_code == 401
    assert _auditOf(env.audit, 'device.logout')[-1]['result'] == 'ok'


def test_logout_rejects_agent_token(env):
    resp = env.client.post('/api/agent/device/logout',
                           headers={'X-Agent-Token': TOKEN_AGENT})
    assert resp.status_code == 403
    resp = env.client.post('/api/agent/device/logout')
    assert resp.status_code == 401


def test_settings_list_revoke_and_session_required(env):
    # 未登录(无 Cookie 无令牌)不能管理设备
    assert env.client.get('/api/settings/devices').status_code == 401
    env.client.post('/login', data={'password': PASSWORD})  # 拿会话 Cookie
    login(env.client)   # 先有一台设备
    listed = env.client.get('/api/settings/devices').get_json()['devices']
    assert len(listed) == 1 and listed[0]['scopes'] == ['upload']
    assert listed[0]['createdAt'] and listed[0]['lastIp']
    token = login(env.client, device='第二台').get_json()['token']
    listed = env.client.get('/api/settings/devices').get_json()['devices']
    assert len(listed) == 2
    assert 'tokenHash' not in json.dumps(listed)   # 令牌签发后永不回显
    rid = listed[1]['id']
    resp = env.client.post('/api/settings/devices/revoke',
                           data=json.dumps({'id': rid}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert len(resp.get_json()['devices']) == 1
    assert upload(env.client, token).status_code == 401   # 吊销即失效
    assert _auditOf(env.audit, 'device.revoke')[-1]['target'] == '第二台'
    assert env.client.post('/api/settings/devices/revoke',
                           data=json.dumps({'id': 'nope'}),
                           content_type='application/json').status_code == 404


def test_device_cap_20(env):
    for n in range(device_auth.MAX_DEVICES):
        assert login(env.client, device='设备%d' % n).status_code == 200
    resp = login(env.client, device='第21台')
    assert resp.status_code == 400 and '上限' in resp.get_json()['error']
    # 同名重登不算新设备, 满员时仍可替换
    assert login(env.client, device='设备0').status_code == 200


# ---- touch 节流: 同一秒内多次使用只写一次盘 ----

def test_touch_throttle_writes_once_per_second(env, monkeypatch):
    token = login(env.client).get_json()['token']

    class FrozenNow(datetime.datetime):
        @staticmethod
        def now():
            return datetime.datetime(2026, 1, 1, 12, 0, 0)

    class FrozenModule(object):
        datetime = FrozenNow

    monkeypatch.setattr(device_auth, 'datetime', FrozenModule)
    calls = []
    orig = stateModule.atomicWriteJson

    def spy(path, data):
        if str(path).endswith(device_auth.DEVICES_FILE):
            calls.append(path)
        return orig(path, data)

    monkeypatch.setattr(stateModule, 'atomicWriteJson', spy)
    assert upload(env.client, token).status_code == 200
    after_first = len(calls)
    assert after_first == 1                  # 首次使用回写 lastUsedAt/lastIp
    assert upload(env.client, token).status_code == 200
    assert len(calls) == after_first         # 同一秒内第二次使用不再写盘
    stored = devicesFile()[0]
    assert stored['lastUsedAt'] == '2026-01-01 12:00:00'
    assert stored['lastIp'] == '127.0.0.1'
