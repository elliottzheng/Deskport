# -*- coding: utf-8 -*-
"""并发登录窗口(批次 DL) — 同名设备 60 秒内多次登录 = 同一次登录的并发尝试。

从 test_device_login.py 拆出(结构门禁 400 行预算)。覆盖: 窗口内多个令牌
并列有效、任一令牌第一次被用于鉴权即收敛(其余 burst 即废, lastUsedAt/lastIp
回写)、burst 上限 4 丢最旧、窗口过后未使用的 burst 仍有效、窗口外再登整条
替换(旧令牌即刻失效)、吊销(设置页 / 自吊销)删整条含 burst、真并发三登录
落在同一条目。全部桩化, 不碰真实主机与真实配置。
"""
import hashlib
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import device_auth, events
from modules import state as stateModule
from modules import tasks_store as tasksStore

PASSWORD = 'pw-123456'


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """密码/状态目录全部桩化 + 装配好的 client；限速计数每例清零。"""

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'getPassword', lambda: PASSWORD)
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(configMod, 'getPhoneUploadDir',
                        lambda: str(tmp_path / '手机上传'))
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 1)
    monkeypatch.setattr(tasksStore, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    device_auth._resetForTests()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), app=app,
                           tmp=tmp_path, monkeypatch=monkeypatch)


def login(client, password=PASSWORD, device='小米手机'):
    return client.post('/api/agent/device/login',
                       data=json.dumps({'password': password, 'device': device}),
                       content_type='application/json')


def stage(client, token):
    """share/stage(桩文件): 设备令牌 share 入口, 用来验令牌是否还有效。"""
    data = {'file': [(io.BytesIO(b'x'), 'a.txt')]}
    return client.post('/api/agent/share/stage', data=data,
                       headers={'X-Agent-Token': token},
                       content_type='multipart/form-data')


def devicesFile():
    with open(device_auth.devicesPath(), 'rb') as f:
        return json.loads(f.read())


class _Clock(object):
    """可推进的 time.time 桩: 窗口外场景要把时钟拨过 burstUntil。"""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def frozenClock(monkeypatch, start):
    clock = _Clock(start)
    monkeypatch.setattr(device_auth, 'time', clock)
    return clock


# ---- 窗口外再登 = 真重登: 整条替换, 旧令牌即刻失效 ----

def test_relogin_outside_window_replaces_token(env, monkeypatch):
    clock = frozenClock(monkeypatch, time.time())
    old = login(env.client).get_json()['token']
    clock.advance(device_auth.BURST_WINDOW_SECONDS + 1)
    new = login(env.client).get_json()['token']
    assert old != new
    assert len(devicesFile()) == 1
    assert devicesFile()[0]['burstHashes'] == []
    assert stage(env.client, old).status_code == 401
    assert stage(env.client, new).status_code == 200


# ---- 窗口内: 多个令牌并列有效, 任一令牌先用即收敛 ----

@pytest.mark.parametrize('first', [0, 1, 2])
def test_burst_window_any_token_usable_then_converge(env, first):
    tokens = [login(env.client).get_json()['token'] for _ in range(3)]
    assert len(set(tokens)) == 3
    stored = devicesFile()
    assert len(stored) == 1                     # 同名多次登录还是一条设备
    assert stored[0]['tokenHash'] == _hash(tokens[0])
    assert stored[0]['burstHashes'] == [_hash(t) for t in tokens[1:]]
    assert stage(env.client, tokens[first]).status_code == 200  # 任一令牌可用
    for n, token in enumerate(tokens):
        if n != first:
            assert stage(env.client, token).status_code == 401
    after = devicesFile()[0]
    assert after['tokenHash'] == _hash(tokens[first])
    assert after['burstHashes'] == []           # 收敛: 只留这一个 hash
    assert after['lastUsedAt'] and after['lastIp'] == '127.0.0.1'


def test_burst_cap_four_keeps_newest(env):
    tokens = [login(env.client).get_json()['token'] for _ in range(6)]
    stored = devicesFile()[0]
    assert stored['tokenHash'] == _hash(tokens[0])
    assert stored['burstHashes'] == [_hash(t) for t in tokens[2:]]   # 丢最旧
    assert stage(env.client, tokens[1]).status_code == 401   # 被挤掉的即废
    assert stage(env.client, tokens[0]).status_code == 200
    assert stage(env.client, tokens[2]).status_code == 401   # 收敛后其余即废


def test_unused_burst_stays_valid_after_window(env, monkeypatch):
    clock = frozenClock(monkeypatch, time.time())
    first = login(env.client).get_json()['token']
    burst = login(env.client).get_json()['token']
    clock.advance(device_auth.BURST_WINDOW_SECONDS + 1)      # 窗口过了, 不登录
    assert stage(env.client, burst).status_code == 200       # 没用过的 burst 仍有效
    assert devicesFile()[0]['tokenHash'] == _hash(burst)
    assert stage(env.client, first).status_code == 401       # 已收敛


# ---- 吊销删整条(含 burst) ----

def test_revoke_removes_whole_burst(env):
    tokens = [login(env.client).get_json()['token'] for _ in range(3)]
    env.client.post('/login', data={'password': PASSWORD})   # 拿会话 Cookie
    listed = env.client.get('/api/settings/devices').get_json()['devices']
    assert len(listed) == 1
    resp = env.client.post('/api/settings/devices/revoke',
                           data=json.dumps({'id': listed[0]['id']}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert devicesFile() == []
    for token in tokens:
        assert stage(env.client, token).status_code == 401


def test_logout_with_burst_token_removes_entry(env):
    tokens = [login(env.client).get_json()['token'] for _ in range(3)]
    resp = env.client.post('/api/agent/device/logout',
                           headers={'X-Agent-Token': tokens[1]})
    assert resp.status_code == 200 and resp.get_json() == {'ok': True}
    assert devicesFile() == []
    for token in tokens:
        assert stage(env.client, token).status_code == 401


# ---- 真并发: raceLogin 三入口齐发 ----

def test_concurrent_logins_land_on_one_entry(env, monkeypatch):
    """三线程同一秒齐登: 签发串行化, 三个令牌落在同一条目且都有效。"""
    orig = stateModule.atomicWriteJson

    def slowWrite(path, data):
        time.sleep(0.05)     # 放大读-写穿插窗口: 无签发锁时必现丢更新
        return orig(path, data)

    monkeypatch.setattr(stateModule, 'atomicWriteJson', slowWrite)
    barrier = threading.Barrier(3)
    codes = [None] * 3
    tokens = [None] * 3

    def worker(n):
        client = env.app.test_client()
        barrier.wait()
        resp = login(client)
        codes[n] = resp.status_code
        if codes[n] == 200:
            tokens[n] = resp.get_json()['token']

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200, 200, 200], codes
    assert len(set(tokens)) == 3
    hashes = {_hash(t) for t in tokens}
    stored = devicesFile()
    assert len(stored) == 1                     # 并发登录收敛成一条设备
    assert stored[0]['tokenHash'] in hashes     # 主哈希是抢先拿到签发锁的那个
    assert set(stored[0]['burstHashes']) == hashes - {stored[0]['tokenHash']}
    primary = next(t for t in tokens if _hash(t) == stored[0]['tokenHash'])
    assert stage(env.client, primary).status_code == 200
    for token in tokens:
        if token != primary:
            assert stage(env.client, token).status_code == 401
