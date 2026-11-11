# -*- coding: utf-8 -*-
"""R5-3 sftp/put mkdirs：本机与 SSH 主机均可自动创建父目录；不带 mkdirs
时父目录缺失返回干净 404（不含内部临时文件名）。"""
import base64
import hashlib
import json

import pytest

import config as configMod
from modules import events as eventsMod
from test_agent_api import FakeSftp, TOKEN, installSftp
H = {'X-Agent-Token': TOKEN}

B64 = lambda b: base64.b64encode(b).decode()   # noqa: E731


@pytest.fixture
def env(tmp_path, monkeypatch):
    """同 test_agent_api.env：令牌配置 + 审计捕获 + 装配好的 client。"""
    from types import SimpleNamespace
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(
                                      TOKEN.encode()).hexdigest()}])
    audit = []
    monkeypatch.setattr(eventsMod, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit)


class MkdirFakeSftp(FakeSftp):
    """补 mkdir：记录调用并登记目录。"""

    def __init__(self, files=None):
        super().__init__(files)
        self.mkdirCalls = []

    def mkdir(self, path):
        self.mkdirCalls.append(path)
        self.dirs.add(path)


# ---------------------------------------------------------------- 本机

def test_local_put_missing_parent_clean_404(env, tmp_path):
    target = tmp_path / 'sub' / 'dir' / 'x.md'
    resp = env.client.post('/api/agent/local/sftp/put', headers=H,
                           data=json.dumps({'path': str(target),
                                            'contentB64': B64(b'hi')}),
                           content_type='application/json')
    assert resp.status_code == 404
    body = resp.get_json()
    assert body['error'].startswith('父目录不存在：')
    assert 'mkdirs' in body['hint']
    assert '.deskport-put' not in json.dumps(body)   # 不暴露临时文件名
    assert not target.exists()


def test_local_put_mkdirs_creates_parents(env, tmp_path):
    target = tmp_path / 'sub' / 'dir' / 'x.md'
    resp = env.client.post('/api/agent/local/sftp/put', headers=H,
                           data=json.dumps({'path': str(target),
                                            'contentB64': B64(b'hi'),
                                            'mkdirs': True}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert target.read_bytes() == b'hi'
    assert target.parent.is_dir()


def test_local_put_mkdirs_on_existing_parent_noop(env, tmp_path):
    target = tmp_path / 'x.md'
    target.write_bytes(b'old')
    resp = env.client.post('/api/agent/local/sftp/put', headers=H,
                           data=json.dumps({'path': str(target),
                                            'contentB64': B64(b'new'),
                                            'mkdirs': True}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert target.read_bytes() == b'new'


def test_sftp_route_local_host_also_supports_mkdirs(env, tmp_path):
    target = tmp_path / 'a' / 'b' / 'x.md'
    resp = env.client.post('/api/agent/sftp/put', headers=H,
                           data=json.dumps({'host': 'local', 'path': str(target),
                                            'contentB64': B64(b'deep'),
                                            'mkdirs': True}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert target.read_bytes() == b'deep'


# ---------------------------------------------------------------- SSH 主机

def test_ssh_put_missing_parent_clean_404(env, monkeypatch):
    fake = MkdirFakeSftp()
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)
    resp = env.client.post('/api/agent/sftp/put', headers=H,
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/new/x.md',
                                            'contentB64': B64(b'hi')}),
                           content_type='application/json')
    assert resp.status_code == 404
    body = resp.get_json()
    assert body['error'] == '父目录不存在：/srv/new'
    assert 'mkdirs' in body['hint']
    assert '.deskport-put' not in json.dumps(body)
    assert fake.mkdirCalls == []                     # 未带 mkdirs 不动远端


def test_ssh_put_mkdirs_creates_remote_dirs(env, monkeypatch):
    fake = MkdirFakeSftp()
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)
    resp = env.client.post('/api/agent/sftp/put', headers=H,
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/a/b/c/x.md',
                                            'contentB64': B64(b'hi'),
                                            'mkdirs': True}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert fake.files['/srv/a/b/c/x.md'] == b'hi'
    assert fake.mkdirCalls == ['/srv/a', '/srv/a/b', '/srv/a/b/c']
    # 已存在的 /srv 不重复 mkdir
    assert '/srv' not in fake.mkdirCalls


def test_ssh_put_existing_parent_still_works(env, monkeypatch):
    fake = MkdirFakeSftp()
    fake.dirs.add('/srv')
    fake.files['/srv/x.md'] = b'old'
    installSftp(monkeypatch, fake)
    resp = env.client.post('/api/agent/sftp/put', headers=H,
                           data=json.dumps({'host': 'office',
                                            'path': '/srv/x.md',
                                            'contentB64': B64(b'new')}),
                           content_type='application/json')
    assert resp.status_code == 200
    assert fake.files['/srv/x.md'] == b'new'
    assert fake.mkdirCalls == []


def test_ssh_put_mkdirs_failed_is_clean_404(env, monkeypatch):
    fake = MkdirFakeSftp()

    def statFail(path):
        if path.startswith('/locked'):
            raise IOError(2, 'No such file')
        return FakeSftp.stat(fake, path)

    fake.stat = statFail
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)
    resp = env.client.post('/api/agent/sftp/put', headers=H,
                           data=json.dumps({'host': 'office',
                                            'path': '/locked/x.md',
                                            'contentB64': B64(b'hi'),
                                            'mkdirs': True}),
                           content_type='application/json')
    assert resp.status_code == 404
    assert '自动创建失败' in resp.get_json()['error']
    assert '.deskport-put' not in json.dumps(resp.get_json())
