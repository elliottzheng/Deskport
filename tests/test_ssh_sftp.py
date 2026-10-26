# -*- coding: utf-8 -*-
"""SFTP 端点级测试：FakeSftp 覆盖批量上传/建目录/下载/打包（不依赖真实 SSH）。"""
import io
import json
import posixpath
import stat as statMod
import zipfile
from contextlib import contextmanager
from urllib.parse import quote

import paramiko
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


class _ReadHandle:
    def __init__(self, store, path):
        self.data = store.get(path, b'')
        self.pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            data = self.data[self.pos:]
            self.pos = len(self.data)
            return data
        data = self.data[self.pos:self.pos + size]
        self.pos += len(data)
        return data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeSftp:
    """最小 SFTP 桩: stat/mkdir/open(读写)/listdir_attr/normalize。"""

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

    def _attr(self, path):
        attr = paramiko.SFTPAttributes()
        attr.filename = posixpath.basename(path.rstrip('/')) or path
        attr.st_size = len(self.files.get(path, b''))
        attr.st_mode = (statMod.S_IFDIR if path in self.dirs else statMod.S_IFREG) | 0o644
        return attr

    def stat(self, path):
        if path in self.dirs or path in self.files:
            return self._attr(path)
        raise IOError('no such file: %s' % path)

    def listdir_attr(self, path):
        base = self.normalize(path)
        out = []
        for p in sorted(set(self.dirs) | set(self.files)):
            if p != base and posixpath.dirname(p) == base:
                out.append(self._attr(p))
        return out

    def mkdir(self, path):
        self.dirs.add(path)

    def open(self, path, mode='rb'):
        if 'w' in mode or '+' in mode or 'a' in mode:
            return _WriteHandle(self.files, path)
        if path not in self.files:
            raise IOError('no such file: %s' % path)
        return _ReadHandle(self.files, path)


@pytest.fixture
def sftp_env(tmp_path, monkeypatch):
    fake = FakeSftp()

    @contextmanager
    def fakeSession(hostId):
        yield fake

    monkeypatch.setattr(ssh_sftp, '_sftpSession', fakeSession)
    monkeypatch.setattr(ssh_sftp, '_openSftp', lambda hostId: fake)

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


def test_download_single_chinese_name(sftp_env):
    """中文名下载: 头里必须出现 RFC5987 编码, 旧实现直接 500。"""
    client, fake = sftp_env
    fake.files['/报告.docx'] = b'PK-docx-bytes'
    resp = client.get('/api/ssh/sftp/download?hostId=h&path=' + quote('/报告.docx'))
    assert resp.status_code == 200
    assert resp.data == b'PK-docx-bytes'
    disp = resp.headers['Content-Disposition']
    assert "filename*=UTF-8''" in disp
    assert '%E6%8A%A5%E5%91%8A' in disp   # 「报告」的 UTF-8 百分号编码


def test_download_missing_is_404(sftp_env):
    client, fake = sftp_env
    fake.dirs.add('/d')
    resp = client.get('/api/ssh/sftp/download?hostId=h&path=' + quote('/d/nope.txt'))
    assert resp.status_code == 404


def test_download_zip_multi_and_folder(sftp_env):
    client, fake = sftp_env
    fake.dirs |= {'/sub', '/sub/deep'}
    fake.files['/a.txt'] = b'A'
    fake.files['/sub/b.txt'] = b'B'
    fake.files['/sub/deep/c.txt'] = b'C'

    paths = json.dumps(['/a.txt', '/sub'])
    resp = client.get('/api/ssh/sftp/download-zip?hostId=h&paths=' + quote(paths))
    assert resp.status_code == 200
    assert resp.data[:2] == b'PK'
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert 'a.txt' in names
    assert 'sub/b.txt' in names
    assert 'sub/deep/c.txt' in names
    assert zf.read('a.txt') == b'A'
    assert zf.read('sub/deep/c.txt') == b'C'
    assert 'zip' in resp.headers['Content-Type']


def test_zip_requires_paths(sftp_env):
    client, _fake = sftp_env
    resp = client.get('/api/ssh/sftp/download-zip?hostId=h')
    assert resp.status_code == 400


def test_zip_budget_exceeded_is_413(sftp_env, monkeypatch):
    client, fake = sftp_env
    fake.files['/big.bin'] = b'x' * 64
    monkeypatch.setattr(ssh_sftp, 'ZIP_SIZE_BUDGET', 16)

    paths = json.dumps(['/big.bin'])
    resp = client.get('/api/ssh/sftp/download-zip?hostId=h&paths=' + quote(paths))
    assert resp.status_code == 413
