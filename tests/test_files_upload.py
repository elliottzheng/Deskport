# -*- coding: utf-8 -*-
"""文件上传: 目录校验与根视图拒绝。

背景: 用户在「此电脑」视图 (currentPath='/') 点上传, 前端把 path='/' 发给后端,
后端直接 400「无效的保存目录」—— 用户看到的就是"没反应"。前端已禁用该视图的
上传按钮, 后端这里补一层明确校验 (相对路径曾落到守护进程 cwd, 写成 500)。
"""
import io
import os

import pytest


@pytest.fixture()
def client(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['authenticated'] = True
    return c


def _post(client, path, filename='probe.txt', content=b'x', headers=None):
    data = {'file': (io.BytesIO(content), filename)}
    if path is not None:
        data['path'] = path
    return client.post('/api/files/upload', data=data, headers=headers,
                       content_type='multipart/form-data')


def test_upload_to_real_dir_ok(client, tmp_path):
    target = tmp_path / 'dest'
    target.mkdir()
    r = _post(client, str(target))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert (target / 'probe.txt').read_bytes() == b'x'


def test_upload_root_view_rejected(client):
    """「此电脑」根视图: 明确 400, 而不是让用户选完文件才失败。"""
    r = _post(client, '/')
    assert r.status_code == 400
    assert '无效的保存目录' in r.get_json()['error']


def test_upload_empty_path_rejected(client):
    r = _post(client, '')
    assert r.status_code == 400


def test_upload_dot_path_rejected(client):
    """'.' 会落到守护进程 cwd, 必须拒绝。"""
    r = _post(client, '.')
    assert r.status_code == 400
    assert '无效的保存目录' in r.get_json()['error']


def test_upload_relative_path_rejected(client):
    """相对路径曾表现为 500 No such file, 且可能写在错误位置。"""
    r = _post(client, 'Downloads')
    assert r.status_code == 400
    assert '绝对路径' in r.get_json()['error']


def test_upload_missing_dir_rejected(client, tmp_path):
    r = _post(client, str(tmp_path / 'nope'))
    assert r.status_code == 400
    assert '不存在' in r.get_json()['error']


def test_upload_no_file_rejected(client, tmp_path):
    target = tmp_path / 'dest'
    target.mkdir()
    r = client.post('/api/files/upload', data={'path': str(target)},
                    content_type='multipart/form-data')
    assert r.status_code == 400


def test_upload_requires_login(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    with app.test_client() as c:
        r = _post(c, str(tmp_path))
        assert r.status_code == 401


# ---------------------------------------------------------------- 原子落盘

def test_upload_same_name_gets_suffix_and_keeps_original(client, tmp_path):
    target = tmp_path / 'dest'
    target.mkdir()
    (target / 'probe.txt').write_bytes(b'old')
    r = _post(client, str(target), content=b'new')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert (target / 'probe.txt').read_bytes() == b'old'
    assert (target / 'probe(2).txt').read_bytes() == b'new'


def test_upload_filename_path_components_stripped(client, tmp_path):
    target = tmp_path / 'dest'
    target.mkdir()
    r = _post(client, str(target), filename=r'..\..\evil.txt', content=b'x')
    assert r.status_code == 200
    saved = r.get_json()['path']
    assert os.path.dirname(saved) == str(target)          # 没有写到父目录
    assert not (tmp_path / 'evil.txt').exists()


class _BoomStream:
    def __init__(self, exc):
        self.exc = exc

    def read(self, size=-1):
        raise self.exc


class _FakeFile:
    def __init__(self, exc):
        self.filename = 'x.bin'
        self.stream = _BoomStream(exc)


def test_interrupted_upload_oserror_leaves_no_part(tmp_path):
    from modules import files_transfer
    target, failure = files_transfer._saveUpload(
        _FakeFile(OSError('client disconnected')), str(tmp_path))
    assert target is None and failure[1] == 500
    assert list(tmp_path.iterdir()) == []   # 没有 .part，也没有半成品


def test_interrupted_upload_runtime_error_leaves_no_part(tmp_path):
    from modules import files_transfer
    with pytest.raises(RuntimeError):
        files_transfer._saveUpload(_FakeFile(RuntimeError('boom')), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_federated_upload_over_limit_leaves_no_part(client, tmp_path, monkeypatch):
    from modules import federation_write
    monkeypatch.setattr(federation_write, 'roots', lambda: [str(tmp_path)])
    monkeypatch.setattr(federation_write, 'uploadLimitMB', lambda: 1)
    payload = b'x' * (1024 * 1024 + 1)
    r = _post(client, str(tmp_path), content=payload,
              headers={'X-Deskport-From': 'hub'})
    assert r.status_code == 413
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == []
