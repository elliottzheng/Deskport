# -*- coding: utf-8 -*-
"""SFTP 批量上传与递归建目录：FakeSftp + 端点级测试（不依赖真实 SSH）。"""
import io
import json
from contextlib import contextmanager

import pytest

from modules import ssh_sftp


class _WriteHandle:
    def __init__(self, store, path):
        self.store, self.path, self.buf = store, path, bytearray()

    def write(self, data):
        self.buf.extend(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.store[self.path] = bytes(self.buf)


class FakeSftp:
    """最小 SFTP 桩: 只实现被测路径需要的 stat/mkdir/open/normalize。"""

    def __init__(self):
        self.dirs = {'/'}
        self.files = {}

    def normalize(self, path):
        path = (path or '.').strip()
        if path in ('.', '', '/'):
            return '/'
        if not path.startswith('/'):
            path = '/' + path
        return path.rstrip('/') or '/'

    def stat(self, path):
        if path in self.dirs or path in self.files:
            return object()
        raise IOError('no such file: %s' % path)

    def mkdir(self, path):
        self.dirs.add(path)

    def open(self, path, mode='rb'):
        assert mode == 'wb', '测试只覆盖写模式'
        return _WriteHandle(self.files, path)


@pytest.fixture
def sftp_env(tmp_path, monkeypatch):
    fake = FakeSftp()

    @contextmanager
    def fakeSession(hostId):
        yield fake

    monkeypatch.setattr(ssh_sftp, '_sftpSession', fakeSession)

    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client, fake


def test_mkdir_creates_parents(sftp_env):
    client, fake = sftp_env
    resp = client.post('/api/ssh/sftp/mkdir',
                       data=json.dumps({'hostId': 'h', 'path': '/a/b/c'}),
                       content_type='application/json')
    assert resp.status_code == 200
    assert {'/a', '/a/b', '/a/b/c'} <= fake.dirs


def test_mkdir_requires_path(sftp_env):
    client, fake = sftp_env
    resp = client.post('/api/ssh/sftp/mkdir',
                       data=json.dumps({'hostId': 'h', 'path': '  '}),
                       content_type='application/json')
    assert resp.status_code == 400


def test_upload_batch_keeps_folder_structure(sftp_env):
    client, fake = sftp_env
    data = {
        'hostId': 'h',
        'remoteDir': '.',
        'paths': ['myproj/docs/x.txt', 'myproj/y.txt'],
    }
    resp = client.post('/api/ssh/sftp/upload-batch', data={
        **data,
        'files': [(io.BytesIO(b'hello'), 'x.txt'), (io.BytesIO(b'world'), 'y.txt')],
    }, content_type='multipart/form-data')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] and len(body['uploaded']) == 2 and not body['failed']
    assert fake.files['/myproj/docs/x.txt'] == b'hello'
    assert fake.files['/myproj/y.txt'] == b'world'
    assert '/myproj/docs' in fake.dirs


def test_upload_batch_rejects_escape_paths(sftp_env):
    client, fake = sftp_env
    resp = client.post('/api/ssh/sftp/upload-batch', data={
        'hostId': 'h', 'remoteDir': '.',
        'paths': ['../evil.txt'],
        'files': [(io.BytesIO(b'x'), 'evil.txt')],
    }, content_type='multipart/form-data')
    body = resp.get_json()
    assert body['ok'] is False
    assert body['failed'] and '非法路径' in body['failed'][0]['error']
    assert not any('evil' in p for p in fake.files)


def test_upload_batch_without_files_is_400(sftp_env):
    client, _fake = sftp_env
    resp = client.post('/api/ssh/sftp/upload-batch', data={
        'hostId': 'h', 'remoteDir': '.',
    }, content_type='multipart/form-data')
    assert resp.status_code == 400


def test_upload_batch_continues_after_io_error(sftp_env, monkeypatch):
    client, fake = sftp_env

    realEnsure = ssh_sftp._ensureRemoteDir

    def flaky(sftp, path):
        realEnsure(sftp, path)
        if path.endswith('/bad'):
            raise IOError('disk full')

    monkeypatch.setattr(ssh_sftp, '_ensureRemoteDir', flaky)

    resp = client.post('/api/ssh/sftp/upload-batch', data={
        'hostId': 'h', 'remoteDir': '.',
        'paths': ['bad/f1.txt', 'ok/f2.txt'],
        'files': [(io.BytesIO(b'1'), 'f1.txt'), (io.BytesIO(b'2'), 'f2.txt')],
    }, content_type='multipart/form-data')
    body = resp.get_json()
    assert body['ok'] is False
    assert len(body['uploaded']) == 1 and len(body['failed']) == 1
    assert fake.files['/ok/f2.txt'] == b'2'
