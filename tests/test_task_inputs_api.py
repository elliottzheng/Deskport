# -*- coding: utf-8 -*-
"""跨机附件投递的建任务集成：prompt 补目录、失败不建任务、远端 share。

SSH 目录布局/原子性/重名见 tests/test_task_inputs.py；这里覆盖 tasks_api
的投递钩子、/api/agent/share 接到远端任务，以及联邦通道的上传与清理。
"""
import hashlib
import io
import json
import os
import queue
from types import SimpleNamespace

import pytest

import config as configMod
import modules.agent_nodes as nodesMod
from modules import events as eventsMod
from modules import task_inputs
from modules import tasks_api
from modules import tasks_store as store
from modules.task_inputs_base import DeliverError
from modules.tasks_store import TaskRepository

HOME = '/home/u/proj'


def _local(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------- 建任务集成

TOKEN = 'tok-' + 'b' * 24
TOKEN_SHARE = 'tok-share-' + 's' * 16
H = {'X-Agent-Token': TOKEN}
HS = {'X-Agent-Token': TOKEN_SHARE}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 't1', 'name': '测试智能体',
         'tokenHash': hashlib.sha256(TOKEN.encode()).hexdigest()},
        {'id': 'share', 'name': '手机分享', 'scopes': ['share'],
         'tokenHash': hashlib.sha256(TOKEN_SHARE.encode()).hexdigest()}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': 10 ** 9, 'data': {}})
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'via': 'h1'})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def _create(env, body):
    return env.client.post('/api/tasks', headers=H,
                           data=json.dumps(body),
                           content_type='application/json')


def test_remote_task_prompt_note_and_remote_paths(client, tmp_path, monkeypatch):
    a = _local(tmp_path, 'a.txt', b'A')
    monkeypatch.setattr(task_inputs, 'dirFor',
                        lambda node, cwd, taskId:
                        cwd + '/.deskport-inputs/' + taskId)
    monkeypatch.setattr(task_inputs, 'deliver',
                        lambda node, cwd, taskId, paths=(), attachments=():
                        {'dir': cwd + '/.deskport-inputs/' + taskId,
                         'paths': [cwd + '/.deskport-inputs/' + taskId + '/a.txt']})
    resp = _create(client, {'prompt': '看附件', 'node': 'ai-xmyz',
                            'cwd': HOME, 'paths': [a]})
    body = resp.get_json()
    assert resp.status_code == 202, body
    task = client.repo.get(body['task']['id'])
    assert task['prompt'].endswith(
        '附件已投递到 /home/u/proj/.deskport-inputs/' + task['id'])
    assert task['sources'] == []          # 不再往本机工作区复制
    assert body['paths'][0].endswith('/a.txt')
    assert body['paths'][0].startswith('/home/u/proj/.deskport-inputs/')


def test_delivery_failure_leaves_no_task(client, tmp_path, monkeypatch):
    a = _local(tmp_path, 'a.txt', b'A')
    b = _local(tmp_path, 'b.txt', b'B')
    monkeypatch.setattr(task_inputs, 'dirFor',
                        lambda node, cwd, taskId:
                        cwd + '/.deskport-inputs/' + taskId)
    monkeypatch.setattr(task_inputs, 'deliver',
                        lambda *a, **k: (_ for _ in ()).throw(
                            DeliverError('模拟第 2 个文件失败', 502)))
    resp = _create(client, {'prompt': '看附件', 'node': 'ai-xmyz',
                            'cwd': HOME, 'paths': [a, b]})
    assert resp.status_code == 502
    assert client.repo.list() == []
    root = client.tmp / 'root'
    assert not root.is_dir() or not list(root.iterdir())


def test_local_task_prompt_unchanged(client, tmp_path, monkeypatch):
    a = _local(tmp_path, 'a.txt', b'A')
    monkeypatch.setattr(task_inputs, 'deliver',
                        lambda *a, **k: pytest.fail('本机不走投递'))
    resp = _create(client, {'prompt': '看附件', 'paths': [a]})
    body = resp.get_json()
    assert resp.status_code == 202, body
    task = client.repo.get(body['task']['id'])
    assert task['prompt'] == '看附件'
    assert task['sources']


def test_prompt_note_over_limit_rejected(client, tmp_path, monkeypatch):
    a = _local(tmp_path, 'a.txt', b'A')
    monkeypatch.setattr(task_inputs, 'dirFor',
                        lambda node, cwd, taskId: '/x/' + ('d' * 60) + taskId)
    resp = _create(client, {'prompt': 'x' * 3999, 'node': 'ai-xmyz',
                            'cwd': HOME, 'paths': [a]})
    assert resp.status_code == 400
    assert '4000' in resp.get_json()['error']
    assert client.repo.list() == []


def _share(env, files=(), prompt='', **form):
    data = {'prompt': prompt}
    if files:
        data['file'] = [(io.BytesIO(raw), name) for name, raw in files]
    data.update(form)
    return env.client.post('/api/agent/share', data=data, headers=HS,
                           content_type='multipart/form-data')


def test_share_append_to_remote_task(client, monkeypatch):
    """接到远端任务：附件先投到该任务的输入目录，消息里用远端路径。"""
    task = {'id': 'task-r', 'title': '远端任务', 'prompt': '旧描述',
            'status': 'waiting', 'sessionId': 'sess-1', 'events': [], 'seq': 0,
            'createdAt': 1, 'workspace': str(client.tmp / 'ws'),
            'node': 'ai-xmyz', 'cwd': HOME, 'mode': 'default'}
    client.repo.put(task)
    client.repo.save()
    events = queue.Queue()
    client.repo.setAgent('task-r', {'queue': events, 'client': None})
    seen = {}

    def fakeDeliver(node, cwd, taskId, paths=(), attachments=()):
        seen.update(node=node, cwd=cwd, taskId=taskId, paths=list(paths))
        return {'dir': cwd + '/.deskport-inputs/' + taskId,
                'paths': [cwd + '/.deskport-inputs/' + taskId + '/x.txt']}

    monkeypatch.setattr(task_inputs, 'deliver', fakeDeliver)
    resp = _share(client, files=[('x.txt', b'X')], prompt='补充材料',
                  taskId='task-r')
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert seen == {'node': 'ai-xmyz', 'cwd': HOME, 'taskId': 'task-r',
                    'paths': [seen['paths'][0]]}
    item = events.get_nowait()
    assert item['type'] == 'message'
    assert item['text'].startswith('补充材料\n\n附件：')
    assert item['text'].endswith(HOME + '/.deskport-inputs/task-r/x.txt')
    assert body['paths'] == [HOME + '/.deskport-inputs/task-r/x.txt']


def test_fed_target_upload_and_cleanup(tmp_path):
    """联邦通道：同名 (2)、目录创建、失败清理走回收站。"""
    from modules.task_inputs_fed import FedTarget

    member = {'id': 'win4710', 'name': 'win4710', 'base': 'http://x',
              'token': 't', 'files': True, 'filesWrite': True}
    target = FedTarget(member)
    join = os.path.join
    root = 'D:' + os.sep + 'work'
    dirs = {root, root[:2] + os.sep}
    stored = {}
    recycled = []

    def names(path):
        prefix = path if path.endswith(os.sep) else path + os.sep
        return {p[len(prefix):].lower() for p in list(dirs) + list(stored)
                if p.lower().startswith(prefix.lower())
                and os.sep not in p[len(prefix):]}

    def call(method, path, timeout=0, params=None, json=None, data=None,
             files=None):
        if path == '/api/files':
            if params['path'] not in dirs:
                return 404, {}
            return 200, {'entries': [{'name': n}
                                     for n in names(params['path'])]}
        if path == '/api/files/new-folder':
            full = join(json['path'], json['name'])
            if full in dirs or full in stored:
                return 409, {}
            dirs.add(full)
            return 200, {'path': full}
        if path == '/api/files/upload':
            name = files['file'][0]
            payload = files['file'][1].read()
            full = join(data['path'], name)
            i = 2
            while full in stored or full in dirs:
                stem, dot, ext = name.rpartition('.')
                full = join(data['path'], ('%s(%d).%s' % (stem, i, ext))
                            if dot else '%s(%d)' % (name, i))
                i += 1
            stored[full] = payload
            return 200, {'ok': True, 'path': full,
                         'filename': os.path.basename(full)}
        if path == '/api/files/recycle-delete':
            recycled.extend(json['paths'])
            for p in json['paths']:
                stored.pop(p, None)
                dirs.discard(p)
            return 200, {'ok': True}
        raise AssertionError(path)

    target._call = call
    target._info = {'maxUploadMB': 100, 'writeRootsConfigured': True}
    a = tmp_path / 'a.txt'
    a.write_bytes(b'A')
    base = join(root, '.deskport-inputs', 't1')
    assert target.ensureDir(join(root, '.deskport-inputs'))
    assert target.ensureDir(base)
    first = target.putFile(str(a), 'a.txt', base, track=True)
    second = target.putFile(str(a), 'a.txt', base, track=True)
    assert first.endswith('a.txt') and second.endswith('a(2).txt')
    assert stored[first] == b'A' and stored[second] == b'A'
    target.cleanup(base, {'task': True, 'inputs': True})
    assert recycled == [base]


def test_fed_info_without_from_header_and_limit_takes_min(monkeypatch):
    """能力声明不带 X-Deskport-From（会撞成员端联邦白名单）；上限两端取小。"""
    from modules import federation_write
    from modules.task_inputs_fed import FedTarget

    target = FedTarget({'id': 'win4710', 'name': 'win4710',
                        'base': 'http://x', 'token': 't'})
    seen = {}

    def call(method, path, timeout=0, fromHeader=True, **kw):
        seen['path'] = path
        seen['fromHeader'] = fromHeader
        return 200, {'maxUploadMB': 1, 'writeRootsConfigured': True}

    target._call = call
    monkeypatch.setattr(federation_write, 'uploadLimitMB', lambda: 10)
    assert target.info()['maxUploadMB'] == 1
    assert seen == {'path': '/api/files/federation-info', 'fromHeader': False}
    assert target.maxMB() == 1


def test_remote_prompt_does_not_point_to_local_inputs():
    """远端任务的 workspace 在本机，prompt 不能再写『附件在本机 inputs』。"""
    task = {'prompt': '跑', 'workspace': 'E:' + os.sep + 'ws',
            'cwd': HOME, 'node': 'ai-xmyz'}
    text = store._promptFor(task, [])
    assert '你的工作目录是' not in text
    assert task['workspace'] not in text
