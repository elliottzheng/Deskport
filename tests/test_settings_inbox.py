# -*- coding: utf-8 -*-
"""收件箱设置：默认关闭，开启后按配置目录扫描。"""
import json
import os

import pytest

import config


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


def test_inbox_disabled_by_default(cfgfile):
    assert config.isInboxEnabled() is False
    assert config.getInboxDirs() == []


def test_default_inbox_dirs_use_software_locations(monkeypatch):
    home = os.path.expanduser('~')
    monkeypatch.setattr(config.os.path, 'isdir', lambda path: True)
    dirs = config.defaultInboxDirs()
    assert dirs
    assert all(path.startswith(home) for path in dirs)
    assert not any('wxid_' in path for path in dirs)


def test_enable_and_configure_dirs(cfgfile, tmp_path):
    first = tmp_path / 'inbox-a'
    second = tmp_path / 'inbox-b'
    first.mkdir()
    second.mkdir()
    config.setInboxSettings(True, [str(first), str(second)])
    assert config.isInboxEnabled() is True
    assert config.getInboxDirs() == [str(first), str(second)]

    config.setInboxSettings(False, [str(first)])
    assert config.getInboxDirs() == []


def test_inbox_api_roundtrip(client, cfgfile, tmp_path):
    inbox = tmp_path / 'inbox'
    inbox.mkdir()

    resp = client.get('/api/settings/inbox')
    assert resp.status_code == 200
    initial = resp.get_json()
    assert initial['enabled'] is False
    assert 'defaults' in initial and 'detected' not in initial
    assert initial.get('home')

    resp = client.post('/api/settings/inbox',
                       json={'enabled': True, 'dirs': [str(inbox)]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['dirs'] == [str(inbox)]

    resp = client.get('/api/settings/inbox')
    data = resp.get_json()
    assert data['enabled'] is True
    assert str(inbox) in data['dirs']
    assert str(inbox) in config.getInboxDirs()


def test_inbox_api_rejects_missing_dir(client, cfgfile, tmp_path):
    resp = client.post('/api/settings/inbox',
                       json={'enabled': True, 'dirs': [str(tmp_path / 'nope')]})
    assert resp.status_code == 400
