# -*- coding: utf-8 -*-
"""网页分享表单：ticket 暂存 + Cookie 会话提交（/api/share/*）。

覆盖: stage 鉴权与校验、ticket 读取不消费、一次性、过期删暂存、
三种 way（upload / new / append）、organize=folder 继承、subdir 校验、
未登录 401、closed 任务 409 后可重试。
全部桩化，不碰真机与真实配置。
"""
import hashlib
import io
import pathlib
import queue
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules import share_web
from modules.tasks_store import TaskRepository

TOKEN_SHARE = 'tok-share-' + 'd' * 16
TOKEN_UPLOAD = 'tok-up-' + 'c' * 16


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


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
    ])
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(configMod, 'getUploadTargets', lambda: [
        {'id': 'phone', 'name': '手机上传', 'path': str(tmp_path / 'phone')},
    ])
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 20)
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
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    anonymous = app.test_client()
    return SimpleNamespace(client=client, anonymous=anonymous, audit=audit,
                           repo=repo, tmp=tmp_path, app=app)


def stage(env, files=(), prompt='', token=TOKEN_SHARE):
    data = {}
    if prompt:
        data['prompt'] = prompt
    if files:
        data['file'] = [(io.BytesIO(raw), name) for name, raw in files]
    headers = {} if token is None else {'X-Agent-Token': token}
    return env.client.post('/api/agent/share/stage', data=data, headers=headers,
                           content_type='multipart/form-data')


def staged(env, files=(), prompt=''):
    resp = stage(env, files=files, prompt=prompt)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def submit(env, ticket, client=None, **body):
    body['ticket'] = ticket
    return (client or env.client).post('/api/share/submit', json=body)


def seedTask(env, **over):
    task = {'id': 'task-' + 'b' * 8, 'title': '旧任务', 'prompt': '旧描述',
            'status': 'waiting', 'sessionId': 'sess-1', 'events': [], 'seq': 0,
            'createdAt': time.time() - 60, 'lastActiveAt': time.time() - 10,
            'workspace': str(env.tmp / 'ws'), 'node': 'local',
            'mode': 'default', 'resultFiles': []}
    task.update(over)
    env.repo.put(task)
    env.repo.save()
    return task


class _Client(object):
    def cancel(self):
        pass


def attachAgent(env, tid):
    agent = {'queue': queue.Queue(), 'client': None}
    env.repo.setAgent(tid, agent)
    return agent


def batches(env):
    root = env.tmp / 'taskroot' / '_uploads'
    return list(root.iterdir()) if root.is_dir() else []


# ---- 暂存入口 ----

def test_stage_requires_share_scope(env):
    assert stage(env, files=[('a.txt', b'x')], token=None).status_code == 401
    denied = stage(env, files=[('a.txt', b'x')], token=TOKEN_UPLOAD)
    assert denied.status_code == 403
    assert denied.get_json()['need'] == 'share'
    assert batches(env) == []


def test_stage_returns_ticket_and_metadata(env):
    body = staged(env, files=[('讲义一.pdf', b'%PDF-1'),
                              ('讲义二.pdf', b'%PDF-2')],
                  prompt='合成一份周测卷')
    assert len(body['ticket']) == 32
    assert [f['name'] for f in body['files']] == ['讲义一.pdf', '讲义二.pdf']
    assert [f['size'] for f in body['files']] == [6, 6]
    assert abs(body['expiresAt'] - (time.time() + 300)) <= 5
    assert len(batches(env)) == 1


def test_stage_rejects_bad_input(env):
    tooMany = [('f%d.txt' % i, b'x') for i in range(11)]
    assert stage(env, files=tooMany).status_code == 400
    assert stage(env, files=[('sub/evil.txt', b'x')]).status_code == 400
    assert stage(env).status_code == 400
    assert stage(env, prompt='x' * 4001).status_code == 400
    assert batches(env) == []


def test_stage_info_requires_login(env):
    body = staged(env, files=[('a.txt', b'x')], prompt='看看')
    assert env.anonymous.get('/api/share/stage/' + body['ticket']).status_code == 401


def test_stage_info_reads_without_consuming(env):
    body = staged(env, files=[('a.txt', b'x')], prompt='看看')
    first = env.client.get('/api/share/stage/' + body['ticket'])
    second = env.client.get('/api/share/stage/' + body['ticket'])
    assert first.status_code == second.status_code == 200
    assert first.get_json()['files'][0]['name'] == 'a.txt'
    assert first.get_json()['prompt'] == '看看'
    assert 'ticket' not in first.get_json()
    assert len(batches(env)) == 1


def test_submit_requires_login(env):
    body = staged(env, files=[('a.txt', b'x')])
    assert submit(env, body['ticket'], client=env.anonymous,
                  way='new').status_code == 401


# ---- 三个去处 ----

def test_way_new_creates_task_and_ticket_is_one_time(env):
    body = staged(env, files=[('讲义一.pdf', b'%PDF-1'),
                              ('讲义二.pdf', b'%PDF-2')],
                  prompt='合成一份周测卷')
    resp = submit(env, body['ticket'], way='new', mode='auto')
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out['mode'] == 'created' and out['taskId']
    task = env.repo.get(out['taskId'])
    assert task['prompt'] == '合成一份周测卷'
    assert task['mode'] == 'auto'
    assert all(pathlib.Path(p).is_file() for p in out['paths'])

    again = submit(env, body['ticket'], way='new', mode='auto')
    assert again.status_code == 400
    assert 'ticket' in again.get_json()['error']


def test_prompt_comes_from_stage_when_submit_omits_it(env):
    body = staged(env, files=[('a.txt', b'x')], prompt='整理成表格')
    out = submit(env, body['ticket'], way='new').get_json()
    assert env.repo.get(out['taskId'])['prompt'] == '整理成表格'


def test_way_append_delivers_message(env):
    seedTask(env, id='task-wait')
    agent = attachAgent(env, 'task-wait')
    body = staged(env, files=[('表.xlsx', b'data')], prompt='按上次的表格重算')
    resp = submit(env, body['ticket'], way='append', taskId='task-wait')
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out['mode'] == 'appended' and out['taskId'] == 'task-wait'
    item = agent['queue'].get_nowait()
    assert item['text'].startswith('按上次的表格重算\n\n附件：\n- ')
    assert out['paths'][0] in item['text']


def test_way_append_requires_task_id(env):
    body = staged(env, prompt='补充一句')
    resp = submit(env, body['ticket'], way='append')
    assert resp.status_code == 400
    assert 'taskId' in resp.get_json()['error']
    # 票据没被烧掉：补上 taskId 还能用
    seedTask(env, id='task-2')
    attachAgent(env, 'task-2')
    assert submit(env, body['ticket'], way='append',
                  taskId='task-2').status_code == 200


def test_append_closed_task_409_then_retry(env):
    seedTask(env, id='task-closed', status='closed')
    body = staged(env, files=[('a.txt', b'x')], prompt='x')
    assert submit(env, body['ticket'], way='append',
                  taskId='task-closed').status_code == 409
    seedTask(env, id='task-open')
    attachAgent(env, 'task-open')
    assert submit(env, body['ticket'], way='append',
                  taskId='task-open').status_code == 200


def test_way_upload_moves_files_into_subdir(env):
    body = staged(env, files=[('卷一.pdf', b'one'), ('卷二.pdf', b'two')])
    resp = submit(env, body['ticket'], way='upload', subdir='周测卷')
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out['ok'] is True
    paths = [item['path'] for item in out['uploaded']]
    assert paths == ['手机上传/周测卷/卷一.pdf', '手机上传/周测卷/卷二.pdf']
    for rel in paths:
        parts = rel.split('/')[1:]
        assert (env.tmp / 'phone').joinpath(*parts).is_file()
    assert batches(env) == []
    assert submit(env, body['ticket'], way='upload',
                  subdir='周测卷').status_code == 400


def test_way_upload_bad_subdir_400_and_retry(env):
    body = staged(env, files=[('a.txt', b'x')])
    assert submit(env, body['ticket'], way='upload',
                  subdir='../escape').status_code == 400
    assert submit(env, body['ticket'], way='upload',
                  subdir='C:\\abs').status_code == 400
    assert submit(env, body['ticket'], way='upload').status_code == 200


def test_chinese_names_spaces_and_duplicates(env):
    body = staged(env, files=[('周测 卷一.pdf', b'a'), ('周测 卷一.pdf', b'b')])
    assert [f['name'] for f in body['files']] == ['周测 卷一.pdf', '周测 卷一(2).pdf']
    out = submit(env, body['ticket'], way='upload', subdir='周测 卷').get_json()
    assert [item['path'] for item in out['uploaded']] == [
        '手机上传/周测 卷/周测 卷一.pdf',
        '手机上传/周测 卷/周测 卷一(2).pdf',
    ]


# ---- organize=folder ----

def test_folder_mode_inherits_semantics(env):
    body = staged(env, files=[('讲义一.pdf', b'%PDF-1'),
                              ('讲义二.pdf', b'%PDF-2')],
                  prompt='合成一份周测卷')
    resp = submit(env, body['ticket'], way='new', organize='folder',
                  folderName='周测卷')
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    folder = pathlib.Path(out['paths'][0])
    assert folder.is_dir() and folder.name == '周测卷'
    assert sorted(p.name for p in folder.iterdir()) == ['讲义一.pdf', '讲义二.pdf']
    assert out['prompt'].endswith('以上附件在 周测卷 目录下')
    inputs = pathlib.Path(env.repo.get(out['taskId'])['workspace']) / 'inputs' / '周测卷'
    assert inputs.is_dir()
    assert not [p for p in batches(env) if p.name.startswith('stage-')]

    second = staged(env, files=[('b.txt', b'x')])
    out2 = submit(env, second['ticket'], way='new', organize='folder',
                  folderName='周测卷').get_json()
    assert pathlib.Path(out2['paths'][0]).name == '周测卷(2)'
    assert out2['prompt'].endswith('以上附件在 周测卷(2) 目录下')


def test_folder_bad_name_400_ticket_retryable(env):
    body = staged(env, files=[('a.txt', b'x')])
    assert submit(env, body['ticket'], way='new', organize='folder',
                  folderName='../x').status_code == 400
    assert submit(env, body['ticket'], way='new', organize='folder',
                  folderName='好名字').status_code == 200


# ---- 票据时效 ----

def test_ticket_expiry_deletes_staged_files(env, monkeypatch):
    monkeypatch.setattr(share_web, 'TICKET_TTL', 0)
    body = staged(env, files=[('a.txt', b'x')])
    assert len(batches(env)) == 1
    resp = env.client.get('/api/share/stage/' + body['ticket'])
    assert resp.status_code == 400
    assert '过期' in resp.get_json()['error']
    assert batches(env) == []
    assert submit(env, body['ticket'], way='new', prompt='x').status_code == 400


def test_submit_expired_ticket_400(env, monkeypatch):
    monkeypatch.setattr(share_web, 'TICKET_TTL', 0)
    body = staged(env, files=[('a.txt', b'x')])
    assert submit(env, body['ticket'], way='new').status_code == 400
    assert batches(env) == []


# ---- 其他校验 ----

def test_invalid_way_400_ticket_survives(env):
    body = staged(env, files=[('a.txt', b'x')])
    assert submit(env, body['ticket'], way='zip').status_code == 400
    assert submit(env, body['ticket'], way='new').status_code == 200


def test_invalid_mode_400(env):
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='new', mode='yolo')
    assert resp.status_code == 400 and 'mode' in resp.get_json()['error']


def test_nodes_and_tasks_require_login_and_return_payload(env):
    assert env.anonymous.get('/api/share/nodes').status_code == 401
    assert env.anonymous.get('/api/share/tasks').status_code == 401
    nodes = env.client.get('/api/share/nodes').get_json()
    assert nodes['default'] == 'local'
    assert nodes['nodes'][0]['name'] == 'local'
    seedTask(env, id='task-list')
    tasks = env.client.get('/api/share/tasks').get_json()
    assert any(t['id'] == 'task-list' for t in tasks['tasks'])


def test_audit_written(env):
    body = staged(env, files=[('a.txt', b'x')])
    submit(env, body['ticket'], way='new')
    actions = [a['action'] for a in env.audit]
    assert 'share.stage' in actions
    assert 'share.submit' in actions
    assert 'agent.share' in actions


def test_page_requires_login_and_serves_form(env):
    anon = env.anonymous.get('/m/share?ticket=abc')
    assert anon.status_code == 302
    assert '/login?next=' in anon.headers['Location']

    page = env.client.get('/m/share?ticket=abc')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for marker in ('id="way-new"', 'id="way-append"', 'id="way-upload"',
                   'id="share-submit"', 'id="share-result"',
                   'id="share-task-link"', '/assets/js/m-share.js'):
        assert marker in html, marker