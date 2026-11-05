# -*- coding: utf-8 -*-
"""文件上传: 目录校验与根视图拒绝。

背景: 用户在「此电脑」视图 (currentPath='/') 点上传, 前端把 path='/' 发给后端,
后端直接 400「无效的保存目录」—— 用户看到的就是"没反应"。前端已禁用该视图的
上传按钮, 后端这里补一层明确校验 (相对路径曾落到守护进程 cwd, 写成 500)。
"""
import io

import pytest


@pytest.fixture()
def client(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['authenticated'] = True
    return c


def _post(client, path, filename='probe.txt', content=b'x'):
    data = {'file': (io.BytesIO(content), filename)}
    if path is not None:
        data['path'] = path
    return client.post('/api/files/upload', data=data,
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
