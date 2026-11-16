# -*- coding: utf-8 -*-
"""批次 P：并发上限按执行点各自计数（limitFor / 建任务只数该节点 / 429 payload /
GET /api/tasks 的 nodes[] / remotes[].maxRunning 保存校验）。

不启动真实 runner：_startTask 打桩，任务手动推进到 running 占名额。
"""
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules import task_state
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'p' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN.encode()).hexdigest()
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
    import time as _time
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def create(env, body):
    return env.client.post('/api/tasks', headers=H, data=json.dumps(body),
                           content_type='application/json')


def _useCfg(tmp_path, monkeypatch, cfg):
    """把 config 指到临时文件（load 按 mtime 热读，清缓存后立即可见）。"""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
    configMod._cache.update(mtime=None, data=None)
    return path


def _nodes(*specs):
    """[('A', 2), ('B', None)] → config.agentNodes 条目（url 占位，不真连）。"""
    out = []
    for i, (name, maxRunning) in enumerate(specs):
        item = {'name': name, 'url': 'http://127.0.0.1:%d' % (9000 + i)}
        if maxRunning is not None:
            item['maxRunning'] = maxRunning
        out.append(item)
    return out


def _running(env, taskId):
    """_startTask 已打桩：手动把任务推进到 running（占该节点的并发名额）。"""
    with env.repo.lock:
        task_state.transition(env.repo.get(taskId),
                              task_state.TaskState.RUNNING)


# ---- P1：limitFor 一处解析上限 ----

def test_limit_for_declared_default_zero_and_normalize(tmp_path, monkeypatch):
    _useCfg(tmp_path, monkeypatch,
            {'taskMaxRunning': 5,
             'agentNodes': _nodes(('A', 2), ('B', None), ('C', 0))})
    assert runner.limitFor('A') == 2            # 节点声明优先
    assert runner.limitFor('B') == 5            # 未声明 → 缺省
    assert runner.limitFor('C') == 0            # 0 = 不限
    assert runner.limitFor('local') == 5        # 本机未声明 → 缺省
    assert runner.limitFor(' A ') == 2          # 写法差异绕不过上限（normalize）
    assert runner.limitFor('') == 5             # 空 → local


def test_limit_for_default_is_eight(tmp_path, monkeypatch):
    _useCfg(tmp_path, monkeypatch, {'agentNodes': _nodes(('A', None))})
    assert runner.maxRunning() == 8 and runner.limitFor('A') == 8


def test_limit_for_local_node_declaration(tmp_path, monkeypatch):
    _useCfg(tmp_path, monkeypatch,
            {'localAgentNodePort': 9101, 'localAgentNodeMaxRunning': 3})
    assert runner.limitFor('local') == 3


def test_limit_for_bad_declaration_inherits(tmp_path, monkeypatch):
    """手改 config.json 写出非法 maxRunning：按未声明处理（继承），不禁用节点。"""
    _useCfg(tmp_path, monkeypatch,
            {'taskMaxRunning': 4,
             'agentNodes': _nodes(('A', 'abc'), ('B', -1), ('C', True))})
    assert runner.limitFor('A') == 4
    assert runner.limitFor('B') == 4
    assert runner.limitFor('C') == 4


# ---- P2：建任务只数该节点的 ----

def test_nodes_have_independent_limits(env, tmp_path, monkeypatch):
    """验收 1：A/B 各 maxRunning 2——A 满只挡 A，B 照样 202。"""
    _useCfg(tmp_path, monkeypatch,
            {'agentNodes': _nodes(('A', 2), ('B', 2))})
    a = [create(env, {'prompt': 'A-%d' % i, 'node': 'A', 'cwd': '/tmp'})
         for i in range(2)]
    assert [r.status_code for r in a] == [202, 202]
    for r in a:
        _running(env, r.get_json()['task']['id'])
    over = create(env, {'prompt': 'A-3', 'node': 'A', 'cwd': '/tmp'})
    assert over.status_code == 429
    body = over.get_json()
    assert body['node'] == 'A' and body['limit'] == 2 and body['running'] == 2
    assert 'A' in body['error']
    assert [h['id'] for h in body['holders']] == \
        [r.get_json()['task']['id'] for r in a]
    assert body['holders'][0]['owner'] == '测试智能体'
    # B 不受影响：同时照样建 2 个
    b = [create(env, {'prompt': 'B-%d' % i, 'node': 'B', 'cwd': '/tmp'})
         for i in range(2)]
    assert [r.status_code for r in b] == [202, 202]
    for r in b:
        _running(env, r.get_json()['task']['id'])
    overB = create(env, {'prompt': 'B-3', 'node': 'B', 'cwd': '/tmp'})
    assert overB.status_code == 429
    assert overB.get_json()['node'] == 'B'


def test_undeclared_node_inherits_default_eight(env, tmp_path, monkeypatch):
    """验收 2a：未声明的执行点继承缺省 8。"""
    _useCfg(tmp_path, monkeypatch, {'agentNodes': _nodes(('A', None))})
    for i in range(8):
        resp = create(env, {'prompt': 't%d' % i, 'node': 'A', 'cwd': '/tmp'})
        assert resp.status_code == 202
        _running(env, resp.get_json()['task']['id'])
    over = create(env, {'prompt': '第9个', 'node': 'A', 'cwd': '/tmp'})
    assert over.status_code == 429
    assert over.get_json()['limit'] == 8 and over.get_json()['running'] == 8


def test_zero_means_unlimited(env, tmp_path, monkeypatch):
    """验收 2b：maxRunning 0 = 不限，9 个都 202 且都停在 running。"""
    _useCfg(tmp_path, monkeypatch, {'agentNodes': _nodes(('D', 0))})
    for i in range(9):
        resp = create(env, {'prompt': 'd%d' % i, 'node': 'D', 'cwd': '/tmp'})
        assert resp.status_code == 202
        _running(env, resp.get_json()['task']['id'])
    with env.repo.lock:
        assert sum(1 for t in env.repo.list()
                   if t.get('status') == 'running') == 9


def test_holders_only_list_that_node(env, tmp_path, monkeypatch):
    """验收 4：429 的 holders 只含该节点的任务，不出现别的节点的 taskId。"""
    _useCfg(tmp_path, monkeypatch,
            {'agentNodes': _nodes(('A', 2), ('B', 2))})
    a = [create(env, {'prompt': 'A-%d' % i, 'node': 'A', 'cwd': '/tmp'})
         for i in range(2)]
    b = [create(env, {'prompt': 'B-%d' % i, 'node': 'B', 'cwd': '/tmp'})
         for i in range(2)]
    for r in a + b:
        _running(env, r.get_json()['task']['id'])
    over = create(env, {'prompt': 'B-3', 'node': 'B', 'cwd': '/tmp'})
    assert over.status_code == 429
    holderIds = [h['id'] for h in over.get_json()['holders']]
    assert holderIds == [r.get_json()['task']['id'] for r in b]
    assert all(r.get_json()['task']['id'] not in holderIds for r in a)


def test_counting_normalizes_node_and_ignores_non_running(env, tmp_path,
                                                          monkeypatch):
    """旧记录 node 写法不同（' A '）也归一进该节点；queued/waiting 不占名额。"""
    _useCfg(tmp_path, monkeypatch, {'agentNodes': _nodes(('A', 1))})
    first = create(env, {'prompt': '排队中', 'node': 'A', 'cwd': '/tmp'})
    assert first.status_code == 202                    # queued 不计数
    second = create(env, {'prompt': '又一个', 'node': 'A', 'cwd': '/tmp'})
    assert second.status_code == 202
    _running(env, first.get_json()['task']['id'])
    with env.repo.lock:                                # waiting 也不计数
        task_state.transition(env.repo.get(first.get_json()['task']['id']),
                              task_state.TaskState.WAITING)
    third = create(env, {'prompt': '第三个', 'node': 'A', 'cwd': '/tmp'})
    assert third.status_code == 202
    with env.repo.lock:                                # 旧记录：node 带空格
        env.repo.put({'id': 'legacy01', 'status': 'running', 'node': ' A ',
                      'createdAt': 0, 'events': [], 'title': '旧记录',
                      'owner': '旧主控'})
    fourth = create(env, {'prompt': '第四个', 'node': 'A', 'cwd': '/tmp'})
    assert fourth.status_code == 429                   # ' A ' 归一后算 A 的
    assert fourth.get_json()['node'] == 'A'


# ---- P3：列表端点 ----

def test_list_tasks_nodes_and_count_scope(env, tmp_path, monkeypatch):
    """验收 5：nodes[] 与实际 running 一致；countScope 不再说「全局」。"""
    _useCfg(tmp_path, monkeypatch,
            {'agentNodes': _nodes(('A', 2), ('B', 3))})
    a = create(env, {'prompt': 'A-1', 'node': 'A', 'cwd': '/tmp'})
    _running(env, a.get_json()['task']['id'])
    local = create(env, {'prompt': '本机活'})
    _running(env, local.get_json()['task']['id'])
    lst = env.client.get('/api/tasks', headers=H).get_json()
    assert lst['running'] == 2 and lst['queued'] == 0
    assert lst['limit'] == 8                           # 缺省上限保留（向后兼容）
    assert '全局' not in lst['countScope']
    assert lst['countScope'].startswith('per-node')
    byName = {n['name']: n for n in lst['nodes']}
    assert byName['A'] == {'name': 'A', 'running': 1, 'limit': 2}
    assert byName['B'] == {'name': 'B', 'running': 0, 'limit': 3}
    assert byName['local'] == {'name': 'local', 'running': 1, 'limit': 8}


# ---- P4：保存校验 ----

@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'port': 6788, 'password': 'test'}),
                    encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
    configMod._cache.update(mtime=None, data=None)
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c
    configMod._cache.update(mtime=None, data=None)


@pytest.mark.parametrize('bad', [-1, 'abc', 2.5, True])
def test_remotes_maxrunning_rejected(web, bad):
    """验收 3：maxRunning 非整数/负数 → 400，文案含字段名，且不落盘。"""
    resp = web.post('/api/settings/federation/remotes', json={'remotes': [
        {'id': 'r1', 'name': 'a', 'base': 'http://a:1', 'token': '',
         'maxRunning': bad}]})
    assert resp.status_code == 400
    assert 'maxRunning' in resp.get_json()['error']
    assert configMod.getRemotes() == []


def test_remotes_maxrunning_roundtrip(web):
    resp = web.post('/api/settings/federation/remotes', json={'remotes': [
        {'id': 'r1', 'name': 'a', 'base': 'http://a:1', 'token': '',
         'maxRunning': 2},
        {'id': 'r2', 'name': 'b', 'base': 'http://b:2', 'token': ''}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    remotes = configMod.getRemotes()
    assert remotes[0]['maxRunning'] == 2
    assert remotes[1]['maxRunning'] is None    # 缺省 = 不声明（继承 taskMaxRunning）
    got = web.get('/api/settings/federation').get_json()['remotes']
    assert got[0]['maxRunning'] == 2 and got[1]['maxRunning'] is None
