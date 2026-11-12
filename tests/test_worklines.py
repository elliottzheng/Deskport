# -*- coding: utf-8 -*-
"""工作线（R5-1a）：接力棒注入、轮末写回、自动路由与轮换。

不启动真实 runner：_startTask 打桩，任务停在 queued，专测契约与状态。
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules import worklines_store as wlStore
from modules.tasks_store import TaskRepository
from modules.worklines_store import WorklineRepository

TOKEN = 'tok-' + 'w' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN}
_counter = {'n': 0}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试主控',
                                  'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    wlRepo = WorklineRepository(str(tmp_path / 'worklines.json'),
                                str(tmp_path / 'wldocs'))
    monkeypatch.setattr(wlStore, 'repo', wlRepo)
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'shutdownNodeSession', lambda *a, **k: None)
    import time as _time
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    (tmp_path / 'ws').mkdir()
    return SimpleNamespace(client=app.test_client(), repo=repo, wlRepo=wlRepo,
                           tmp=tmp_path)


def post(env, path, body):
    return env.client.post(path, headers=H, data=json.dumps(body),
                           content_type='application/json')


def put(env, path, body):
    return env.client.put(path, headers=H, data=json.dumps(body),
                          content_type='application/json')


def newWorkline(env, title='R5 工作线', doc='# 接力棒\n- 进度：A 已完成',
                policy=None, **extra):
    body = {'title': title, 'doc': doc, 'policy': policy or {}}
    body.update(extra)
    resp = post(env, '/api/worklines', body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['workline']


def mkTask(env, **kw):
    _counter['n'] += 1
    task = {'id': kw.pop('id', 'task%02d' % _counter['n']), 'title': 't',
            'prompt': 'x', 'workspace': str(env.tmp / 'ws'), 'status': 'waiting',
            'createdAt': 0, 'events': [], 'seq': 0, 'sessionId': 's1',
            'node': 'local', 'resultFiles': []}
    task.update(kw)
    env.repo.put(task)
    return task


# ---------------------------------------------------------------- 仓储/接口

def test_create_and_get_workline(env):
    wl = newWorkline(env, repo=r'E:\proj', branch='workline')
    assert wl['id'] and wl['rev']
    assert wl['doc'].startswith('# 接力棒')
    assert wl['repo'] == r'E:\proj' and wl['branch'] == 'workline'
    listed = env.client.get('/api/worklines', headers=H).get_json()
    assert listed['count'] == 1
    assert listed['worklines'][0]['id'] == wl['id']
    detail = env.client.get('/api/worklines/%s' % wl['id'], headers=H).get_json()
    assert detail['workline']['doc'] == wl['doc']
    assert detail['workline']['nextAction'] == ''


def test_create_requires_title_and_rejects_dup_id(env):
    assert post(env, '/api/worklines', {'doc': 'x'}).status_code == 400
    first = newWorkline(env)
    dup = post(env, '/api/worklines', {'id': first['id'], 'title': 'x'})
    assert dup.status_code == 409


def test_update_doc_bumps_rev_and_guards_base_rev(env):
    wl = newWorkline(env)
    rev1 = wl['rev']
    resp = put(env, '/api/worklines/%s' % wl['id'],
               {'doc': '# v2\n- 新进度', 'baseRev': rev1})
    assert resp.status_code == 200
    rev2 = resp.get_json()['workline']['rev']
    assert rev2 != rev1
    stale = put(env, '/api/worklines/%s' % wl['id'],
                {'doc': '# v3', 'baseRev': rev1})
    assert stale.status_code == 409
    assert stale.get_json()['rev'] == rev2


def test_auth_required(env):
    anon = env.client.get('/api/worklines')
    assert anon.status_code in (401, 302)


# ---------------------------------------------------------------- 注入

def test_task_with_workline_injects_baton_and_rules(env):
    wl = newWorkline(env, doc='# 接力棒\nMARKER-R5-1\n- 下一步：跑测试',
                     policy={'default': 'allow'})
    resp = post(env, '/api/tasks', {'prompt': '继续推进', 'workline': wl['id']})
    assert resp.status_code == 202, resp.get_json()
    task = env.repo.get(resp.get_json()['task']['id'])
    assert task['workline']['id'] == wl['id']
    assert task['policy']['default'] == 'allow'      # 工作线边界成为任务默认
    prompt = store._promptFor(task, [], task['worklineBlock'])
    assert 'MARKER-R5-1' in prompt
    assert '不要使用后台任务' in prompt
    assert '允许路径' not in prompt    # 空 allowPaths 不写噪音
    assert 'WORKLINE.md' in prompt
    assert prompt.index('不要使用后台任务') < prompt.index('继续推进')
    with open(task['workspace'] + '/WORKLINE.md', encoding='utf-8') as f:
        baton = f.read()
    assert 'MARKER-R5-1' in baton
    assert wl['id'] in json.dumps(env.repo.summary(task)['workline'])


def test_oversize_baton_degrades_to_pointer(env):
    doc = '# 长接力棒\n' + '\n'.join('第 %03d 条进度' % i for i in range(600))
    wl = newWorkline(env, doc=doc)
    resp = post(env, '/api/tasks',
                {'prompt': 'x' * 3000, 'workline': wl['id']})
    task = env.repo.get(resp.get_json()['task']['id'])
    prompt = store._promptFor(task, [], task['worklineBlock'])
    assert 'WORKLINE.md' in prompt
    assert '第 599 条进度' not in prompt             # 正文没全塞进去
    assert len(prompt) <= wlStore.PROMPT_LIMIT + 120  # 预算守住
    assert task['workline']['injected'] in ('excerpt', 'pointer')
    with open(task['workspace'] + '/WORKLINE.md', encoding='utf-8') as f:
        full = f.read()
    assert '第 599 条进度' in full                   # 完整版在工作区


def test_plain_task_prompt_has_no_baton(env):
    resp = post(env, '/api/tasks', {'prompt': '普通任务'})
    task = env.repo.get(resp.get_json()['task']['id'])
    prompt = store._promptFor(task, [], task.get('worklineBlock') or '')
    assert '工作线接力棒' not in prompt
    assert '不要使用后台任务' not in prompt


def test_workline_missing_is_404(env):
    resp = post(env, '/api/tasks', {'prompt': 'x', 'workline': 'ghost'})
    assert resp.status_code == 404


# ---------------------------------------------------------------- 写回

def test_sync_from_workspace_only_when_changed(env):
    wl = newWorkline(env, doc='# v1')
    task = mkTask(env, workline={'id': wl['id'], 'rev': wl['rev']})
    path = env.tmp / 'ws' / 'WORKLINE.md'
    path.write_text('# v1', encoding='utf-8')
    assert wlStore.syncFromWorkspace(task) is None          # 没改就不写
    path.write_text('# v2\n- 执行方更新', encoding='utf-8')
    result = wlStore.syncFromWorkspace(task)
    assert result['rev'] != wl['rev']
    assert wlStore.repo.readDoc(wl).startswith('# v2')
    assert task['workline']['rev'] == result['rev']


def test_sync_skips_when_master_edited_mid_turn(env):
    wl = newWorkline(env, doc='# v1')
    stored = env.wlRepo.get(wl['id'])
    task = mkTask(env, workline={'id': wl['id'], 'rev': wl['rev']})
    (env.tmp / 'ws' / 'WORKLINE.md').write_text('# 执行方版本', encoding='utf-8')
    env.wlRepo.writeDoc(stored, '# 主控版本', 'master')     # 轮中主控改过
    result = wlStore.syncFromWorkspace(task)
    assert result['skipped'] == 'conflict'
    assert env.wlRepo.readDoc(stored) == '# 主控版本'        # 不覆盖


def test_turn_tail_syncs_master_update_into_workspace(env):
    wl = newWorkline(env, doc='# v1')
    stored = env.wlRepo.get(wl['id'])
    task = mkTask(env, workline={'id': wl['id'], 'rev': wl['rev']})
    (env.tmp / 'ws' / 'WORKLINE.md').write_text('# v1', encoding='utf-8')
    env.wlRepo.writeDoc(stored, '# v2 主控改的', 'master')
    tail = wlStore.turnTail(task)
    assert '已被主控更新' in tail and 'WORKLINE.md' in tail
    assert (env.tmp / 'ws' / 'WORKLINE.md').read_text(encoding='utf-8').startswith('# v2')


def test_after_turn_emits_baton_and_context_events(env):
    wl = newWorkline(env, doc='# v1', contextWarn=0.5)
    task = mkTask(env, workline={'id': wl['id'], 'rev': wl['rev']},
                  usage={'used': 800, 'size': 1000})
    (env.tmp / 'ws' / 'WORKLINE.md').write_text('# v2\n- 完成', encoding='utf-8')
    runner._afterWorklineTurn(task)
    types = [e.get('text') or '' for e in task['events'] if e['type'] == 'status']
    assert any('接力棒已更新' in t for t in types)
    assert any('POST /api/worklines/%s/rotate' % wl['id'] in t for t in types)
    assert task['contextHigh'] is True


# ---------------------------------------------------------------- 路由/轮换

def test_routing_waiting_goes_to_message(env, monkeypatch):
    wl = newWorkline(env)
    task = mkTask(env)
    wlStore.repo.setCurrent(wl['id'], task['id'])
    calls = []
    monkeypatch.setattr(runner, '_resumeWithMessage',
                        lambda t, text: calls.append(text))
    resp = post(env, '/api/worklines/%s/tasks' % wl['id'], {'prompt': '接着来'})
    assert resp.status_code == 202
    assert resp.get_json()['routed'] == 'message'
    assert calls == ['接着来']


def test_routing_queued_task_enqueues(env):
    wl = newWorkline(env)
    task = mkTask(env, status='queued', sessionId='')
    wlStore.repo.setCurrent(wl['id'], task['id'])
    resp = post(env, '/api/worklines/%s/tasks' % wl['id'], {'prompt': '排队'})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['routed'] == 'queued' and body['position'] == 1
    assert (task.get('messageQueue') or [])[0]['text'] == '排队'


def test_worklines_api_rejects_engine_param(env):
    """engine 已从对外面移除：建线/改线传了都 400。"""
    resp = post(env, '/api/worklines', {'title': 'x', 'engine': 'acp'})
    assert resp.status_code == 400
    assert 'node' in resp.get_json()['error']
    wl = newWorkline(env)
    resp = put(env, '/api/worklines/%s' % wl['id'], {'engine': 'acp'})
    assert resp.status_code == 400


def test_legacy_workline_engine_maps_to_node(env, monkeypatch):
    """旧工作线只有 engine 时：acp→local、remote→原 node；remote 缺 node 报错。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: {'name': name})
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)

    def legacy(wid, engine, node=''):
        """模拟旧盘上只有 engine 的工作线元数据（新建接口不再写 engine）。"""
        wl = env.wlRepo.create({'id': wid, 'title': wid, 'node': node})
        with env.wlRepo.lock:
            wl['engine'] = engine
            env.wlRepo.save()
        return wl

    wl = legacy('legacy-acp', 'acp')
    resp = post(env, '/api/worklines/%s/tasks' % wl['id'], {'prompt': '开工'})
    assert resp.status_code == 202, resp.get_json()
    assert env.repo.get(resp.get_json()['task']['id'])['node'] == 'local'

    wl2 = legacy('legacy-remote', 'remote', 'ai-xmyz')
    resp = post(env, '/api/worklines/%s/tasks' % wl2['id'],
                {'prompt': '开工', 'cwd': '/mnt/x'})
    assert resp.status_code == 202, resp.get_json()
    assert env.repo.get(resp.get_json()['task']['id'])['node'] == 'ai-xmyz'

    wl3 = legacy('legacy-nonode', 'remote')
    resp = post(env, '/api/worklines/%s/tasks' % wl3['id'], {'prompt': '开工'})
    assert resp.status_code == 400
    assert 'node' in resp.get_json()['error']


def test_routing_closed_task_starts_new_with_baton(env):
    wl = newWorkline(env, doc='DOC-NEW')
    task = mkTask(env, status='closed')
    wlStore.repo.setCurrent(wl['id'], task['id'])
    resp = post(env, '/api/worklines/%s/tasks' % wl['id'],
                {'prompt': '开新会话'})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['routed'] == 'new' and body['task']['id'] != task['id']
    newTask = env.repo.get(body['task']['id'])
    assert 'DOC-NEW' in newTask['worklineBlock']
    assert wlStore.repo.get(wl['id'])['currentTask'] == newTask['id']


def test_rotate_closes_old_and_carries_baton(env):
    wl = newWorkline(env, doc='ROTATE-MARKER')
    old = mkTask(env, status='waiting')
    wlStore.repo.setCurrent(wl['id'], old['id'])
    resp = post(env, '/api/worklines/%s/rotate' % wl['id'], {})
    assert resp.status_code == 202, resp.get_json()
    body = resp.get_json()
    assert body['closedTaskId'] == old['id']
    assert old['status'] == 'closed'
    newTask = env.repo.get(body['task']['id'])
    assert 'ROTATE-MARKER' in newTask['worklineBlock']
    assert wlStore.repo.get(wl['id'])['currentTask'] == newTask['id']


def test_next_action_rotate_when_context_high(env):
    wl = newWorkline(env, contextWarn=0.6)
    task = mkTask(env, usage={'used': 900, 'size': 1000})
    wlStore.repo.setCurrent(wl['id'], task['id'])
    detail = env.client.get('/api/worklines/%s' % wl['id'], headers=H).get_json()
    assert detail['workline']['nextAction'] == 'rotate'
    assert detail['workline']['context']['ratio'] == 0.9


def test_delete_workline_removes_doc(env):
    wl = newWorkline(env)
    assert env.client.delete('/api/worklines/%s' % wl['id'],
                             headers=H).status_code == 200
    assert wlStore.repo.get(wl['id']) is None
    assert not (env.tmp / 'wldocs' / (wl['id'] + '.md')).exists()


# ---------------------------------------------------------------- 轮次编排

def test_session_thread_sends_and_records_baton(env):
    """runner 真链路：首条 prompt 带接力棒，user 事件也落完整首条消息。"""
    wl = newWorkline(env, doc='THREAD-MARKER')
    stored = env.wlRepo.get(wl['id'])
    block, _mode = wlStore.composeBlock(stored, env.wlRepo.readDoc(stored), 3000)
    task = mkTask(env, prompt='干活', status='queued', sessionId='',
                  workline={'id': wl['id'], 'rev': wl['rev']},
                  worklineBlock=block)
    record = []

    class Client:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0, model=''):
            self.stderr_lines = []

        def start(self, session_id=None):
            return 'sid1'

        def prompt(self, text, timeout=None):
            record.append(text)
            return {'stopReason': 'end_turn'}

        def set_mode(self, mode):
            pass

        def cancel(self):
            pass

        def close(self):
            pass

        def shutdown(self):
            pass

    threading.Thread(target=runner._sessionThread, args=(task, []),
                     kwargs={'clientFactory': Client}, daemon=True).start()
    for _ in range(100):
        if record:
            break
        time.sleep(0.05)
    assert record and 'THREAD-MARKER' in record[0]
    users = [e['text'] for e in task['events'] if e.get('type') == 'user']
    assert 'THREAD-MARKER' in users[0]
    assert '不要使用后台任务' in users[0]


# ---------------------------------------------------------------- 纯函数

def test_policy_summary_is_compact():
    text = wlStore.policySummary({'default': 'ask', 'allowPaths': ['E:\\a'],
                                  'allowCommands': ['^git '], 'denyCommands': [],
                                  'allowBackground': False})
    assert '默认 ask' in text and 'E:\\a' in text and '后台任务：禁用' in text
    assert '未设置 policy' in wlStore.policySummary({})


def test_compose_block_empty_doc_still_has_contract():
    block, mode = wlStore.composeBlock({'id': 'w1', 'title': 't', 'rev': '',
                                        'policy': {}}, '', 4000)
    assert mode == 'empty'
    assert '不要使用后台任务' in block and 'WORKLINE.md' in block