# -*- coding: utf-8 -*-
"""在线编辑乐观锁：read 带 mtime+sha256；write 版本不符 409 且不落盘。

本机与联邦走同一套校验（files_browse._versionDenied）；不带版本参数时
必须显式 force=true，且 force 只对本机开放。
"""
import hashlib

import pytest

from modules import federation_write


@pytest.fixture()
def client(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def _read(client, path):
    resp = client.post('/api/files/read', json={'path': str(path)})
    assert resp.status_code == 200
    return resp.get_json()


def test_read_returns_mtime_and_sha256(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'hello')
    data = _read(client, path)
    assert data['mtime'] == int(path.stat().st_mtime)
    assert data['sha256'] == hashlib.sha256(b'hello').hexdigest()


def test_write_with_matching_version_succeeds(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    data = _read(client, path)
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'new',
        'baseMtime': data['mtime'], 'baseSha256': data['sha256']})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert path.read_bytes() == b'new'
    assert body['sha256'] == hashlib.sha256(b'new').hexdigest()
    assert body['mtime'] == int(path.stat().st_mtime)


def test_write_without_version_is_400_and_unchanged(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    resp = client.post('/api/files/write', json={'path': str(path), 'content': 'new'})
    assert resp.status_code == 400
    assert 'baseMtime' in resp.get_json()['error']
    assert path.read_bytes() == b'old'


def test_write_mtime_mismatch_409_and_unchanged(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    current = _read(client, path)['mtime']
    stale = current - 100
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'new', 'baseMtime': stale})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['currentMtime'] == current
    assert path.read_bytes() == b'old'


def test_write_sha_mismatch_409_and_unchanged(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'new', 'baseSha256': '0' * 64})
    assert resp.status_code == 409
    assert resp.get_json()['currentMtime'] == int(path.stat().st_mtime)
    assert path.read_bytes() == b'old'


def test_write_force_allowed_locally(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'forced', 'force': True})
    assert resp.status_code == 200
    assert path.read_bytes() == b'forced'


def test_write_force_rejected_for_federated_request(client, tmp_path, monkeypatch):
    """成员端即使根白名单放行，也不允许联邦请求用 force 绕过版本校验。"""
    monkeypatch.setattr(federation_write, 'roots', lambda: [str(tmp_path)])
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'forced', 'force': True},
        headers={'X-Deskport-From': 'hub', 'X-Deskport-Actor': '10.0.0.5'})
    assert resp.status_code == 403
    assert 'force' in resp.get_json()['error']
    assert path.read_bytes() == b'old'


def test_write_federated_with_version_inside_root(client, tmp_path, monkeypatch):
    monkeypatch.setattr(federation_write, 'roots', lambda: [str(tmp_path)])
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    data = _read(client, path)
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'new',
        'baseMtime': data['mtime'], 'baseSha256': data['sha256']},
        headers={'X-Deskport-From': 'hub', 'X-Deskport-Actor': '10.0.0.5'})
    assert resp.status_code == 200
    assert path.read_bytes() == b'new'