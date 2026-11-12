# -*- coding: utf-8 -*-
"""成员端联邦写白名单：空根一律 403、根内放行、根外/.. /软链/UNC 拒绝。"""
import os

import pytest

import config
from modules import events, federation_write as fw


@pytest.fixture()
def cfg(monkeypatch):
    state = {'roots': []}
    monkeypatch.setattr(config, 'load',
                        lambda: {'federationRoots': list(state['roots'])})
    return state


@pytest.fixture()
def client(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def _fed(extra=None):
    headers = {'X-Deskport-From': 'hub', 'X-Deskport-Actor': '10.0.0.5'}
    headers.update(extra or {})
    return headers


# ---------------------------------------------------------------- 单元

def test_empty_roots_rejects_every_path(cfg, tmp_path):
    ok, why = fw.checkPaths([str(tmp_path / 'a.txt')])
    assert not ok and 'federationRoots' in why
    assert fw.roots() == []


def test_paths_inside_and_outside_root(cfg, tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    cfg['roots'] = [str(root)]
    assert fw.checkPaths([str(root / 'a.txt')])[0]
    assert not fw.checkPaths([str(tmp_path / 'outside.txt')])[0]


def test_dotdot_escape_rejected(cfg, tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    cfg['roots'] = [str(root)]
    escape = str(root / '..' / 'evil.txt')
    assert not fw.checkPaths([escape])[0]


def test_drive_letter_case_insensitive(cfg, tmp_path):
    root = tmp_path / 'Root'
    root.mkdir()
    cfg['roots'] = [str(root)]
    assert fw.checkPaths([str(root).upper() + '\\x.txt'])[0]


@pytest.mark.skipif(os.name != 'nt', reason='Windows 长路径前缀')
def test_long_path_prefix_is_stripped(cfg, tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    cfg['roots'] = [str(root)]
    longPath = '\\\\?\\' + str(root / 'x.txt')
    assert fw.checkPaths([longPath])[0]


def test_unc_path_outside_roots_rejected(cfg, tmp_path):
    cfg['roots'] = [str(tmp_path)]
    assert not fw.checkPaths([r'\\server\share\evil.txt'])[0]


def test_unc_root_matches_its_share(cfg):
    cfg['roots'] = [r'\\server\share']
    assert fw.checkPaths([r'\\server\share\sub\ok.txt'])[0]
    assert not fw.checkPaths([r'\\server\other\evil.txt'])[0]


def test_symlink_escape_rejected(cfg, tmp_path, monkeypatch):
    """用 realpath 替身模拟软链：根内链接解析到根外，必须 403。"""
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    cfg['roots'] = [str(root)]
    realRealpath = os.path.realpath

    def fakeRealpath(path):
        if os.path.normcase(os.path.abspath(path)).startswith(
                os.path.normcase(str(root / 'link'))):
            return str(outside / 'evil.txt')
        return realRealpath(path)

    monkeypatch.setattr(fw.os.path, 'realpath', fakeRealpath)
    assert not fw.checkPaths([str(root / 'link' / 'evil.txt')])[0]


def test_relative_and_device_paths_rejected(cfg, tmp_path):
    cfg['roots'] = [str(tmp_path)]
    for bad in ('relative\\x.txt', 'C:relative.txt', '\\\\.\\PhysicalDrive0'):
        assert not fw.checkPaths([bad])[0], bad


def test_bad_roots_are_skipped(cfg):
    cfg['roots'] = ['relative', '', 42, None]
    assert fw.roots() == []


def test_upload_limit_default_and_invalid(monkeypatch):
    monkeypatch.setattr(config, 'load', lambda: {})
    assert fw.uploadLimitMB() == 2048
    monkeypatch.setattr(config, 'load', lambda: {'federationUploadMaxMB': 'x'})
    assert fw.uploadLimitMB() == 2048
    monkeypatch.setattr(config, 'load', lambda: {'federationUploadMaxMB': 10})
    assert fw.uploadLimitMB() == 10


# ---------------------------------------------------------------- 集成

def _setRoots(monkeypatch, root):
    monkeypatch.setattr(fw, 'roots', lambda: [str(root)])


def test_member_write_with_empty_roots_403(client, tmp_path, monkeypatch):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    monkeypatch.setattr(fw, 'roots', lambda: [])
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'new', 'force': True}, headers=_fed())
    assert resp.status_code == 403
    assert path.read_bytes() == b'old'


def test_member_rename_outside_root_403(client, tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'x')
    _setRoots(monkeypatch, root)
    resp = client.post('/api/files/rename', json={
        'path': str(outside), 'newName': 'moved.txt'}, headers=_fed())
    assert resp.status_code == 403
    assert outside.exists()


def test_member_rename_inside_root_ok(client, tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    path = root / 'a.txt'
    path.write_bytes(b'x')
    _setRoots(monkeypatch, root)
    resp = client.post('/api/files/rename', json={
        'path': str(path), 'newName': 'b.txt'}, headers=_fed())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert (root / 'b.txt').exists()
    assert not path.exists()


def test_member_new_folder_name_escape_rejected(client, tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    _setRoots(monkeypatch, root)
    resp = client.post('/api/files/new-folder', json={
        'path': str(root), 'name': '..'}, headers=_fed())
    assert resp.status_code == 403
    assert sorted(p.name for p in tmp_path.iterdir()) == ['nav', 'root']


def test_member_recycle_paths_outside_root_403(client, tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    inside = root / 'a.txt'
    inside.write_bytes(b'a')
    outside = tmp_path / 'b.txt'
    outside.write_bytes(b'b')
    _setRoots(monkeypatch, root)
    resp = client.post('/api/files/recycle-delete', json={
        'paths': [str(inside), str(outside)]}, headers=_fed())
    assert resp.status_code == 403
    assert outside.exists()


def test_member_paste_source_outside_root_403(client, tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'src.txt'
    outside.write_bytes(b's')
    _setRoots(monkeypatch, root)
    resp = client.post('/api/files/paste', json={
        'mode': 'move', 'paths': [str(outside)], 'dest': str(root)}, headers=_fed())
    assert resp.status_code == 403
    assert outside.exists()


def test_member_permanent_delete_never_allowed(client, tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    path = root / 'a.txt'
    path.write_bytes(b'x')
    _setRoots(monkeypatch, root)
    resp = client.post('/api/files/delete', json={'paths': [str(path)]},
                       headers=_fed())
    assert resp.status_code == 403
    assert path.exists()


@pytest.mark.parametrize('endpoint', [
    ('/api/files/extract', {'archivePath': 'x.zip'}),
    ('/api/files/compress', {'paths': ['x'], 'parent': 'y', 'name': 'z'}),
    ('/api/files/zip', {'paths': ['x']}),
    ('/api/files/remote-download', {'url': 'http://x', 'path': 'y'}),
    ('/api/files/start', {'path': 'x'}),
    ('/api/files/pins', {'pins': []}),
])
def test_member_forbidden_endpoints(client, endpoint, monkeypatch, tmp_path):
    _setRoots(monkeypatch, tmp_path)
    path, body = endpoint
    resp = client.post(path, json=body, headers=_fed())
    assert resp.status_code == 403, path


def test_member_upload_tempdir_rejected(client, tmp_path, monkeypatch):
    _setRoots(monkeypatch, tmp_path)
    import io
    resp = client.post('/api/files/upload', headers=_fed(),
                       data={'path': str(tmp_path), 'tempdir': 'true',
                             'file': (io.BytesIO(b'x'), 'a.txt')},
                       content_type='multipart/form-data')
    assert resp.status_code == 403


def test_member_audit_includes_from_and_actor(client, tmp_path, monkeypatch):
    audit = []
    monkeypatch.setattr(events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    _setRoots(monkeypatch, tmp_path)
    (tmp_path / 'a.txt').write_bytes(b'x')
    resp = client.post('/api/files/rename', json={
        'path': str(tmp_path / 'a.txt'), 'newName': 'b.txt'}, headers=_fed())
    assert resp.status_code == 200
    entry = [a for a in audit if a['action'] == 'files.federation'][-1]
    assert entry['target'] == '来自 hub'
    assert 'actor=10.0.0.5' in entry['detail']
    assert 'from=hub' in entry['detail']