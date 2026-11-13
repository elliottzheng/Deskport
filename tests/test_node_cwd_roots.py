# -*- coding: utf-8 -*-
"""批次 B3：远端 cwd 的前置校验——执行点自报允许根（cwdRoots）。

node != local 且该节点 cwdRoots 非空时，cwd 必须落在某个根之下（按目标
OS 的路径语义比：Windows 大小写不敏感、POSIX 敏感）；cwdRoots 为空数组
= 未知，沿用 NODE-OS 的只查语法（向后兼容）。本机执行点不走这里——
task_cwd.resolve 管本机路径的存在性与越界（test_task_cwd.py 全绿即不变）。
"""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
import modules.agent_nodes as nodesMod
from modules import events as eventsMod
from modules import node_path, task_inputs, tasks_api
from modules import tasks_store as store
from modules.task_inputs_base import DeliverError
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'r' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def seedPlatform(monkeypatch, **entries):
    """把平台声明塞进 health 缓存（等价于 node /health 已上报）。"""
    data = {name: dict(info) for name, info in entries.items()}
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': data})


def stubNodes(monkeypatch, *names):
    """这些名字都解析成执行点（agent node 优先，成员随后）。"""
    members = set(names[1]) if len(names) > 1 else set()

    def findNode(name):
        return {'name': name} if name in names[0] else None

    def findMember(name):
        if name in members:
            return {'id': name, 'name': name, 'files': True,
                    'filesWrite': True, 'base': 'http://x', 'token': 't'}
        return None
    monkeypatch.setattr(nodesMod, 'findNode', findNode)
    monkeypatch.setattr(nodesMod, 'findFederationMember', findMember)


# ---------------------------------------------------------------- 建任务校验

def test_cwd_under_declared_root_accepted_outside_rejected(env, monkeypatch):
    """cwdRoots=[C:\\work]：根之下 202，根之外 400 且文案带允许根。"""
    seedPlatform(monkeypatch, **{'i7-4710MQ': {'os': 'windows',
                                               'cwdRoots': [r'C:\work']}})
    stubNodes(monkeypatch, ('i7-4710MQ',))
    tid = create(env, {'prompt': 'x', 'node': 'i7-4710MQ',
                       'cwd': r'C:\work\a'}).get_json()['task']['id']
    resp = create(env, {'prompt': 'x', 'node': 'i7-4710MQ', 'cwd': r'D:\x'})
    assert resp.status_code == 400
    error = resp.get_json()['error']
    assert r'D:\x' in error and 'i7-4710MQ' in error and r'C:\work' in error
    assert '不在执行点' in error and '允许：' in error
    with env.repo.lock:
        # 400 不建任务（仍只有前面 202 建的那一个）
        assert [t['id'] for t in env.repo.list()] == [tid]


def test_windows_roots_are_case_insensitive(env, monkeypatch):
    """Windows 执行点：C:\\WORK\\a 也放行（目标 OS 大小写不敏感）。"""
    seedPlatform(monkeypatch, **{'i7-4710MQ': {'os': 'windows',
                                               'cwdRoots': [r'C:\work']}})
    stubNodes(monkeypatch, ('i7-4710MQ',))
    assert create(env, {'prompt': 'x', 'node': 'i7-4710MQ',
                        'cwd': r'C:\WORK\a'}).status_code == 202
    # 根自身算命中；前缀必须落在目录边界（C:\\work 不命中 C:\\workx）
    assert create(env, {'prompt': 'x', 'node': 'i7-4710MQ',
                        'cwd': r'C:\work'}).status_code == 202
    assert create(env, {'prompt': 'x', 'node': 'i7-4710MQ',
                        'cwd': r'C:\workx'}).status_code == 400


def test_posix_roots_are_case_sensitive(env, monkeypatch):
    """linux 执行点：/SRV/app 不命中 /srv/app（POSIX 大小写敏感）。"""
    seedPlatform(monkeypatch, **{'ai-xmyz': {'os': 'linux',
                                             'cwdRoots': ['/srv/app']}})
    stubNodes(monkeypatch, ('ai-xmyz',))
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/srv/app/sub'}).status_code == 202
    resp = create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/SRV/app'})
    assert resp.status_code == 400
    assert '/SRV/app' in resp.get_json()['error']


def test_empty_roots_means_unknown_not_validated(env, monkeypatch):
    """cwdRoots 缺省或空数组 = 未知：两种 cwd 都放行（向后兼容）。"""
    stubNodes(monkeypatch, ('ai-xmyz',))
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/tmp'}).status_code == 202
    seedPlatform(monkeypatch, **{'ai-xmyz': {'os': 'linux',
                                             'cwdRoots': []}})
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/tmp'}).status_code == 202
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/srv/app'}).status_code == 202


def test_federation_member_roots_enforced(env, monkeypatch):
    """联邦成员同样按它自报的 cwdRoots 前置校验。"""
    seedPlatform(monkeypatch, **{'win4710': {'os': 'windows',
                                             'cwdRoots': [r'D:\work']}})
    stubNodes(monkeypatch, (), ('win4710',))
    assert create(env, {'prompt': 'x', 'node': 'win4710',
                        'cwd': r'D:\work\a'}).status_code == 202
    resp = create(env, {'prompt': 'x', 'node': 'win4710', 'cwd': r'C:\tmp'})
    assert resp.status_code == 400
    assert r'C:\tmp' in resp.get_json()['error'] and r'D:\work' in \
        resp.get_json()['error']


def test_local_node_rules_unchanged(env, monkeypatch):
    """本机执行点仍走 task_cwd.resolve（存在性 + 越界），不受 B3 影响。"""
    work = env.tmp / 'w'
    work.mkdir()
    (work / 'p').mkdir()
    monkeypatch.setattr(configMod, 'getWorkDirs', lambda: [str(work)])
    monkeypatch.setattr(configMod, 'getFallbackDir',
                        lambda: str(env.tmp / 'fb'))
    assert create(env, {'prompt': 'x',
                        'cwd': str(work / 'p')}).status_code == 202
    outside = env.tmp / 'outside'
    outside.mkdir()
    assert create(env, {'prompt': 'x', 'cwd': str(outside)}).status_code == 400
    # 本机 cwdRoots 取自 config（供 nodes 列表展示），不参与本机校验
    assert node_path.nodeCwdRoots('local') == [str(work),
                                               str(env.tmp / 'fb')]


# ---------------------------------------------------------------- 单元：node_path

def test_platform_entry_carries_cwd_roots():
    entry = node_path.platformEntry({'os': 'linux',
                                     'cwdRoots': ['/srv/app', '', 3, None]})
    assert entry['cwdRoots'] == ['/srv/app', '3']
    assert node_path.platformEntry({'cwdRoots': 'nope'})['cwdRoots'] == []
    assert node_path.platformEntry(None)['cwdRoots'] == []


def test_cwd_roots_error_message_and_boundaries(monkeypatch):
    seedPlatform(monkeypatch, **{'i7-4710MQ': {
        'os': 'windows', 'cwdRoots': [r'C:\work', r'D:\proj']}})
    assert node_path.cwdRootsError('i7-4710MQ', r'C:\work\a\b') is None
    assert node_path.cwdRootsError('i7-4710MQ', r'C:\WORK') is None
    assert node_path.cwdRootsError('i7-4710MQ', r'D:\proj') is None
    err = node_path.cwdRootsError('i7-4710MQ', r'E:\tmp')
    assert err == ('cwd E:\\tmp 不在执行点 i7-4710MQ 允许的根下'
                   '（允许：C:\\work, D:\\proj）')
    assert node_path.cwdRootsError('i7-4710MQ', r'C:\workx') is not None
    # 根未知（没上报 / 空数组）不校验
    assert node_path.cwdRootsError('ghost', r'E:\tmp') is None
    # realpath 复核：词法在根下、解析后不在 → 拒
    assert node_path.cwdRootsError('i7-4710MQ', r'C:\work\link',
                                   realpath=lambda p: r'D:\proj\x') is None
    assert node_path.cwdRootsError('i7-4710MQ', r'C:\work\link',
                                   realpath=lambda p: r'E:\elsewhere') \
        is not None


# ---------------------------------------------------------------- /api/tasks/nodes

def test_nodes_list_reports_cwd_roots(env, monkeypatch):
    """每项带 cwdRoots：本机取 config，node 取 /health 上报，成员没有给 []。"""
    import modules.tasks_federation as fedMod
    work = env.tmp / 'w'
    work.mkdir()
    monkeypatch.setattr(configMod, 'getWorkDirs', lambda: [str(work)])
    monkeypatch.setattr(configMod, 'getFallbackDir',
                        lambda: str(env.tmp / 'fb'))
    seedPlatform(monkeypatch,
                 **{'i7-4710MQ': {'os': 'windows', 'shells': ['bash'],
                                  'cwdRoots': [r'C:\work', r'D:\proj']},
                    'ghost': {'os': 'linux'}})
    monkeypatch.setattr(fedMod.config, 'getAgentNodes', lambda: [
        {'name': 'i7-4710MQ', 'via': 'h1', 'models': [], 'model': ''}])
    monkeypatch.setattr(fedMod.config, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(fedMod.config, 'getRemotes', lambda: [
        {'id': 'w1', 'name': 'ghost', 'models': [], 'model': ''}])
    monkeypatch.setattr(nodesMod, 'findFederationMember',
                        lambda name: {'name': name} if name == 'ghost' else None)
    resp = env.client.get('/api/tasks/nodes', headers=H).get_json()
    byName = {n['name']: n for n in resp['nodes']}
    assert byName['i7-4710MQ']['cwdRoots'] == [r'C:\work', r'D:\proj']
    assert byName['ghost']['cwdRoots'] == []          # 没上报 = 未知
    assert byName['local']['cwdRoots'] == [str(work), str(env.tmp / 'fb')]


def test_federation_member_cwd_roots_from_its_nodes_list(monkeypatch):
    """联邦成员的允许根取它自己 /api/tasks/nodes 的 local 条目。"""
    import modules.tasks_federation as fedMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    monkeypatch.setattr(nodesMod.config, 'getAgentNodes', lambda: [])
    monkeypatch.setattr(nodesMod.config, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(nodesMod.config, 'getRemotes', lambda: [
        {'id': 'win4710', 'name': 'win4710', 'base': 'http://x', 'token': 't',
         'terminal': True, 'apps': [], 'models': [], 'model': ''}])

    def fakeMemberCall(member, method, path, payload=None, timeout=20):
        assert method == 'GET' and path == '/api/tasks/nodes'
        return 200, {'nodes': [{'name': 'local', 'os': 'windows',
                                'cwdRoots': [r'D:\work']}]}
    monkeypatch.setattr(fedMod, '_memberCall', fakeMemberCall)
    nodesMod._refreshHealthModels(rounds=1)
    assert node_path.nodeCwdRoots('win4710') == [r'D:\work']


# ---------------------------------------------------------------- 投递复核

class _FakeSftp(object):
    """内存远端：dirs 集合 + files 字典 + links 软链表（normalize 解析）。"""

    def __init__(self, dirs=('/',), links=None):
        self.dirs = set(dirs)
        self.files = {}
        self.links = dict(links or {})
        self.closed = False

    def normalize(self, path):
        return self.links.get(str(path), str(path))

    def close(self):
        self.closed = True

    def stat(self, path):
        p = str(path)
        if p in self.dirs:
            return SimpleNamespace(st_mode=0o040755, st_size=0)
        if p in self.files:
            return SimpleNamespace(st_mode=0o100644, st_size=len(self.files[p]))
        raise IOError('no such file: ' + p)

    def mkdir(self, path):
        p = str(path)
        if p in self.dirs:
            raise IOError('exists')
        self.dirs.add(p)

    def put(self, localPath, remotePath):
        with open(localPath, 'rb') as f:
            self.files[str(remotePath)] = f.read()

    def posix_rename(self, src, dst):
        self.files[str(dst)] = self.files.pop(str(src))

    rename = posix_rename

    def remove(self, path):
        self.files.pop(str(path), None)

    def rmdir(self, path):
        self.dirs.discard(str(path))

    def listdir_attr(self, path):
        base = str(path)
        prefix = '/' if base == '/' else base + '/'
        out = []
        for p, data in self.files.items():
            if p.startswith(prefix) and '/' not in p[len(prefix):]:
                out.append(SimpleNamespace(filename=p[len(prefix):],
                                           st_mode=0o100644, st_size=len(data)))
        for d in self.dirs:
            if d != base and d.startswith(prefix) \
                    and '/' not in d[len(prefix):]:
                out.append(SimpleNamespace(filename=d[len(prefix):],
                                           st_mode=0o040755, st_size=0))
        return out


def _sshTarget(monkeypatch, name, fake):
    import modules.task_inputs_sftp as sftpMod
    seedPlatform(monkeypatch, **{name: {'os': 'linux',
                                        'cwdRoots': ['/srv/app']}})
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda n: {'name': n, 'via': 'h1'})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda n: None)
    monkeypatch.setattr(sftpMod, 'viaHost', lambda via: {'id': 'h1'})
    monkeypatch.setattr(sftpMod, '_openSftp', lambda hid: fake)


def test_deliver_rejects_symlink_swapped_cwd(monkeypatch, tmp_path):
    """TOCTOU：建卡后 cwd 被换成指向根外的软链——投递前按 realpath 拒。"""
    fake = _FakeSftp({'/', '/srv', '/srv/app', '/srv/app/link', '/outside'},
                     links={'/srv/app/link': '/outside/real'})
    _sshTarget(monkeypatch, 'ai-xmyz', fake)
    a = tmp_path / 'a.txt'
    a.write_bytes(b'A')
    with pytest.raises(DeliverError) as err:
        task_inputs.deliver('ai-xmyz', '/srv/app/link', 'task1', [str(a)])
    assert err.value.status == 400
    assert '允许' in str(err.value) and '/srv/app' in str(err.value)
    assert fake.files == {} and fake.closed is True      # 什么都没落


def test_deliver_realpath_within_roots_still_delivers(monkeypatch, tmp_path):
    """realpath 复核在根之下 → 照常投递。"""
    fake = _FakeSftp({'/', '/srv', '/srv/app'})
    _sshTarget(monkeypatch, 'ai-xmyz', fake)
    a = tmp_path / 'a.txt'
    a.write_bytes(b'A')
    out = task_inputs.deliver('ai-xmyz', '/srv/app', 'task2', [str(a)])
    assert out['dir'] == '/srv/app/.deskport-inputs/task2'
    assert out['paths'] == ['/srv/app/.deskport-inputs/task2/a.txt']
    assert fake.files['/srv/app/.deskport-inputs/task2/a.txt'] == b'A'


def test_deliver_unknown_roots_not_validated(monkeypatch, tmp_path):
    """cwdRoots 未知（空数组）：投递不拦（向后兼容，原行为）。"""
    fake = _FakeSftp({'/', '/tmp'})
    import modules.task_inputs_sftp as sftpMod
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda n: {'name': n, 'via': 'h1'})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda n: None)
    monkeypatch.setattr(sftpMod, 'viaHost', lambda via: {'id': 'h1'})
    monkeypatch.setattr(sftpMod, '_openSftp', lambda hid: fake)
    a = tmp_path / 'a.txt'
    a.write_bytes(b'A')
    out = task_inputs.deliver('ghost-node', '/tmp', 'task3', [str(a)])
    assert out['dir'] == '/tmp/.deskport-inputs/task3'
    assert fake.files['/tmp/.deskport-inputs/task3/a.txt'] == b'A'


def test_fed_target_realpath_is_lexical(monkeypatch):
    """联邦通道没有 realpath 端点：按成员声明的路径语义词法归一化。"""
    from modules.task_inputs_fed import FedTarget
    seedPlatform(monkeypatch, **{'win4710': {'os': 'windows'},
                                 'ai-xmyz': {'os': 'linux'}})
    monkeypatch.setattr(nodesMod, 'findFederationMember',
                        lambda name: {'name': name})
    win = FedTarget({'id': 'w', 'name': 'win4710', 'base': 'http://x',
                     'token': 't'})
    lin = FedTarget({'id': 'a', 'name': 'ai-xmyz', 'base': 'http://x',
                     'token': 't'})
    assert win.realpath(r'D:\work\a\..\b') == r'D:\work\b'
    assert lin.realpath('/srv/app/../app/x') == '/srv/app/x'
