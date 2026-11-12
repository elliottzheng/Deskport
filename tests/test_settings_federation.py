# -*- coding: utf-8 -*-
"""联邦设置：本机令牌与远端机器列表的读写、校验与机器选项。"""
import json

import pytest

import config
from modules import federation


@pytest.fixture()
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'port': 6788, 'password': 'test'}), encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', str(path))
    config._cache.update(mtime=None, data=None)
    yield path
    config._cache.update(mtime=None, data=None)


@pytest.fixture()
def client():
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def test_federation_disabled_by_default(cfgfile):
    assert config.getFederationToken() == ''
    assert config.getRemotes() == []
    assert federation.machineOptions() == [{'id': '', 'name': '本机'}]


def test_set_token_empty_disables(cfgfile):
    config.setFederationToken('abcdefgh')
    assert config.getFederationToken() == 'abcdefgh'
    config.setFederationToken('')
    assert config.getFederationToken() == ''
    assert json.loads(cfgfile.read_text(encoding='utf-8'))['federationToken'] == ''


def test_set_remotes_roundtrip(cfgfile):
    remotes = [{'id': 'win4710', 'name': 'i7-4710MQ',
                'base': 'http://10.126.126.3:6789/', 'token': 'vn1WdB7IAKfd'}]
    config.setRemotes(remotes)
    assert config.getRemotes() == [{'id': 'win4710', 'name': 'i7-4710MQ',
                                    'base': 'http://10.126.126.3:6789',
                                    'token': 'vn1WdB7IAKfd',
                                    'terminal': True, 'apps': [], 'files': False,
                                    'models': [], 'model': ''}]   # 策略缺省: 接终端, 不接应用/文件, 模型未声明
    # 其他配置键保留
    assert json.loads(cfgfile.read_text(encoding='utf-8'))['port'] == 6788
    assert federation.machineOptions() == [
        {'id': '', 'name': '本机'},
        {'id': 'win4710', 'name': 'i7-4710MQ'},
    ]


def test_token_api_roundtrip(client, cfgfile):
    resp = client.get('/api/settings/federation')
    assert resp.status_code == 200
    assert resp.get_json()['token'] == ''
    assert resp.get_json()['remotes'] == []
    assert resp.get_json()['machines'] == [{'id': '', 'name': '本机'}]

    resp = client.post('/api/settings/federation/token', json={'token': 'tok-12345678'})
    assert resp.status_code == 200
    assert resp.get_json()['token'] == 'tok-12345678'
    assert config.getFederationToken() == 'tok-12345678'


def test_token_api_rejects_bad_shape(client, cfgfile):
    for bad in ['short', 'has space!', 'x' * 65, '中文令牌']:
        resp = client.post('/api/settings/federation/token', json={'token': bad})
        assert resp.status_code == 400, bad
    assert config.getFederationToken() == ''
    # 空串合法：表示关闭联邦接入
    resp = client.post('/api/settings/federation/token', json={'token': ''})
    assert resp.status_code == 200


def test_remotes_api_roundtrip(client, cfgfile):
    payload = {'remotes': [
        {'id': 'r-1', 'name': '办公室台式机', 'base': 'http://10.0.0.2:6789', 'token': 'aaaaaaaa',
         'terminal': False, 'apps': ['yuanchuang']},
        {'id': 'r-2', 'name': '家里服务器', 'base': 'https://home.example.com', 'token': ''},
    ]}
    resp = client.post('/api/settings/federation/remotes', json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    machines = resp.get_json()['machines']
    assert machines[0] == {'id': '', 'name': '本机'}
    assert [m['id'] for m in machines[1:]] == ['r-2']              # terminal=false 不出现在新建会话选项
    assert config.getRemotes()[0]['base'] == 'http://10.0.0.2:6789'
    assert config.getRemotes()[0]['terminal'] is False             # 策略键原样保留
    assert config.getRemotes()[0]['apps'] == ['yuanchuang']
    assert config.getRemotes()[1]['terminal'] is True and config.getRemotes()[1]['apps'] == []
    # 远端令牌不回传到 machines 列表
    assert all('token' not in m for m in machines[1:])


@pytest.mark.parametrize('bad', [
    {'id': 'r1', 'name': 'x' * 25, 'base': 'http://a:1', 'token': ''},
    {'id': 'r1', 'name': 'ok', 'base': 'ftp://a:1', 'token': ''},
    {'id': 'r1', 'name': 'ok', 'base': '', 'token': ''},
    {'id': 'r1', 'name': 'ok', 'base': 'http://a:1', 'token': 't' * 129},
    {'id': 'BAD', 'name': 'ok', 'base': 'http://a:1', 'token': ''},   # 允许: 自动转小写
    {'id': 'r 1', 'name': 'ok', 'base': 'http://a:1', 'token': ''},
    {'id': '', 'name': 'ok', 'base': 'http://a:1', 'token': ''},
])
def test_remotes_api_rejects_bad_entry(client, cfgfile, bad):
    resp = client.post('/api/settings/federation/remotes', json={'remotes': [bad]})
    if bad['id'] == 'BAD':
        assert resp.status_code == 200
        assert config.getRemotes()[0]['id'] == 'bad'
        return
    assert resp.status_code == 400, bad
    assert config.getRemotes() == []


def test_remotes_api_rejects_duplicate_id(client, cfgfile):
    resp = client.post('/api/settings/federation/remotes', json={'remotes': [
        {'id': 'r1', 'name': 'a', 'base': 'http://a:1', 'token': ''},
        {'id': 'r1', 'name': 'b', 'base': 'http://b:2', 'token': ''},
    ]})
    assert resp.status_code == 400
    assert config.getRemotes() == []


def test_remotes_api_rejects_non_list(client, cfgfile):
    resp = client.post('/api/settings/federation/remotes', json={'remotes': 'nope'})
    assert resp.status_code == 400


def test_federation_api_requires_login(tmp_path, monkeypatch):
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        assert c.get('/api/settings/federation').status_code == 401
