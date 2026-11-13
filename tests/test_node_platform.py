# -*- coding: utf-8 -*-
"""批次 NODE-OS：执行点自报平台、派活前 cwd 平台语法校验、按目标 OS 拼路径。

回归两个真实故障：
  1. Windows 执行点 + cwd=/tmp → 派活前 400（原来 Popen(cwd=...) 的
     FileNotFoundError 被对端归成「找不到 kimi 命令」）；
  2. hub(Windows) → Linux 成员投附件不再拼出 /home/me/project\\.deskport-inputs\\<id>。
"""
import hashlib
import json
import os
import posixpath
import time
from types import SimpleNamespace

import pytest

import config as configMod
import modules.agent_nodes as nodesMod
from modules import events as eventsMod
from modules import node_path, task_inputs, tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'p' * 24
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


# ---------------------------------------------------------------- cwd 校验

def test_windows_node_rejects_posix_cwd(env, monkeypatch):
    """故障 1 回归：Windows 执行点 + /tmp → 400，文案带平台与示例。"""
    seedPlatform(monkeypatch, **{'i7-4710MQ': {'os': 'windows',
                                               'shells': ['bash']}})
    stubNodes(monkeypatch, ('i7-4710MQ',))
    resp = create(env, {'prompt': 'x', 'node': 'i7-4710MQ', 'cwd': '/tmp'})
    body = resp.get_json()
    assert resp.status_code == 400
    assert 'windows' in body['error'] and 'C:\\work' in body['error']
    assert '/tmp' in body['error']
    # 同节点给盘符路径 → 照常受理
    assert create(env, {'prompt': 'x', 'node': 'i7-4710MQ',
                        'cwd': 'C:\\work'}).status_code == 202


def test_windows_node_accepts_unc_cwd(env, monkeypatch):
    seedPlatform(monkeypatch, **{'i7-4710MQ': {'os': 'windows'}})
    stubNodes(monkeypatch, ('i7-4710MQ',))
    resp = create(env, {'prompt': 'x', 'node': 'i7-4710MQ',
                        'cwd': '\\\\nas\\team\\work'})
    assert resp.status_code == 202


def test_linux_node_rejects_windows_cwd(env, monkeypatch):
    seedPlatform(monkeypatch, **{'ai-xmyz': {'os': 'linux',
                                             'shells': ['bash', 'sh']}})
    stubNodes(monkeypatch, ('ai-xmyz',))
    resp = create(env, {'prompt': 'x', 'node': 'ai-xmyz', 'cwd': 'C:\\work'})
    body = resp.get_json()
    assert resp.status_code == 400
    assert 'linux' in body['error'] and '/home/me/project' in body['error']
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/srv/app'}).status_code == 202


def test_unknown_os_node_not_validated(env, monkeypatch):
    """os 未知（老 node / 老成员没上报）：两种 cwd 都不拦，向后兼容。"""
    stubNodes(monkeypatch, ('ai-xmyz',))
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': '/tmp'}).status_code == 202
    assert create(env, {'prompt': 'x', 'node': 'ai-xmyz',
                        'cwd': 'C:\\work'}).status_code == 202


def test_local_node_cwd_rules_unchanged(env):
    """本机执行点仍走 task_cwd.resolve：相对路径 400，盘符绝对路径受理。"""
    assert create(env, {'prompt': 'x', 'cwd': 'relative/dir'}).status_code == 400
    home = os.path.expanduser('~')
    assert os.path.isdir(home)
    assert create(env, {'prompt': 'x', 'cwd': home}).status_code == 202


def test_darwin_node_rejects_windows_cwd(env, monkeypatch):
    seedPlatform(monkeypatch, **{'mac-mini': {'os': 'darwin'}})
    stubNodes(monkeypatch, ('mac-mini',))
    assert create(env, {'prompt': 'x', 'node': 'mac-mini',
                        'cwd': 'C:\\work'}).status_code == 400
    assert create(env, {'prompt': 'x', 'node': 'mac-mini',
                        'cwd': '/Users/me/project'}).status_code == 202


# ---------------------------------------------------------------- 单元：node_path

def test_placeholder_and_path_module():
    assert node_path.placeholder('windows') == 'C:\\work'
    assert node_path.placeholder('linux') == '/home/me/project'
    assert node_path.placeholder('darwin') == '/home/me/project'
    assert node_path.placeholder('') == '/path'
    assert node_path.pathModule('windows') is os.path
    assert node_path.pathModule('linux') is posixpath
    assert node_path.pathModule('', fallback=os.path) is os.path
    assert node_path.thisOs() in ('', 'windows', 'linux', 'darwin')


def test_platform_entry_normalizes_garbage():
    entry = node_path.platformEntry({'os': 'Windows', 'shells': ['bash', 3],
                                     'defaultCwd': 5})
    assert entry['os'] == 'windows'
    assert entry['shells'] == ['bash', '3']
    assert entry['defaultCwd'] == ''
    assert node_path.platformEntry(None)['os'] == ''
    assert node_path.normOs('sunos') == ''


def test_cwd_error_unknown_os_is_none():
    assert node_path.cwdError(' whoever', '/tmp') is None
    assert node_path.cwdError(' whoever', 'C:\\work') is None


# ---------------------------------------------------------------- /api/tasks/nodes

def test_nodes_list_reports_platform(env, monkeypatch):
    """§7.2 扩展：每项带 os/shells，节点给了 defaultCwd 才带。"""
    import modules.tasks_federation as fedMod
    seedPlatform(monkeypatch,
                 **{'ai-xmyz': {'os': 'linux', 'shells': ['bash', 'sh'],
                                'defaultCwd': '/srv/app', 'models': ['k2']},
                    'win4710': {'os': 'windows',
                                'shells': ['bash', 'powershell', 'pwsh']}})
    monkeypatch.setattr(fedMod.config, 'getAgentNodes', lambda: [
        {'name': 'ai-xmyz', 'via': 'h1', 'models': ['k2'], 'model': 'k2'}])
    monkeypatch.setattr(fedMod.config, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(fedMod.config, 'getRemotes', lambda: [
        {'id': 'w1', 'name': 'win4710', 'models': [], 'model': ''}])
    monkeypatch.setattr(nodesMod, 'findFederationMember',
                        lambda name: {'name': name} if name == 'win4710' else None)
    resp = env.client.get('/api/tasks/nodes', headers=H).get_json()
    byName = {n['name']: n for n in resp['nodes']}
    assert byName['ai-xmyz']['os'] == 'linux'
    assert byName['ai-xmyz']['shells'] == ['bash', 'sh']
    assert byName['ai-xmyz']['defaultCwd'] == '/srv/app'
    assert byName['win4710']['os'] == 'windows'
    assert byName['win4710']['shells'] == ['bash', 'powershell', 'pwsh']
    assert 'defaultCwd' not in byName['win4710']   # 拿不到就不给
    assert byName['local']['os'] == 'windows'      # 本机声明（localHostEntry）
    assert byName['local']['shells'][0] == 'bash'


def test_agent_node_health_platform_reaches_nodes_list(monkeypatch):
    """provider 侧回归：node /health 自报的 os 经刷新缓存出现在 nodes 列表。"""
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    monkeypatch.setattr(nodesMod.config, 'getAgentNodes', lambda: [
        {'name': 'ai-xmyz', 'url': 'http://127.0.0.1:1', 'token': 't',
         'via': '', 'remote': '', 'models': [], 'model': ''}])
    monkeypatch.setattr(nodesMod.config, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(nodesMod.config, 'getRemotes', lambda: [])

    def fakeNodeRequest(node, method, path, payload=None, timeout=30):
        assert path == '/health'
        return 200, {'models': ['k2'], 'defaultModel': 'k2',
                     'os': 'linux', 'shells': ['bash', 'sh'],
                     'defaultCwd': '/srv/app'}
    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeNodeRequest)
    nodesMod._refreshHealthModels(rounds=1)
    assert node_path.nodeOs('ai-xmyz') == 'linux'
    assert node_path.nodeShells('ai-xmyz') == ['bash', 'sh']
    assert node_path.nodeDefaultCwd('ai-xmyz') == '/srv/app'


def test_federation_member_platform_from_its_nodes_list(monkeypatch):
    """联邦成员的平台取它自己 /api/tasks/nodes 的 local 条目。"""
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
                                'shells': ['bash', 'powershell', 'pwsh'],
                                'defaultCwd': 'D:\\work'}]}
    monkeypatch.setattr(fedMod, '_memberCall', fakeMemberCall)
    nodesMod._refreshHealthModels(rounds=1)
    assert node_path.nodeOs('win4710') == 'windows'
    assert node_path.nodeDefaultCwd('win4710') == 'D:\\work'


# ---------------------------------------------------------------- 路径拼接

def test_dirFor_uses_member_os_separator(monkeypatch):
    """故障 2 回归：Linux 成员用 / 拼，Windows 成员用 \\ 拼。"""
    seedPlatform(monkeypatch, **{'ai-xmyz': {'os': 'linux'},
                                 'win4710': {'os': 'windows'}})
    stubNodes(monkeypatch, ('ai-xmyz',), ('win4710',))
    assert task_inputs.dirFor('ai-xmyz', '/home/me/project', 't1') == \
        '/home/me/project/.deskport-inputs/t1'
    assert task_inputs.dirFor('win4710', 'D:\\work', 't1') == \
        'D:\\work\\.deskport-inputs\\t1'


def test_dirFor_ssh_fallback_and_windows_node(monkeypatch):
    """ssh 通道：os 未知保持 posix（原行为），Windows 节点按 ntpath。"""
    stubNodes(monkeypatch, ('ai-xmyz', 'win-node'))
    assert task_inputs.dirFor('ai-xmyz', '/srv/app', 't1') == \
        '/srv/app/.deskport-inputs/t1'
    seedPlatform(monkeypatch, **{'win-node': {'os': 'windows'}})
    assert task_inputs.dirFor('win-node', 'C:\\work', 't1') == \
        'C:\\work\\.deskport-inputs\\t1'


def test_fed_target_joins_by_member_os(monkeypatch):
    from modules.task_inputs_fed import FedTarget
    seedPlatform(monkeypatch, **{'win4710': {'os': 'windows'},
                                 'ai-xmyz': {'os': 'linux'}})
    monkeypatch.setattr(nodesMod, 'findFederationMember',
                        lambda name: {'name': name})
    win = FedTarget({'id': 'w', 'name': 'win4710', 'base': 'http://x',
                     'token': 't'})
    lin = FedTarget({'id': 'a', 'name': 'ai-xmyz', 'base': 'http://x',
                     'token': 't'})
    assert win.pm is os.path
    assert lin.pm is posixpath
    assert win.join('D:\\work', '.deskport-inputs', 't1') == \
        'D:\\work\\.deskport-inputs\\t1'
    assert lin.join('/srv/app', '.deskport-inputs', 't1') == \
        '/srv/app/.deskport-inputs/t1'
    # 未知 os 的成员保持原行为（本机 os.path）
    plain = FedTarget({'id': 'p', 'name': 'ghost', 'base': 'http://x',
                       'token': 't'})
    assert plain.pm is os.path


def test_ssh_target_ensure_dir_windows_node(monkeypatch):
    """Windows 节点（SSH 到 Windows）：ensureDir 逐级建盘符路径。"""
    import modules.task_inputs_sftp as sftpMod
    seedPlatform(monkeypatch, **{'win-node': {'os': 'windows'}})
    stubNodes(monkeypatch, ('win-node',))
    made = []

    class FakeSftp(object):
        def stat(self, path):
            raise IOError('no such file')

        def mkdir(self, path):
            made.append(path)
    monkeypatch.setattr(sftpMod, 'viaHost', lambda via: {'id': 'h1'})
    monkeypatch.setattr(sftpMod, '_openSftp', lambda hid: FakeSftp())
    target = sftpMod.SshTarget({'name': 'win-node', 'via': 'h1'})
    assert target.pm is os.path
    assert target.ensureDir('C:\\work\\a\\b') is True
    assert made == ['C:\\work', 'C:\\work\\a', 'C:\\work\\a\\b']
