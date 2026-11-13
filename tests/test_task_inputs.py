# -*- coding: utf-8 -*-
"""跨机附件投递（批次 B）：远端目录、原子性、上限、重名与两层开关。

SSH 通道用内存假 SFTP（不连真机）；联邦通道只测路由层的两层开关与
成员端根白名单声明——真实上传的建任务路径在 tests/test_task_inputs_api.py，
失败清理（倒序删 / 残留审计 / 清理异常不掩盖原始错误）在
tests/test_task_inputs_cleanup.py。
"""
import posixpath
from types import SimpleNamespace

import pytest

import modules.agent_nodes as nodesMod
from modules import task_inputs
from modules.task_inputs_base import DeliverError

HOME = '/home/u/proj'


def _norm(path):
    return posixpath.normpath(str(path).replace('\\', '/'))


class _Writer(object):
    def __init__(self, fake, path):
        self.fake = fake
        self.path = path
        self.buf = b''

    def write(self, data):
        self.buf += data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fake.commit(self.path, self.buf)
        return False


class FakeSftp(object):
    """内存远端：dirs 集合 + files 字典；failNames 命中即模拟投递失败。"""

    def __init__(self):
        self.dirs = {'/'}
        self.files = {}
        self.failNames = set()
        self.closed = False

    def _fail(self, path):
        name = posixpath.basename(path).split('.deskport-part')[0]
        if name in self.failNames:
            raise IOError('模拟投递失败: ' + name)

    def commit(self, path, data):
        self._fail(path)
        self.files[_norm(path)] = data
        self.dirs.add(posixpath.dirname(_norm(path)))

    def close(self):
        self.closed = True

    def stat(self, path):
        p = _norm(path)
        if p in self.dirs:
            return SimpleNamespace(st_mode=0o040755, st_size=0)
        if p in self.files:
            return SimpleNamespace(st_mode=0o100644, st_size=len(self.files[p]))
        raise IOError('no such file')

    def mkdir(self, path):
        p = _norm(path)
        if p in self.dirs:
            raise IOError('exists')
        self.dirs.add(p)

    def open(self, path, mode):
        assert mode == 'wb'
        return _Writer(self, path)

    def put(self, localPath, remotePath):
        self._fail(remotePath)
        with open(localPath, 'rb') as f:
            self.files[_norm(remotePath)] = f.read()
        self.dirs.add(posixpath.dirname(_norm(remotePath)))

    def posix_rename(self, src, dst):
        self.files[_norm(dst)] = self.files.pop(_norm(src))

    rename = posix_rename

    def remove(self, path):
        self.files.pop(_norm(path), None)

    def rmdir(self, path):
        p = _norm(path)
        if any(x.startswith(p.rstrip('/') + '/') for x in
               list(self.files) + list(self.dirs)):
            raise IOError('not empty')
        self.dirs.discard(p)

    def listdir_attr(self, path):
        base = _norm(path)
        prefix = '/' if base == '/' else base + '/'
        out = []
        for p, data in self.files.items():
            if p.startswith(prefix) and '/' not in p[len(prefix):]:
                out.append(SimpleNamespace(filename=p[len(prefix):],
                                           st_mode=0o100644, st_size=len(data)))
        for d in self.dirs:
            if d != base and d.startswith(prefix) and '/' not in d[len(prefix):]:
                out.append(SimpleNamespace(filename=d[len(prefix):],
                                           st_mode=0o040755, st_size=0))
        return out


@pytest.fixture
def fake(monkeypatch):
    fake = FakeSftp()
    fake.dirs.add(HOME)
    import modules.task_inputs_sftp as sftpMod
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'via': 'h1'})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    monkeypatch.setattr(sftpMod, 'viaHost', lambda via: {'id': 'h1'})
    monkeypatch.setattr(sftpMod, '_openSftp', lambda hid: fake)
    return fake


def _local(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_deliver_files_to_task_dir(fake, tmp_path):
    a = _local(tmp_path, 'a.txt', b'A')
    b = _local(tmp_path, 'b.txt', b'B')
    out = task_inputs.deliver('ai-xmyz', HOME, 'task1', [a, b])
    assert out['dir'] == HOME + '/.deskport-inputs/task1'
    assert out['paths'] == [out['dir'] + '/a.txt', out['dir'] + '/b.txt']
    assert fake.files[out['dir'] + '/a.txt'] == b'A'
    assert fake.files[out['dir'] + '/b.txt'] == b'B'
    assert fake.closed is True


def test_dirFor_matches_delivery_dir(fake, tmp_path):
    a = _local(tmp_path, 'a.txt', b'A')
    out = task_inputs.deliver('ai-xmyz', HOME, 'task2', [a])
    assert task_inputs.dirFor('ai-xmyz', HOME, 'task2') == out['dir']


def test_attachments_are_written_as_bytes(fake):
    out = task_inputs.deliver('ai-xmyz', HOME, 'task3', [], [
        {'name': 'x.bin', 'contentB64': 'eHl6'}])
    assert fake.files[out['dir'] + '/x.bin'] == b'xyz'


def test_same_name_gets_2_no_overwrite(fake, tmp_path):
    one = tmp_path / 'one'
    two = tmp_path / 'two'
    one.mkdir()
    two.mkdir()
    a = one / 'same.txt'
    a.write_bytes(b'one')
    b = two / 'same.txt'
    b.write_bytes(b'two')
    outcome = task_inputs.deliver('ai-xmyz', HOME, 'task4', [str(a), str(b)])
    assert outcome['paths'][0].endswith('/same.txt')
    assert outcome['paths'][1].endswith('/same(2).txt')
    assert fake.files[outcome['paths'][0]] == b'one'
    assert fake.files[outcome['paths'][1]] == b'two'


def test_redelivery_keeps_single_task_dir(fake, tmp_path):
    a = _local(tmp_path, 'a.txt', b'A')
    first = task_inputs.deliver('ai-xmyz', HOME, 'task5', [a])
    second = task_inputs.deliver('ai-xmyz', HOME, 'task5', [a])
    assert first['dir'] == second['dir']
    assert second['paths'][0].endswith('a(2).txt')
    taskDirs = [d for d in fake.dirs if d.endswith('/task5')]
    assert taskDirs == [first['dir']]


def test_second_file_failure_leaves_no_residue(fake, tmp_path, monkeypatch):
    audit = []
    monkeypatch.setattr(task_inputs.events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append((action, result, detail)))
    a = _local(tmp_path, 'a.txt', b'A')
    b = _local(tmp_path, 'b.txt', b'B')
    fake.failNames = {'b.txt'}
    with pytest.raises(DeliverError) as err:
        task_inputs.deliver('ai-xmyz', HOME, 'task6', [a, b])
    assert err.value.status == 502                 # 原始错误仍透出
    assert 'b.txt' in str(err.value) and '模拟投递失败' in str(err.value)
    assert not [p for p in fake.files if '/task6/' in p]
    assert not [d for d in fake.dirs if d.endswith('/task6')]
    assert not [d for d in fake.dirs if d.endswith('/.deskport-inputs')]
    assert not audit                                # 清干净了就不该有清理审计


def test_taskId_rejects_dot_and_separators(fake, tmp_path):
    a = _local(tmp_path, 'a.txt', b'A')
    for bad in ('', '.', '..', 'a/b', 'a\\b'):
        with pytest.raises(DeliverError) as err:
            task_inputs.deliver('ai-xmyz', HOME, bad, [a])
        assert 'taskId' in str(err.value)
    assert fake.files == {}


def test_directory_input_keeps_structure(fake, tmp_path):
    src = tmp_path / '讲义'
    (src / 'sub').mkdir(parents=True)
    (src / 'a.txt').write_bytes(b'A')
    (src / 'sub' / 'b.txt').write_bytes(b'B')
    out = task_inputs.deliver('ai-xmyz', HOME, 'task7', [str(src)])
    assert out['paths'] == [HOME + '/.deskport-inputs/task7/讲义']
    assert fake.files[out['paths'][0] + '/a.txt'] == b'A'
    assert fake.files[out['paths'][0] + '/sub/b.txt'] == b'B'


def test_size_limit_rejected_before_transfer(fake, tmp_path, monkeypatch):
    import modules.task_inputs_sftp as sftpMod
    monkeypatch.setattr(sftpMod, 'federation_write',
                        SimpleNamespace(uploadLimitMB=lambda: 0))
    a = _local(tmp_path, 'big.bin', b'x' * 32)
    with pytest.raises(DeliverError) as err:
        task_inputs.deliver('ai-xmyz', HOME, 'task8', [a])
    assert err.value.status == 413 and '上限' in str(err.value)
    assert fake.files == {}


def test_empty_inputs_rejected(fake):
    with pytest.raises(DeliverError):
        task_inputs.deliver('ai-xmyz', HOME, 'task9', [], [])


def test_member_layer_messages(monkeypatch):
    member = {'id': 'win4710', 'name': 'win4710', 'base': 'http://x',
              'token': '', 'files': True, 'filesWrite': False}
    monkeypatch.setattr(nodesMod, 'findFederationMember',
                        lambda name: member)
    with pytest.raises(DeliverError) as err:
        task_inputs.dirFor('win4710', r'D:\work', 't1')
    assert err.value.status == 403 and 'filesWrite' in str(err.value)

    member = dict(member, filesWrite=True, files=False)
    with pytest.raises(DeliverError) as err:
        task_inputs.dirFor('win4710', r'D:\work', 't1')
    assert err.value.status == 403 and 'files' in str(err.value)


def test_member_root_whitelist_layer(monkeypatch):
    class FakeTarget(object):
        name = 'win4710'

        def __init__(self, member):
            pass

        def info(self):
            return {'maxUploadMB': 100, 'writeRootsConfigured': False}

        def close(self):
            pass

    monkeypatch.setattr(task_inputs, 'FedTarget', FakeTarget)
    member = {'id': 'win4710', 'name': 'win4710', 'base': 'http://x',
              'token': '', 'files': True, 'filesWrite': True}
    with pytest.raises(DeliverError) as err:
        task_inputs._openTarget('federation', member)
    assert err.value.status == 403
    assert 'federationRoots' in str(err.value)
