# -*- coding: utf-8 -*-
"""agent_sftp 写操作: delete / mkdir / rename（本机 + SSH 桩，带审计）。"""
import errno
import hashlib
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events
from test_agent_api import TOKEN, FakeSftp, errnoErr, installSftp

H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(
                                      TOKEN.encode()).hexdigest()}])
    audit = []
    monkeypatch.setattr(events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit,
                           monkeypatch=monkeypatch)


def _post(env, path, body):
    return env.client.post(path, headers=H, data=json.dumps(body),
                           content_type='application/json')


class WriteFake(FakeSftp):
    def __init__(self, files=None):
        super().__init__(files)
        self.mkdirCalls = []

    def mkdir(self, path):
        if path in self.dirs:
            raise IOError(errno.EEXIST, 'File exists')
        self.mkdirCalls.append(path)
        self.dirs.add(path)

    def rmdir(self, path):
        children = list(self.files) + list(self.dirs)
        if any(p.startswith(path.rstrip('/') + '/') for p in children):
            raise IOError(errno.ENOTEMPTY, 'Directory not empty')
        self.dirs.discard(path)

    def rename(self, old, new):
        if new in self.files or new in self.dirs:
            raise IOError(errno.EEXIST, 'File exists')
        if old in self.files:
            self.files[new] = self.files.pop(old)
        elif old in self.dirs:
            self.dirs.discard(old)
            self.dirs.add(new)
        else:
            raise IOError(errnoErr('missing'))


# ---------------------------------------------------------------- 本机

def test_local_mkdir_and_conflict(env, tmp_path):
    target = tmp_path / 'a' / 'b'
    r = _post(env, '/api/agent/sftp/mkdir',
              {'host': 'local', 'path': str(target), 'parents': True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert target.is_dir()
    r2 = _post(env, '/api/agent/sftp/mkdir',
               {'host': 'local', 'path': str(target)})
    assert r2.status_code == 409


def test_local_delete_requires_recursive_for_nonempty_dir(env, tmp_path):
    folder = tmp_path / 'd'
    folder.mkdir()
    (folder / 'x.txt').write_bytes(b'x')
    r = _post(env, '/api/agent/sftp/delete',
              {'host': 'local', 'path': str(folder)})
    assert r.status_code == 409
    assert folder.exists()
    r2 = _post(env, '/api/agent/sftp/delete',
               {'host': 'local', 'path': str(folder), 'recursive': True})
    assert r2.status_code == 200
    assert not folder.exists()


def test_local_delete_missing_is_404(env, tmp_path):
    r = _post(env, '/api/agent/sftp/delete',
              {'host': 'local', 'path': str(tmp_path / 'nope')})
    assert r.status_code == 404


def test_local_rename_conflict_and_overwrite(env, tmp_path):
    src = tmp_path / 'a.txt'
    src.write_bytes(b'a')
    dst = tmp_path / 'b.txt'
    dst.write_bytes(b'b')
    r = _post(env, '/api/agent/sftp/rename',
              {'host': 'local', 'from': str(src), 'to': str(dst)})
    assert r.status_code == 409
    assert dst.read_bytes() == b'b'
    r2 = _post(env, '/api/agent/sftp/rename',
               {'host': 'local', 'from': str(src), 'to': str(dst),
                'overwrite': True})
    assert r2.status_code == 200
    assert dst.read_bytes() == b'a'


def test_local_ops_are_audited(env, tmp_path):
    target = tmp_path / 'x.txt'
    target.write_bytes(b'x')
    _post(env, '/api/agent/sftp/rename',
          {'host': 'local', 'from': str(target), 'to': str(tmp_path / 'y.txt')})
    actions = [a['action'] for a in env.audit]
    assert 'agent.sftp.rename' in actions


# ---------------------------------------------------------------- SSH

def test_ssh_delete_mkdir_rename(env, monkeypatch):
    fake = WriteFake({'/srv/a.txt': b'a'})
    fake.dirs.add('/srv')
    installSftp(monkeypatch, fake)

    r = _post(env, '/api/agent/sftp/mkdir',
              {'host': 'office', 'path': '/srv/new'})
    assert r.status_code == 200
    assert '/srv/new' in fake.dirs

    r = _post(env, '/api/agent/sftp/rename',
              {'host': 'office', 'from': '/srv/a.txt', 'to': '/srv/b.txt'})
    assert r.status_code == 200
    assert fake.files.get('/srv/b.txt') == b'a'

    r = _post(env, '/api/agent/sftp/delete',
              {'host': 'office', 'path': '/srv/b.txt'})
    assert r.status_code == 200
    assert '/srv/b.txt' not in fake.files
    assert 'agent.sftp.delete' in [a['action'] for a in env.audit]


def test_ssh_delete_nonempty_dir_needs_recursive(env, monkeypatch):
    fake = WriteFake({'/srv/d/x.txt': b'x'})
    fake.dirs.update({'/srv', '/srv/d'})
    installSftp(monkeypatch, fake)
    r = _post(env, '/api/agent/sftp/delete',
              {'host': 'office', 'path': '/srv/d'})
    assert r.status_code == 409
    assert '/srv/d' in fake.dirs
    r2 = _post(env, '/api/agent/sftp/delete',
               {'host': 'office', 'path': '/srv/d', 'recursive': True})
    assert r2.status_code == 200
    assert '/srv/d' not in fake.dirs
    assert '/srv/d/x.txt' not in fake.files


def test_requires_agent_token(env):
    r = env.client.post('/api/agent/sftp/delete', json={'host': 'local',
                                                       'path': 'x'})
    assert r.status_code == 401