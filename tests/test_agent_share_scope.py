# -*- coding: utf-8 -*-
"""转发任务 — 鉴权 / 权限 / 执行点与任务列表（/api/agent/share）。

从 test_agent_share.py 拆出（结构门禁 400 行预算）：本文件只覆盖
scope 边界（share 可分享不能 exec、upload 403、full 与旧令牌放行、
设备令牌默认含 share）、nodes 只列本机、tasks 最近 20 条非 closed。
落盘与三个去处的用例在 tests/test_agent_share.py。
"""
import hashlib
import io
import json
import os
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import device_auth
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN_SHARE = 'tok-share-' + 'e' * 16   # scopes ["share"]
TOKEN_UPLOAD = 'tok-up-' + 'f' * 16     # scopes ["upload"]（旧手机令牌）
TOKEN_FULL = 'tok-full-' + 'a' * 16     # 没写 scopes = 全权
TOKEN_REMOTE = 'tok-remote-' + 'r' * 16  # scopes ["share", "upload_remote"]
PASSWORD = 'pw-share'


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _auditOf(audit, action):
    return [a for a in audit if a['action'] == action]


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'share', 'name': '手机分享', 'tokenHash': _hash(TOKEN_SHARE),
         'scopes': ['share']},
        {'id': 'phone', 'name': '手机上传', 'tokenHash': _hash(TOKEN_UPLOAD),
         'scopes': ['upload']},
        {'id': 'full', 'name': '全权', 'tokenHash': _hash(TOKEN_FULL)},
        {'id': 'remote', 'name': '远端投递', 'tokenHash': _hash(TOKEN_REMOTE),
         'scopes': ['share', 'upload_remote']},
    ])
    monkeypatch.setattr(configMod, 'getPassword', lambda: PASSWORD)
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(configMod, 'getPhoneUploadDir',
                        lambda: str(tmp_path / '手机上传'))
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 1)
    monkeypatch.setattr(eventsMod, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    device_auth._resetForTests()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), audit=audit, repo=repo,
                           tmp=tmp_path, monkeypatch=monkeypatch)


def share(env, token, files=(), prompt='', **form):
    """files: [(name, bytes)]；file 字段可重复。"""
    data = {}
    if prompt:
        data['prompt'] = prompt
    if files:
        data['file'] = [(io.BytesIO(raw), name) for name, raw in files]
    data.update(form)
    headers = {} if token is None else {'X-Agent-Token': token}
    return env.client.post('/api/agent/share', data=data, headers=headers,
                           content_type='multipart/form-data')


def seedTask(env, **over):
    task = {'id': 'task-' + 'a' * 8, 'title': '旧任务', 'prompt': '旧描述',
            'status': 'waiting', 'sessionId': 'sess-1', 'events': [], 'seq': 0,
            'createdAt': time.time() - 60, 'lastActiveAt': time.time() - 10,
            'workspace': str(env.tmp / 'ws'), 'node': 'local',
            'mode': 'default', 'resultFiles': []}
    task.update(over)
    env.repo.put(task)
    env.repo.save()
    return task


def test_share_token_can_share_but_not_exec_or_tasks(env):
    resp = share(env, TOKEN_SHARE, prompt='记一下')
    assert resp.status_code == 200, resp.get_json()
    execResp = env.client.post('/api/agent/ssh/exec',
                               headers={'X-Agent-Token': TOKEN_SHARE},
                               data=json.dumps({'host': 'local', 'command': 'ls'}),
                               content_type='application/json')
    assert execResp.status_code == 403
    assert execResp.get_json()['need'] == 'full'
    tasksResp = env.client.post('/api/tasks', headers={'X-Agent-Token': TOKEN_SHARE},
                                data=json.dumps({'prompt': 'x'}),
                                content_type='application/json')
    assert tasksResp.status_code == 403 and tasksResp.get_json()['need'] == 'full'


def test_upload_token_denied_share_with_audit(env):
    resp = share(env, TOKEN_UPLOAD, prompt='x')
    assert resp.status_code == 403
    assert resp.get_json()['need'] == 'share'
    denies = _auditOf(env.audit, 'agent.scope')
    assert denies and denies[-1]['target'] == '手机上传'
    assert 'need=share' in denies[-1]['detail']


def test_full_and_legacy_tokens_allowed(env):
    assert share(env, TOKEN_FULL, prompt='全权').status_code == 200


def test_no_token_401(env):
    assert share(env, None, prompt='x').status_code == 401


def test_device_login_issues_share_scope_and_can_submit(env):
    login = env.client.post('/api/agent/device/login',
                            data=json.dumps({'password': PASSWORD, 'device': '测试手机'}),
                            content_type='application/json')
    assert login.status_code == 200
    body = login.get_json()
    assert body['scopes'] == ['upload', 'share']
    token = body['token']
    assert share(env, token, files=[('a.txt', b'hi')], prompt='交给执行方').status_code == 200
    resp = env.client.post('/api/agent/ssh/exec',
                           headers={'X-Agent-Token': token},
                           data=json.dumps({'host': 'local', 'command': 'ls'}),
                           content_type='application/json')
    assert resp.status_code == 403 and resp.get_json()['need'] == 'full'


def test_nodes_include_local_and_remote(env, monkeypatch):
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'nodeModelsFor', lambda name, entry=None: (['k2'], 'k2'))
    monkeypatch.setattr(configMod, 'getAgentNodes', lambda: [
        {'name': 'ai-xmyz', 'url': 'http://x', 'token': '', 'via': 'h1',
         'remote': '', 'models': [], 'model': ''}])
    monkeypatch.setattr(configMod, 'getRemotes', lambda: [
        {'id': 'win4710', 'name': 'win4710', 'base': 'http://y', 'token': '',
         'files': True, 'filesWrite': True}])
    body = env.client.get('/api/agent/share/nodes',
                          headers={'X-Agent-Token': TOKEN_SHARE}).get_json()
    assert body['default'] == 'local'
    names = [n['name'] for n in body['nodes']]
    assert names == ['local', 'ai-xmyz', 'win4710']
    # 批次 NODE-OS：每项带 os（本机声明 + 远端自报），分享页占位符按它切
    assert body['nodes'][0] == {'name': 'local', 'label': '本机',
                                'status': 'online', 'models': ['k2'],
                                'defaultModel': 'k2', 'os': 'windows'}
    assert body['nodes'][1]['models'] == ['k2']
    assert body['nodes'][1]['os'] == ''     # 没上报 = 未知
    assert body['nodes'][2]['os'] == ''


def test_tasks_list_recent_non_closed_sorted_and_limited(env):
    now = time.time()
    seedTask(env, id='t-old', status='waiting', lastActiveAt=now - 300)
    seedTask(env, id='t-new', status='running', node='local', lastActiveAt=now)
    seedTask(env, id='t-closed', status='closed', lastActiveAt=now + 10)
    for i in range(25):
        seedTask(env, id='t-%02d' % i, status='waiting', lastActiveAt=now - 100 - i)
    body = env.client.get('/api/agent/share/tasks',
                          headers={'X-Agent-Token': TOKEN_SHARE}).get_json()
    items = body['tasks']
    assert len(items) == 20
    ids = [i['id'] for i in items]
    assert 't-closed' not in ids
    assert ids[0] == 't-new'
    updated = [i['updatedAt'] for i in items]
    assert updated == sorted(updated, reverse=True)
    assert set(items[0]) == {'id', 'title', 'status', 'updatedAt', 'node'}
    assert items[0]['updatedAt'] == pytest.approx(now)


# ---- upload_remote：往远端执行点投附件的第三层授权（批次 B2） ----

def _stubRemote(monkeypatch):
    """远端投递桩：不连真机，只记录投到了哪。"""
    import modules.agent_nodes as nodesMod
    from modules import task_inputs
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'via': 'h1'})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    monkeypatch.setattr(task_inputs, 'dirFor',
                        lambda node, cwd, taskId:
                        cwd + '/.deskport-inputs/' + taskId)
    seen = {}

    def fakeDeliver(node, cwd, taskId, paths=(), attachments=()):
        remote = cwd + '/.deskport-inputs/' + taskId
        seen.update(node=node, cwd=cwd, paths=list(paths))
        return {'dir': remote,
                'paths': [remote + '/' + os.path.basename(p) for p in paths]}

    monkeypatch.setattr(task_inputs, 'deliver', fakeDeliver)
    return seen


def test_remote_node_without_upload_remote_403(env, monkeypatch):
    seen = _stubRemote(monkeypatch)
    resp = share(env, TOKEN_SHARE, files=[('a.txt', b'A')], prompt='跑一下',
                 node='ai-xmyz', cwd='/home/u/proj')
    assert resp.status_code == 403
    body = resp.get_json()
    assert body['need'] == 'upload_remote'
    assert 'upload_remote' in body['error'] and '权限' in body['error']
    assert '白名单' not in body['error']      # 别让人去查成员机配置
    assert seen == {}                         # 门在投递之前
    assert env.repo.list() == []              # 任务没建
    staged = env.tmp / 'taskroot'
    assert not staged.is_dir() or not list(staged.iterdir())
    denies = _auditOf(env.audit, 'agent.share')
    assert denies and denies[-1]['result'] == 'deny'
    assert 'need=upload_remote' in denies[-1]['detail']


def test_remote_node_with_upload_remote_allowed(env, monkeypatch):
    seen = _stubRemote(monkeypatch)
    resp = share(env, TOKEN_REMOTE, files=[('a.txt', b'A')], prompt='跑一下',
                 node='ai-xmyz', cwd='/home/u/proj')
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert seen['node'] == 'ai-xmyz' and seen['cwd'] == '/home/u/proj'
    assert body['paths'][0].startswith('/home/u/proj/.deskport-inputs/')
    assert env.repo.get(body['taskId'])['node'] == 'ai-xmyz'


def test_append_to_remote_task_also_needs_upload_remote(env, monkeypatch):
    """taskId 不能绕过授权门：追加到远端任务同样是往那台机器投附件。"""
    seen = _stubRemote(monkeypatch)
    seedTask(env, id='task-r', node='ai-xmyz', cwd='/home/u/proj')
    resp = share(env, TOKEN_SHARE, files=[('a.txt', b'A')], prompt='补充',
                 taskId='task-r')
    assert resp.status_code == 403
    assert resp.get_json()['need'] == 'upload_remote'
    assert seen == {}


def test_local_node_does_not_need_upload_remote(env, monkeypatch):
    seen = _stubRemote(monkeypatch)
    resp = share(env, TOKEN_SHARE, files=[('a.txt', b'A')], prompt='本机跑')
    assert resp.status_code == 200, resp.get_json()
    assert seen == {}
    assert env.repo.list() and env.repo.list()[0]['node'] == 'local'


def test_full_token_can_deliver_remotely(env, monkeypatch):
    seen = _stubRemote(monkeypatch)
    resp = share(env, TOKEN_FULL, files=[('a.txt', b'A')], prompt='跑一下',
                 node='ai-xmyz', cwd='/home/u/proj')
    assert resp.status_code == 200, resp.get_json()
    assert seen['node'] == 'ai-xmyz'


def test_device_login_can_opt_into_upload_remote(env, monkeypatch):
    seen = _stubRemote(monkeypatch)
    login = env.client.post('/api/agent/device/login',
                            data=json.dumps({'password': PASSWORD,
                                             'device': '远端手机',
                                             'allowRemoteUpload': True}),
                            content_type='application/json')
    body = login.get_json()
    assert body['scopes'] == ['upload', 'share', 'upload_remote']
    assert device_auth.publicDevices()[-1]['scopes'] == body['scopes']
    token = body['token']
    ok = share(env, token, files=[('a.txt', b'A')], prompt='跑一下',
               node='ai-xmyz', cwd='/home/u/proj')
    assert ok.status_code == 200, ok.get_json()
    assert seen['node'] == 'ai-xmyz'
