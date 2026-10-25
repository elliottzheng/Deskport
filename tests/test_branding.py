# -*- coding: utf-8 -*-
"""品牌名：默认 Deskport，可配置并注入页面/manifest。"""
import json

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


def test_brand_default_and_set(cfgfile):
    assert config.getBrandName() == 'Deskport'
    config.setBrandName('我的工作台')
    assert config.getBrandName() == '我的工作台'


def test_brand_injected_into_pages_and_manifest(client, cfgfile):
    config.setBrandName('AcmeDesk')
    from app import app
    login = app.test_client().get('/login').get_data(as_text=True)
    assert 'AcmeDesk' in login
    appPage = client.get('/app').get_data(as_text=True)
    assert 'wp-brand' in appPage and 'AcmeDesk' in appPage
    resp = client.get('/manifest.webmanifest')
    assert resp.headers['Content-Type'].startswith('application/manifest+json')
    manifest = resp.get_json()
    assert manifest['name'] == 'AcmeDesk'
    assert manifest['short_name'] == 'AcmeDesk'


def test_brand_api_validates_and_saves(client, cfgfile):
    resp = client.get('/api/settings/brand')
    assert resp.get_json()['name'] == 'Deskport'

    resp = client.post('/api/settings/brand', json={'name': 'AcmeDesk'})
    assert resp.status_code == 200
    assert resp.get_json()['name'] == 'AcmeDesk'
    assert config.getBrandName() == 'AcmeDesk'

    assert client.post('/api/settings/brand', json={'name': ''}).status_code == 400
    assert client.post('/api/settings/brand', json={'name': '<bad>'}).status_code == 400
    assert client.post('/api/settings/brand',
                       json={'name': 'x' * 25}).status_code == 400
