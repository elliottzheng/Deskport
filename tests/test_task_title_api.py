# -*- coding: utf-8 -*-
"""批次 T 任务标题（接口层）：建卡显式/推导标题（例 1/2/3）、工作线派活（例 6）、
分享透传、PATCH 改名（例 5，归属/审计）、老任务输出（例 4，429 占用明细与
指挥台出问题行）。

不启动真实 runner：_startTask 打桩，任务停在 queued，专测标题契约。
纯函数规则（deriveTitle 表格等）在 test_task_title.py。
"""
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import command_view, events as eventsMod, leads_store, share_core
from modules import tasks_api
from modules import tasks_store as store
from modules import worklines_store as wlStore
from modules.tasks_store import TaskRepository
from modules.worklines_store import WorklineRepository

PASSWORD = 'pw-123456'
NOW = 1_800_000_000.0            # 固定"现在"：24h 问题窗口断言不看真时钟（照 test_command_view.py）
BASE_CFG = {'password': PASSWORD, 'port': 6789,
            'sessiondUrl': 'http://127.0.0.1:63991',
            'sshSessiondUrl': 'http://127.0.0.1:63992',
            'localAgentNodePort': 1}
TOKEN = 'tok-' + 't' * 24
TOKEN2 = 'tok-' + 'u' * 24
TOKEN3 = 'tok-' + 'v' * 24
TOKEN4 = 'tok-' + 'w' * 24
H = {'X-Agent-Token': TOKEN}
H2 = {'X-Agent-Token': TOKEN2}
H3 = {'X-Agent-Token': TOKEN3}
H4 = {'X-Agent-Token': TOKEN4}


def _hash(token):
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps(BASE_CFG), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    audit = []
    tokens = [{'id': 't1', 'name': '测试智能体', 'tokenHash': _hash(TOKEN)},
              {'id': 't2', 'name': '另一智能体', 'tokenHash': _hash(TOKEN2)}]
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: audit.append((a, k)))
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: tokens)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    wlRepo = WorklineRepository(str(tmp_path / 'worklines.json'),
                                str(tmp_path / 'wldocs'))
    monkeypatch.setattr(wlStore, 'repo', wlRepo)
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    # 冻结 command_view 的"现在"：出问题行的 24h 窗口不看真时钟
    monkeypatch.setattr(command_view, 'time',
                        SimpleNamespace(time=lambda: NOW))
    import time as _time
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           repo=repo, wlRepo=wlRepo, tmp=tmp_path, app=app,
                           audit=audit, tokens=tokens)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None, token=None, headers=None):
    client = env.fresh if (token or headers) else env.client
    kw = {'headers': headers or ({'X-Agent-Token': token} if token else {})}
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(client, method)(path, **kw)


def create(env, body, headers=H):
    return call(env, 'post', '/api/tasks', body, headers=headers)


# ---------------------------------------------------------------- 建卡（例 1/2/3）

def test_create_with_explicit_title(env):
    """例 2：给 title → 标题与 titleSource=given，响应回显。"""
    resp = create(env, {'prompt': '提示词随便写', 'title': '验收 R'})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['task']['title'] == '验收 R'
    assert body['titleSource'] == 'given'
    assert env.repo.get(body['task']['id'])['titleSource'] == 'given'


def test_create_title_truncated_to_40(env):
    """例 3：45 字 title 截到 40。"""
    resp = create(env, {'prompt': 'x', 'title': '标' * 45})
    assert resp.status_code == 202
    assert resp.get_json()['task']['title'] == '标' * 40


def test_create_derives_title_from_prompt(env):
    """例 1：不给 title 按规则表推导。"""
    resp = create(env, {'prompt': '【目标】实施「批次 Q：主管记录 + 令牌绑定 + '
                                  '任务自动记归属」。'})
    assert resp.status_code == 202
    assert resp.get_json()['task']['title'] \
        == '实施「批次 Q：主管记录 + 令牌绑定 + 任…'
    assert resp.get_json()['titleSource'] == 'derived'


def test_create_rejects_non_string_title(env):
    """title 不是字符串 → 400，不猜。"""
    assert create(env, {'prompt': 'x', 'title': 123}).status_code == 400
    assert create(env, {'prompt': 'x', 'title': ['a']}).status_code == 400


# ---------------------------------------------------------------- 工作线派活（例 6）

def newWorkline(env, title):
    resp = call(env, 'post', '/api/worklines', {'title': title}, token=TOKEN)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['workline']['id']


def test_workline_dispatch_title_composition(env):
    """例 6：工作线派活 → `<工作线标题> · <默认标题>`。"""
    wid = newWorkline(env, '批次 T 工作线')
    resp = call(env, 'post', '/api/worklines/%s/tasks' % wid,
                {'prompt': '【目标】推进任务标题改造。'}, token=TOKEN)
    assert resp.status_code == 202
    assert resp.get_json()['task']['title'] == '批次 T 工作线 · 推进任务标题改造'
    assert resp.get_json()['titleSource'] == 'derived'


def test_workline_dispatch_explicit_title_wins(env):
    """派活显式给 title → 原样用（given），不套工作线规则。"""
    wid = newWorkline(env, '批次 T 工作线')
    resp = call(env, 'post', '/api/worklines/%s/tasks' % wid,
                {'prompt': '【目标】推进任务标题改造。', 'title': '标题改造'},
                token=TOKEN)
    assert resp.status_code == 202
    assert resp.get_json()['task']['title'] == '标题改造'
    assert resp.get_json()['titleSource'] == 'given'


def test_workline_dispatch_title_equal_uses_workline_name(env):
    """默认标题与工作线标题相同 → 只留工作线标题（不重复）。"""
    wid = newWorkline(env, '整理简历')
    resp = call(env, 'post', '/api/worklines/%s/tasks' % wid,
                {'prompt': '整理简历'}, token=TOKEN)
    assert resp.status_code == 202
    assert resp.get_json()['task']['title'] == '整理简历'


def test_workline_rotate_passes_title(env):
    """rotate 同样接受 title（透传给新任务）。"""
    wid = newWorkline(env, '批次 T 工作线')
    resp = call(env, 'post', '/api/worklines/%s/rotate' % wid,
                {'prompt': '接棒继续', 'title': '轮换后的标题'}, token=TOKEN)
    assert resp.status_code == 202
    assert resp.get_json()['task']['title'] == '轮换后的标题'


# ---------------------------------------------------------------- 分享透传

def test_share_options_title_and_limit():
    """parseShareOptions：title 原样带回；超 40 字 400。"""
    opts, err = share_core.parseShareOptions(
        {'prompt': '整理简历', 'title': '简历整理'}, 0)
    assert err is None and opts['title'] == '简历整理'
    opts, err = share_core.parseShareOptions(
        {'prompt': '整理简历', 'title': 'x' * 41}, 0)
    assert opts is None and err[1] == 400


def test_share_create_passes_title(env):
    """/api/agent/share 建任务带上 title → given。"""
    opts, err = share_core.parseShareOptions(
        {'prompt': '整理简历', 'title': '简历整理'}, 0)
    assert err is None
    with env.app.test_request_context('/api/agent/share', method='POST'):
        payload, code = share_core.deliverShare(opts, [])
    assert code == 200
    task = env.repo.get(payload['taskId'])
    assert task['title'] == '简历整理' and task['titleSource'] == 'given'


# ---------------------------------------------------------------- PATCH 改名（例 5）

def test_patch_rename_web_session(env):
    """例 5：网页会话改名 → 列表/详情立即是新名 + 审计 + 任务事件。"""
    login(env.client)
    task = create(env, {'prompt': '【目标】做甲事。'}).get_json()['task']
    resp = call(env, 'patch', '/api/tasks/' + task['id'], {'title': '甲事'})
    assert resp.status_code == 200
    assert resp.get_json()['title'] == '甲事'
    assert resp.get_json()['titleSource'] == 'given'
    detail = call(env, 'get', '/api/tasks/' + task['id']).get_json()['task']
    assert detail['title'] == '甲事'
    row = call(env, 'get', '/api/tasks?full=1').get_json()['tasks'][0]
    assert row['title'] == '甲事'
    renamed = [a for a, _k in env.audit if a[0] == 'agent.task.rename']
    assert renamed and renamed[0][1] == '面板'
    stored = env.repo.get(task['id'])
    assert stored['titleSource'] == 'given'
    assert any(e.get('text') == '任务已改名: 甲事'
               for e in stored.get('events') or [])


def test_patch_rename_token_owner(env):
    """令牌改自己建的任务 → 允许，审计记令牌名。"""
    task = create(env, {'prompt': 'x'}).get_json()['task']
    resp = call(env, 'patch', '/api/tasks/' + task['id'], {'title': '我的活'},
                token=TOKEN)
    assert resp.status_code == 200
    assert env.repo.get(task['id'])['title'] == '我的活'
    renamed = [a for a, _k in env.audit if a[0] == 'agent.task.rename']
    assert renamed and renamed[0][1] == '测试智能体'


def test_patch_rename_token_not_owner_403(env):
    """例 5：令牌改别人的任务 → 403。"""
    task = create(env, {'prompt': 'x'}).get_json()['task']
    resp = call(env, 'patch', '/api/tasks/' + task['id'], {'title': '抢活'},
                token=TOKEN2)
    assert resp.status_code == 403
    assert env.repo.get(task['id'])['title'] != '抢活'


def test_patch_rename_token_bound_lead(env):
    """令牌绑了主管 → 可改该主管名下的任务（含别人代建的）。"""
    lead, err = leads_store.create('甲主管')
    assert not err
    env.tokens.append({'id': 't3', 'name': '甲的分身甲',
                       'tokenHash': _hash(TOKEN3), 'lead': lead['id']})
    env.tokens.append({'id': 't4', 'name': '甲的分身乙',
                       'tokenHash': _hash(TOKEN4), 'lead': lead['id']})
    task = create(env, {'prompt': 'x'}, headers=H3).get_json()['task']
    assert task['lead'] == lead['id']
    resp = call(env, 'patch', '/api/tasks/' + task['id'], {'title': '主管的活'},
                token=TOKEN4)
    assert resp.status_code == 200
    assert env.repo.get(task['id'])['title'] == '主管的活'


def test_patch_rename_empty_title_400(env):
    """改名为空 → 400（不能把标题改成空串）。"""
    login(env.client)
    task = create(env, {'prompt': 'x'}).get_json()['task']
    assert call(env, 'patch', '/api/tasks/' + task['id'],
                {'title': '  '}).status_code == 400


def test_patch_rename_unknown_task_404(env):
    login(env.client)
    assert call(env, 'patch', '/api/tasks/nope', {'title': 'x'}).status_code == 404


def test_patch_rename_unknown_key_400(env):
    """只认 title 键，别的键 400（不做部分更新）。"""
    login(env.client)
    task = create(env, {'prompt': 'x'}).get_json()['task']
    assert call(env, 'patch', '/api/tasks/' + task['id'],
                {'prompt': '改提示词'}).status_code == 400


# ---------------------------------------------------------------- 老任务与输出（例 4）

OLD_PROMPT = '【目标】实施「批次 Q：主管记录 + 令牌绑定 + 任务自动记归属」。'
OLD_EXPECTED = '实施「批次 Q：主管记录 + 令牌绑定 + 任…'


def putOldTask(env, tid='old01', **kw):
    """老任务：无 titleSource，title 还是提示词前 40 字。"""
    task = {'id': tid, 'title': OLD_PROMPT[:40], 'prompt': OLD_PROMPT,
            'status': 'waiting', 'owner': '面板', 'lead': '', 'node': 'local',
            'mode': 'default', 'events': [], 'seq': 1, 'resultFiles': [],
            'workspace': 'ws', 'createdAt': 1, 'lastActiveAt': 1, 'endedAt': 0}
    task.update(kw)
    env.repo.put(task)
    return task


def test_old_task_derived_in_outputs(env):
    """例 4：老任务列表/overview/详情都显示推导标题；存储不变。"""
    login(env.client)
    putOldTask(env)
    row = call(env, 'get', '/api/tasks?full=1').get_json()['tasks'][0]
    assert row['title'] == OLD_EXPECTED
    detail = call(env, 'get', '/api/tasks/old01').get_json()['task']
    assert detail['title'] == OLD_EXPECTED
    ov = call(env, 'get', '/api/command/overview').get_json()
    assert ov['unassigned']['tasks'][0]['title'] == OLD_EXPECTED
    stored = env.repo.get('old01')
    assert stored['title'] == OLD_PROMPT[:40]
    assert 'titleSource' not in stored


def test_given_title_output_as_is(env):
    """titleSource=given 的老任务原样输出（不重新推导）。"""
    login(env.client)
    putOldTask(env, 'old02', title='手起名', titleSource='given')
    row = call(env, 'get', '/api/tasks?full=1').get_json()['tasks'][0]
    assert row['title'] == '手起名'
    detail = call(env, 'get', '/api/tasks/old02').get_json()['task']
    assert detail['title'] == '手起名'


def test_429_holders_use_derived_title(env, monkeypatch):
    """并发上限 429 的占用明细：老任务也显示推导标题。"""
    (env.tmp / 'config.json').write_text(
        json.dumps(dict(BASE_CFG, taskMaxRunning=1)), encoding='utf-8')
    configMod._cache.update(mtime=None, data=None)
    putOldTask(env, 'run01', status='running', owner='测试智能体')
    resp = create(env, {'prompt': '另一个活'})
    assert resp.status_code == 429
    assert resp.get_json()['holders'][0]['title'] == OLD_EXPECTED


def test_command_view_problem_row_uses_derived_title(env):
    """出问题的下属行：老任务标题同样按新规则推导。"""
    login(env.client)
    putOldTask(env, 'bad01', status='failed', error='炸了', endedAt=NOW - 3600)
    ov = call(env, 'get', '/api/command/overview').get_json()
    row = next(p for p in ov['pending'] if p.get('taskId') == 'bad01')
    assert row['title'] == OLD_EXPECTED
