# -*- coding: utf-8 -*-
"""成员端联邦写根白名单的设置接口：读写、逐项校验、清空即拒绝。"""
import json
import os

import pytest

import config
from modules import federation_write as fw


@pytest.fixture(autouse=True)
def cleanRoots():
    config.setFederationRoots([])
    yield
    config.setFederationRoots([])


@pytest.fixture()
def client():
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def _fed():
    return {'X-Deskport-From': 'hub', 'X-Deskport-Actor': '10.0.0.5'}


def _newFolder(client, root, name):
    return client.post('/api/files/new-folder',
                       json={'path': str(root), 'name': name}, headers=_fed())


def test_roots_default_empty(client):
    data = client.get('/api/settings/federation').get_json()
    assert data['roots'] == []
    assert data['maxRoots'] == fw.MAX_ROOTS
    assert fw.checkPaths(['E:\\whatever.txt'])[0] is False


def test_add_root_takes_effect_without_restart(client, tmp_path):
    root = tmp_path / 'share'
    root.mkdir()
    resp = client.post('/api/settings/federation/roots',
                       json={'roots': [str(root)]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    normalized = fw.normalizePath(str(root))
    assert resp.get_json()['roots'] == [normalized]
    assert client.get('/api/settings/federation').get_json()['roots'] == [normalized]
    stored = json.loads(open(config.CONFIG_PATH, encoding='utf-8').read())
    assert stored['federationRoots'] == [normalized]

    # 同一进程、未重启：白名单立即生效
    assert fw.checkPaths([str(root / 'a.txt')])[0] is True
    assert _newFolder(client, root, 'made').status_code == 200
    assert (root / 'made').is_dir()


def test_clear_roots_denies_immediately(client, tmp_path):
    root = tmp_path / 'share'
    root.mkdir()
    client.post('/api/settings/federation/roots', json={'roots': [str(root)]})
    assert _newFolder(client, root, 'before').status_code == 200

    resp = client.post('/api/settings/federation/roots', json={'roots': []})
    assert resp.status_code == 200
    assert resp.get_json()['roots'] == []

    denied = _newFolder(client, root, 'after')
    assert denied.status_code == 403
    assert not (root / 'after').exists()
    ok, why = fw.checkPaths([str(root / 'a.txt')])
    assert not ok and 'federationRoots' in why


@pytest.mark.parametrize('bad', [
    'relative\\x.txt',
    '..',
    '..\\..\\Windows',
    'C:relative',
    '\\\\.\\PhysicalDrive0',
    '',
    '   ',
])
def test_rejects_bad_item_and_keeps_previous(client, tmp_path, bad):
    root = tmp_path / 'share'
    root.mkdir()
    client.post('/api/settings/federation/roots', json={'roots': [str(root)]})
    before = config.load()['federationRoots']

    resp = client.post('/api/settings/federation/roots',
                       json={'roots': [str(root), bad]})
    assert resp.status_code == 400, bad
    assert '第 2 项' in resp.get_json()['error']
    assert config.load()['federationRoots'] == before
    # 未写入：原有目录仍生效
    assert _newFolder(client, root, 'still-ok').status_code == 200


@pytest.mark.parametrize('payload', [None, 'nope', 42, [1], [None], ['']])
def test_rejects_bad_shape(client, payload):
    resp = client.post('/api/settings/federation/roots', json={'roots': payload})
    assert resp.status_code == 400
    assert config.load()['federationRoots'] == []


def test_rejects_too_many_roots(client, tmp_path):
    roots = [str(tmp_path / ('d%d' % i)) for i in range(fw.MAX_ROOTS + 1)]
    resp = client.post('/api/settings/federation/roots', json={'roots': roots})
    assert resp.status_code == 400
    assert config.load()['federationRoots'] == []


def test_dedupe_and_normalize(client, tmp_path):
    root = tmp_path / 'share'
    root.mkdir()
    same = str(root)
    data = client.post('/api/settings/federation/roots',
                       json={'roots': [same, same + os.sep]}).get_json()
    assert data['roots'] == [fw.normalizePath(same)]


def test_rejects_existing_file_as_root(client, tmp_path):
    """文件不能当可写根：只校验形状会让写端点把文件当目录用。"""
    path = tmp_path / 'a.txt'
    path.write_text('x', encoding='utf-8')
    resp = client.post('/api/settings/federation/roots',
                       json={'roots': [str(path)]})
    assert resp.status_code == 400
    assert '目录' in resp.get_json()['error']
    assert config.load()['federationRoots'] == []


def test_allows_missing_root(client, tmp_path):
    """不存在的目录仍放行：目录可能还没建，UNC 共享也可能临时离线。"""
    missing = tmp_path / 'not-created-yet'
    resp = client.post('/api/settings/federation/roots',
                       json={'roots': [str(missing)]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['roots'] == [fw.normalizePath(str(missing))]


def test_unc_root_accepted_without_probing_share(client, monkeypatch):
    """UNC 根不做存在性探测：离线共享一次 stat/isdir 也会等网络超时。"""
    probed = []
    realExists, realIsdir = os.path.exists, os.path.isdir

    def spyExists(path):
        probed.append(str(path))
        return realExists(path)

    def spyIsdir(path):
        probed.append(str(path))
        return realIsdir(path)

    monkeypatch.setattr(fw.os.path, 'exists', spyExists)
    monkeypatch.setattr(fw.os.path, 'isdir', spyIsdir)
    resp = client.post('/api/settings/federation/roots',
                       json={'roots': [r'\\nas\team']})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['roots'] == [r'\\nas\team']
    assert not [p for p in probed if p.startswith('\\\\')]
    assert fw.isExistingFile(r'\\nas\team') is False


def test_roots_api_requires_login():
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        resp = c.post('/api/settings/federation/roots', json={'roots': []})
        assert resp.status_code == 401
    assert config.load()['federationRoots'] == []