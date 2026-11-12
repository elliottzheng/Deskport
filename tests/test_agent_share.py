# -*- coding: utf-8 -*-
"""转发文件 → 交给执行方：落盘与三个去处（/api/agent/share）。

从原 test_agent_share.py 拆出（结构门禁 400 行预算）；鉴权 / 权限 /
执行点与任务列表在 tests/test_agent_share_scope.py。

覆盖: 建任务（含文件 / 只文件）、接到任务（waiting 立即投递 / running
入队）、closed 与不存在 409、远端执行点 400、prompt 超长 / 空请求 /
文件数 / 文件名 / 单文件超限、失败不留暂存垃圾、审计 agent.share、
v3.1 organize=folder（默认名 / 重名 (2) / 坏名 / 清理 / 目录说明）。
全部桩化，不碰真机与真实配置。
"""
import hashlib
import io
import json
import pathlib
import queue
import re
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_share
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN_SHARE = 'tok-share-' + 'e' * 16   # scopes ["share"]
TOKEN_UPLOAD = 'tok-up-' + 'f' * 16     # scopes ["upload"]（旧手机令牌）
TOKEN_FULL = 'tok-full-' + 'a' * 16     # 没写 scopes = 全权


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
    ])
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(eventsMod, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
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
            'workspace': str(env.tmp / 'ws'), 'engine': 'acp', 'node': '',
            'mode': 'default', 'resultFiles': []}
    task.update(over)
    env.repo.put(task)
    env.repo.save()
    return task


class _Client(object):
    def cancel(self):
        pass


def attachAgent(env, tid, running=False):
    agent = {'queue': queue.Queue(), 'client': _Client() if running else None}
    env.repo.setAgent(tid, agent)
    return agent


def stagedDirs(env):
    root = env.tmp / 'taskroot' / '_uploads'
    return list(root.iterdir()) if root.is_dir() else []


# ---- 建任务 ----

def test_create_task_with_two_files_and_prompt(env):
    resp = share(env, TOKEN_SHARE,
                 files=[('讲义一.pdf', b'%PDF-1'), ('讲义二.pdf', b'%PDF-2')],
                 prompt='把这两份讲义合成一份周测卷')
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['mode'] == 'created' and body['taskId']
    assert len(body['paths']) == 2 and body['prompt'] == '把这两份讲义合成一份周测卷'
    for path in body['paths']:
        assert path
        assert (env.tmp / 'taskroot' / '_uploads') in pathlib.Path(path).parents
    task = env.repo.get(body['taskId'])
    assert task['prompt'] == '把这两份讲义合成一份周测卷'
    assert task['engine'] == 'acp' and task['mode'] == 'default'
    names = [s['input'] for s in task['sources']]
    assert any('讲义一.pdf' in n for n in names) and any('讲义二.pdf' in n for n in names)
    audits = _auditOf(env.audit, 'agent.share')
    assert audits and audits[-1]['result'] == 'ok'
    assert audits[-1]['target'] == body['taskId']
    assert 'files=2' in audits[-1]['detail'] and 'promptLen=' in audits[-1]['detail']


def test_create_task_files_only_synthesizes_prompt(env):
    body = share(env, TOKEN_SHARE, files=[('a.xlsx', b'xx')]).get_json()
    assert body['mode'] == 'created'
    task = env.repo.get(body['taskId'])
    assert task['prompt'] and '1' in task['prompt']
    assert task['prompt'] != ''


def test_mode_and_model_passthrough(env, monkeypatch):
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'nodeModelDecl',
                        lambda name='': {'name': 'local', 'models': ['k2'],
                                         'default': 'k2'})
    body = share(env, TOKEN_SHARE, prompt='跑一下', mode='auto', model='k2').get_json()
    task = env.repo.get(body['taskId'])
    assert task['mode'] == 'auto' and task['model'] == 'k2'


# ---- 接到任务 ----

def test_append_to_waiting_task_delivers_message_now(env):
    task = seedTask(env, id='task-wait')
    agent = attachAgent(env, 'task-wait')
    resp = share(env, TOKEN_SHARE, files=[('表.xlsx', b'data')],
                 prompt='按上次的表格重算', taskId='task-wait')
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['mode'] == 'appended' and body['taskId'] == 'task-wait'
    item = agent['queue'].get_nowait()
    assert item['type'] == 'message'
    assert item['text'].startswith('按上次的表格重算\n\n附件：\n- ')
    assert body['paths'][0] in item['text']
    assert task['status'] == 'running'
    audits = _auditOf(env.audit, 'agent.share')
    assert audits and audits[-1]['target'] == 'task-wait'
    assert 'appended' in audits[-1]['detail']


def test_append_while_running_queues_message(env):
    task = seedTask(env, id='task-run', status='running')
    attachAgent(env, 'task-run', running=True)
    resp = share(env, TOKEN_SHARE, files=[('b.pdf', b'p')],
                 prompt='补充材料', taskId='task-run')
    assert resp.status_code == 200, resp.get_json()
    queued = task.get('messageQueue') or []
    assert len(queued) == 1 and '补充材料' in queued[0]['text']
    assert resp.get_json()['paths'][0] in queued[0]['text']


def test_append_unknown_or_closed_task_409(env):
    resp = share(env, TOKEN_SHARE, prompt='x', taskId='no-such-task')
    assert resp.status_code == 409
    assert stagedDirs(env) == []
    seedTask(env, id='task-closed', status='closed')
    resp = share(env, TOKEN_SHARE, files=[('c.txt', b'x')], prompt='x',
                 taskId='task-closed')
    assert resp.status_code == 409
    assert stagedDirs(env) == []


def test_append_to_cancelled_task_409_and_no_leftovers(env):
    seedTask(env, id='task-cancel', status='cancelled')
    resp = share(env, TOKEN_SHARE, files=[('d.txt', b'x')], prompt='x',
                 taskId='task-cancel')
    assert resp.status_code == 409
    assert stagedDirs(env) == []


# ---- 参数边界 ----

def test_empty_prompt_and_no_file_400(env):
    assert share(env, TOKEN_SHARE).status_code == 400
    assert share(env, TOKEN_SHARE, prompt='   ').status_code == 400


def test_prompt_too_long_400(env):
    resp = share(env, TOKEN_SHARE, prompt='x' * 4001)
    assert resp.status_code == 400 and 'prompt' in resp.get_json()['error']


def test_too_many_files_400(env):
    files = [('f%d.txt' % i, b'x') for i in range(11)]
    resp = share(env, TOKEN_SHARE, files=files)
    assert resp.status_code == 400 and '10' in resp.get_json()['error']
    assert stagedDirs(env) == []


def test_bad_filename_400(env):
    resp = share(env, TOKEN_SHARE, files=[('sub/evil.txt', b'x')])
    assert resp.status_code == 400 and '文件名不合法' in resp.get_json()['error']
    assert stagedDirs(env) == []


def test_file_over_limit_413_no_partial(env, monkeypatch):
    monkeypatch.setattr(agent_share, 'MAX_FILE_MB', 1)
    resp = share(env, TOKEN_SHARE, files=[('big.bin', b'x' * (1024 * 1024 + 50))])
    assert resp.status_code == 413
    assert '超过' in resp.get_json()['error']
    assert stagedDirs(env) == []


def test_remote_node_rejected_400(env):
    resp = share(env, TOKEN_SHARE, files=[('e.txt', b'x')], prompt='x',
                 node='ai-xmyz')
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['nodes'] == ['local'] and '远端' in body['error']
    assert stagedDirs(env) == []


def test_invalid_mode_400(env):
    assert share(env, TOKEN_SHARE, prompt='x', mode='yolo').status_code == 400


def test_staged_file_names_deduped(env):
    first = share(env, TOKEN_SHARE, files=[('same.txt', b'one')]).get_json()
    second = share(env, TOKEN_SHARE, files=[('same.txt', b'two')]).get_json()
    assert first['paths'][0] != second['paths'][0]
    assert first['paths'][0].endswith('same.txt')
    assert second['paths'][0].endswith('same.txt')
    with open(first['paths'][0], 'rb') as f:
        assert f.read() == b'one'
    with open(second['paths'][0], 'rb') as f:
        assert f.read() == b'two'


# ---- v3.1：organize=folder（交给 AI 也能新建文件夹） ----

def test_folder_mode_create_task(env):
    body = share(env, TOKEN_SHARE,
                 files=[('讲义一.pdf', b'%PDF-1'), ('讲义二.pdf', b'%PDF-2')],
                 prompt='合成一份周测卷', organize='folder', folderName='周测卷').get_json()
    assert body['mode'] == 'created'
    assert len(body['paths']) == 1
    folder = pathlib.Path(body['paths'][0])
    assert folder.is_dir() and folder.name == '周测卷'
    assert sorted(p.name for p in folder.iterdir()) == ['讲义一.pdf', '讲义二.pdf']
    assert body['prompt'].endswith('以上附件在 周测卷 目录下')
    task = env.repo.get(body['taskId'])
    inputs = pathlib.Path(task['workspace']) / 'inputs' / '周测卷'
    assert inputs.is_dir(), '新建文件夹要作为整体复制进任务工作区'
    assert sorted(p.name for p in inputs.iterdir()) == ['讲义一.pdf', '讲义二.pdf']
    audits = _auditOf(env.audit, 'agent.share')
    assert audits and audits[-1]['target'] == body['taskId']
    assert 'organize=folder' in audits[-1]['detail']


def test_folder_default_name_and_conflict(env):
    first = share(env, TOKEN_SHARE, files=[('a.txt', b'1')],
                  organize='folder').get_json()
    name = pathlib.Path(first['paths'][0]).name
    assert re.match(r'^share-\d{8}-\d{4}$', name), name
    assert first['prompt'].endswith('以上附件在 %s 目录下' % name)

    second = share(env, TOKEN_SHARE, files=[('a.txt', b'2')],
                   organize='folder', folderName=name).get_json()
    renamed = pathlib.Path(second['paths'][0])
    assert renamed.name == name + '(2)'      # 文件夹名冲突自动 (2)
    assert (renamed / 'a.txt').read_bytes() == b'2'
    assert (pathlib.Path(first['paths'][0]) / 'a.txt').read_bytes() == b'1'
    assert second['prompt'].endswith('以上附件在 %s 目录下' % (name + '(2)'))


def test_folder_same_file_name_gets_2(env):
    body = share(env, TOKEN_SHARE,
                 files=[('same.txt', b'one'), ('same.txt', b'two')],
                 organize='folder', folderName='F').get_json()
    folder = pathlib.Path(body['paths'][0])
    names = sorted(p.name for p in folder.iterdir())
    assert names == ['same(2).txt', 'same.txt']
    assert (folder / 'same.txt').read_bytes() == b'one'
    assert (folder / 'same(2).txt').read_bytes() == b'two'


@pytest.mark.parametrize('bad', ['..', '.', 'C:\\x', 'C:/x', '/abs', '\\abs',
                                 'a/b', 'a\\b', 'x<y', 'a"b'])
def test_folder_bad_name_400(env, bad):
    resp = share(env, TOKEN_SHARE, files=[('a.txt', b'x')], prompt='x',
                 organize='folder', folderName=bad)
    assert resp.status_code == 400, bad
    assert '文件夹名' in resp.get_json()['error']
    assert stagedDirs(env) == []


def test_folder_mode_requires_files(env):
    resp = share(env, TOKEN_SHARE, prompt='x', organize='folder')
    assert resp.status_code == 400
    assert '至少一个文件' in resp.get_json()['error']


def test_invalid_organize_400(env):
    resp = share(env, TOKEN_SHARE, files=[('a.txt', b'x')], prompt='x',
                 organize='zip')
    assert resp.status_code == 400 and 'organize' in resp.get_json()['error']


def test_append_with_folder_points_to_folder(env):
    task = seedTask(env, id='task-wait-folder')
    agent = attachAgent(env, 'task-wait-folder')
    body = share(env, TOKEN_SHARE, files=[('表.xlsx', b'data')],
                 prompt='按上次的表格重算', taskId='task-wait-folder',
                 organize='folder', folderName='周测卷').get_json()
    assert body['mode'] == 'appended' and len(body['paths']) == 1
    folder = pathlib.Path(body['paths'][0])
    assert folder.name == '周测卷' and (folder / '表.xlsx').read_bytes() == b'data'
    item = agent['queue'].get_nowait()
    assert '以上附件在 周测卷 目录下' in item['text']
    assert item['text'].endswith('- ' + str(folder))
    assert task['status'] == 'running'


def test_folder_over_limit_removes_folder(env, monkeypatch):
    monkeypatch.setattr(agent_share, 'MAX_FILE_MB', 1)
    resp = share(env, TOKEN_SHARE, files=[('big.bin', b'x' * (1024 * 1024 + 10))],
                 organize='folder', folderName='big')
    assert resp.status_code == 413
    assert stagedDirs(env) == []


def test_folder_note_pushes_prompt_over_limit_400(env):
    resp = share(env, TOKEN_SHARE, files=[('a.txt', b'x')],
                 prompt='x' * 3990, organize='folder', folderName='F')
    assert resp.status_code == 400
    assert '文件夹说明' in resp.get_json()['error']
    assert stagedDirs(env) == []