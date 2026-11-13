# -*- coding: utf-8 -*-
"""失败清理：倒序删、删空目录、清理异常不掩盖原始错误（task_inputs）。

`/api/tasks` 与 share 的投递用例在 tests/test_task_inputs.py；这里只覆盖
"任一文件失败 → 已投部分清干净 + 原始错误仍然透出"。
"""
import os

import pytest

import modules.agent_nodes as nodesMod
from modules import task_inputs
from modules.task_inputs_base import DeliverError


def _local(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


class _StubTarget(object):
    """只实现 deliver 用到的接口；失败固定落在 failOn 这个文件名上。"""

    kind = 'federation'
    name = 'stub'

    def __init__(self, failOn='b.txt', cleanupError=None, leftovers=()):
        self.root = 'D:' + os.sep + 'work'
        self.failOn = failOn
        self.cleanupError = cleanupError
        self.leftovers = list(leftovers)
        self.cleaned = []

    def info(self):
        return {'maxUploadMB': 100, 'writeRootsConfigured': True}

    def join(self, *parts):
        return os.path.join(*[str(p) for p in parts if str(p)])

    def isdir(self, path):
        return os.path.normcase(path) == os.path.normcase(self.root)

    def realpath(self, path):
        return os.path.normpath(path)

    def ensureDir(self, path):
        return True

    def maxMB(self):
        return 100

    def putFile(self, localPath, name, directory, track=False):
        if name == self.failOn:
            raise DeliverError('执行点写入失败: 模拟成员端写失败: ' + name, 502)
        return self.join(directory, name)

    def cleanup(self, base, made):
        self.cleaned.append(base)
        if self.cleanupError is not None:
            raise self.cleanupError
        return self.leftovers

    def close(self):
        pass


def _stubDeliver(monkeypatch, target, tmp_path):
    member = {'id': 'win4710', 'name': 'win4710', 'base': 'http://x',
              'token': 't', 'files': True, 'filesWrite': True}
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: dict(member))
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: None)
    monkeypatch.setattr(task_inputs, 'FedTarget', lambda entry: target)
    a = _local(tmp_path, 'a.txt', b'A')
    b = _local(tmp_path, 'b.txt', b'B')
    return task_inputs.deliver('win4710', target.root, 'taskS', [a, b])


def test_cleanup_exception_does_not_mask_error(monkeypatch, tmp_path):
    target = _StubTarget(cleanupError=RuntimeError('回收站不可达'))
    audit = []
    monkeypatch.setattr(task_inputs.events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append((action, target, result, detail)))
    with pytest.raises(DeliverError) as err:
        _stubDeliver(monkeypatch, target, tmp_path)
    assert err.value.status == 502
    assert 'b.txt' in str(err.value) and '模拟成员端写失败' in str(err.value)
    assert len(target.cleaned) == 1
    cleanup = [a for a in audit if a[0] == 'task_inputs.cleanup']
    assert cleanup and cleanup[-1][2] == 'fail'
    assert 'RuntimeError' in cleanup[-1][3]


def test_cleanup_leftovers_are_audited(monkeypatch, tmp_path):
    leftover = 'D:\\work\\.deskport-inputs\\taskS\\a.txt'
    target = _StubTarget(leftovers=[leftover])
    audit = []
    monkeypatch.setattr(task_inputs.events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append((action, target, result, detail)))
    with pytest.raises(DeliverError) as err:
        _stubDeliver(monkeypatch, target, tmp_path)
    assert err.value.status == 502 and 'b.txt' in str(err.value)
    cleanup = [a for a in audit if a[0] == 'task_inputs.cleanup']
    assert cleanup and cleanup[-1][2] == 'partial'
    assert leftover in cleanup[-1][3]


class _FedMachine(object):
    """内存成员机：只实现 task_inputs_fed 用到的端点，回收站递归生效。"""

    def __init__(self, failName=''):
        self.root = 'D:' + os.sep + 'work'
        self.dirs = {self.root, 'D:' + os.sep}
        self.files = {}
        self.recycled = []
        self.failName = failName

    def _names(self, path):
        prefix = path if path.endswith(os.sep) else path + os.sep
        return {p[len(prefix):].lower()
                for p in list(self.dirs) + list(self.files)
                if p.lower().startswith(prefix.lower())
                and os.sep not in p[len(prefix):]}

    def call(self, method, path, timeout=0, params=None, json=None, data=None,
             files=None):
        if path == '/api/files':
            if params['path'] not in self.dirs:
                return 404, {}
            return 200, {'entries': [{'name': n}
                                     for n in self._names(params['path'])]}
        if path == '/api/files/new-folder':
            full = os.path.join(json['path'], json['name'])
            if full in self.dirs or full in self.files:
                return 409, {}
            self.dirs.add(full)
            return 200, {'path': full}
        if path == '/api/files/upload':
            name, payload = files['file'][0], files['file'][1].read()
            if name == self.failName:
                return 500, {'error': '模拟成员端写失败: ' + name}
            full = os.path.join(data['path'], name)
            self.files[full] = payload
            return 200, {'ok': True, 'path': full}
        if path == '/api/files/recycle-delete':
            for p in json['paths']:
                self.recycled.append(p)
                self._drop(p)
            return 200, {'ok': True}
        raise AssertionError(path)

    def _drop(self, path):
        """回收站：命中目录时整棵子树一起走。"""
        low = path.lower() + os.sep
        self.files.pop(path, None)
        self.dirs.discard(path)
        for p in [x for x in self.files if x.lower().startswith(low)]:
            self.files.pop(p)
        for d in [x for x in self.dirs if x.lower().startswith(low)]:
            self.dirs.discard(d)


def test_fed_second_file_failure_cleans_up_in_reverse(monkeypatch, tmp_path):
    import modules.task_inputs_fed as fedMod

    machine = _FedMachine(failName='c.txt')

    class Target(fedMod.FedTarget):
        def _call(self, *args, **kw):
            return machine.call(*args, **kw)

        def info(self):
            return {'maxUploadMB': 100, 'writeRootsConfigured': True}

    member = {'id': 'win4710', 'name': 'win4710', 'base': 'http://x',
              'token': 't', 'files': True, 'filesWrite': True}
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: dict(member))
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: None)
    monkeypatch.setattr(task_inputs, 'FedTarget', Target)
    audit = []
    monkeypatch.setattr(task_inputs.events, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append((action, result, detail)))

    a = _local(tmp_path, 'a.txt', b'A')
    b = _local(tmp_path, 'b.txt', b'B')
    c = _local(tmp_path, 'c.txt', b'C')
    with pytest.raises(DeliverError) as err:
        task_inputs.deliver('win4710', machine.root, 'taskF', [a, b, c])

    assert err.value.status == 502
    assert 'c.txt' in str(err.value) and '模拟成员端写失败' in str(err.value)
    base = os.path.join(machine.root, '.deskport-inputs', 'taskF')
    assert machine.recycled[:2] == [os.path.join(base, 'b.txt'),
                                    os.path.join(base, 'a.txt')]  # 倒序
    assert os.path.join(base, 'a.txt') not in machine.files
    assert not [d for d in machine.dirs if 'taskF' in d]
    assert not [d for d in machine.dirs if '.deskport-inputs' in d]
    assert not audit                            # 清干净了就不该有清理审计
