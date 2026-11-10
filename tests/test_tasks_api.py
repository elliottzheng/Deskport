# -*- coding: utf-8 -*-
"""任务 API 校验与响应契约（外部智能体实测反馈 P1 批次 + P2-20/21/22）。

不启动真实 runner：_startTask 打桩，任务停在 queued，专测校验与字段。
"""
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import tasks_api
from modules import tasks_store as store
from modules import task_state
from modules.tasks_store import TaskRepository

TOKEN = 'tok-' + 'c' * 24
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
    # 健康模型缓存：给"新鲜空值"——既不触发后台探测线程（真实网络），
    # 也杜绝跨测试互相污染
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


def test_remote_missing_node_is_400_with_nodes(env, monkeypatch):
    """反馈 §3.5：缺 node 400 + nodes 提示，不再 500 HTML。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: None)
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    monkeypatch.setattr(nodesMod, 'listNodeNames', lambda: ['local', 'ai-xmyz'])
    resp = create(env, {'prompt': 'x', 'engine': 'remote'})
    body = resp.get_json()
    assert resp.status_code == 400
    assert body['nodes'] == ['local', 'ai-xmyz']
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'ghost',
                        'cwd': '/tmp'})
    assert resp.status_code == 400
    assert 'ghost' in json.dumps(resp.get_json()['nodes']) or \
        resp.get_json()['nodes'] == ['local', 'ai-xmyz']


def test_remote_missing_cwd_is_400_not_accepted(env, monkeypatch):
    """反馈 §3.5：缺 cwd 直接 400，不再受理后异步失败。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name} if name == 'local' else None)
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'local'})
    assert resp.status_code == 400
    assert 'cwd' in resp.get_json()['error']


def test_acp_rejects_cwd(env):
    """反馈 §3.7：acp 不再静默忽略 cwd，直接说明拒绝原因。"""
    resp = create(env, {'prompt': 'x', 'engine': 'acp', 'cwd': r'E:\事项'})
    assert resp.status_code == 400
    assert 'acp' in resp.get_json()['error']


def test_default_mode_is_default_not_auto(env):
    """反馈 §3.6：不传 mode = 逐项审批（安全默认），auto 必须显式。"""
    resp = create(env, {'prompt': 'x'})
    assert resp.status_code == 202
    assert resp.get_json()['task']['mode'] == 'default'
    resp = create(env, {'prompt': 'x', 'mode': 'auto'})
    assert resp.get_json()['task']['mode'] == 'auto'


def test_prompt_limit_in_error_message(env):
    resp = create(env, {'prompt': 'x' * 4001})
    assert resp.status_code == 400
    assert '4000' in resp.get_json()['error']


def test_engine_p_removed_not_alias(env):
    """API 不做跨版本兼容（2026-11-10 决策）：废弃引擎名 p 直接拒绝。"""
    resp = create(env, {'prompt': 'x', 'engine': 'p'})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'engine 只能是 acp / remote'
    resp = create(env, {'prompt': 'x', 'engine': 'zzz'})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'engine 只能是 acp / remote'


def test_list_counts_running_and_queued(env):
    for i in range(3):
        create(env, {'prompt': 'task-%d' % i})
    with env.repo.lock:
        first = env.repo.get(None) if False else None
    resp = env.client.get('/api/tasks', headers=H).get_json()
    assert resp['queued'] == 3
    assert resp['running'] == 0
    assert 'countScope' in resp   # 统计口径明示


def test_events_since_alias_accepted(env):
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    full = env.client.get('/api/tasks/%s/events' % tid, headers=H).get_json()
    seqs = [e['seq'] for e in full['events']]
    assert seqs
    since = seqs[0]
    r1 = env.client.get('/api/tasks/%s/events?after=%d' % (tid, since),
                        headers=H).get_json()
    r2 = env.client.get('/api/tasks/%s/events?since=%d' % (tid, since),
                        headers=H).get_json()
    assert [e['seq'] for e in r1['events']] == [e['seq'] for e in r2['events']]
    assert since not in [e['seq'] for e in r2['events']]


def test_close_returns_final_status(env):
    """反馈 §4：close 返回的就是终态。"""
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    body = env.client.post('/api/tasks/%s/close' % tid, headers=H).get_json()
    assert body['status'] == 'closed'


def test_summary_workspace_shows_remote_cwd(env, monkeypatch):
    """反馈 §4：remote 任务的 workspace 字段显示远端 cwd。"""
    task = {'id': 'r1', 'status': 'waiting', 'createdAt': 0, 'events': [],
            'engine': 'remote', 'node': 'ai-xmyz', 'remoteCwd': '/mnt/sda1',
            'workspace': r'E:\x\_agent_tasks\local-dir'}
    out = store.repo.summary(task)
    assert out['workspace'] == '/mnt/sda1'


def test_engine_not_overwritten_on_session_start(env):
    """反馈 §4：remote 任务建卡后 engine 保持 remote（曾被强制改成 acp）。"""
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'local',
                        'cwd': '/tmp'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        assert task['engine'] == 'remote'


def _approveSoon(agent, optionId, delay=0.0):
    """模拟审批方：等处理器挂起 perm 后写入决策并唤醒。"""
    import threading
    import time as _time

    def run():
        deadline = _time.time() + 3
        _time.sleep(delay)
        while _time.time() < deadline:
            if agent.get('perm'):
                agent['permDecision']['value'] = optionId
                agent['permEvent'].set()
                return
            _time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


def test_permission_request_carries_call_and_args(env):
    """反馈 §3.4：审批事件必须能对回具体工具调用。"""
    from modules.tasks_runner import _handleAcpPermission
    with store.repo.lock:
        task = {'id': 'p1', 'seq': 0, 'events': [], 'status': 'running',
                'createdAt': 0}
        store.repo.put(task)
    agent = {'perm': None, 'permDecision': None,
             'permEvent': __import__('threading').Event()}
    _approveSoon(agent, 'opt-allow')
    params = {'toolCall': {'toolCallId': 'call-42', 'title': 'Bash',
                           'rawInput': {'command': 'pwd'}},
              'options': [{'optionId': 'opt-allow', 'name': '允许',
                           'kind': 'allow_once'}]}
    value = _handleAcpPermission(task, agent, params, 'req-1')
    assert value == 'opt-allow'
    ev = [e for e in task['events'] if e['type'] == 'permission_request'][0]
    assert ev['callId'] == 'call-42'
    assert ev['args'] == {'command': 'pwd'}   # R4-1：args 升级为对象（rawInput 原样）
    assert ev['name'] == 'Bash'


def test_permission_falls_back_to_last_tool_call(env):
    """ACP permission 不带 toolCallId 时，退回最近一次 tool_call。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    with store.repo.lock:
        task = {'id': 'p2', 'seq': 0, 'events': [], 'status': 'running',
                'createdAt': 0}
        store.repo.put(task)
    _handleAcpUpdate(task, {'sessionUpdate': 'tool_call',
                            'toolCallId': 'call-7', 'title': 'Bash',
                            'rawInput': 'ls'})
    agent = {'perm': None, 'permDecision': None,
             'permEvent': __import__('threading').Event()}
    _approveSoon(agent, 'opt-a')
    params = {'toolCall': {'title': 'Bash'},
              'options': [{'optionId': 'opt-a', 'name': '允许',
                           'kind': 'allow_once'}]}
    value = _handleAcpPermission(task, agent, params, 'req-2')
    assert value == 'opt-a'
    ev = [e for e in task['events'] if e['type'] == 'permission_request'][0]
    assert ev['callId'] == 'call-7'
    assert ev['args'] == 'ls'


def test_api_404_is_json(env):
    """反馈 §3.8：/api/* 的 404/401 都是 JSON 而非 Flask HTML。

    未知 /api 路径由认证守卫 401 JSON（不暴露端点存在性）；
    已知路由下的资源缺失才是 404 JSON。
    """
    resp = env.client.get('/api/no-such-endpoint', headers=H)
    assert resp.status_code == 401
    assert resp.is_json and 'error' in resp.get_json()
    resp = env.client.get('/api/tasks/ghost-id-xyz', headers=H)
    assert resp.status_code == 404
    assert resp.is_json
    assert 'error' in resp.get_json()


def test_api_405_is_json(env):
    resp = env.client.get('/api/tasks', headers=H)   # GET 支持的列表；用 DELETE 打 405
    assert resp.status_code == 200
    resp = env.client.delete('/api/tasks', headers=H)
    assert resp.status_code == 405
    assert resp.is_json
    assert resp.get_json()['code'] == 405


# ---- §7 派活选模型 ----

def test_model_stored_and_reported(env, monkeypatch):
    """model 合法性依赖执行点声明：声明为空时可任选并如实落卡。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'nodeModelDecl',
                        lambda name='': {'name': 'local', 'models': [],
                                         'default': ''})
    resp = create(env, {'prompt': 'x', 'model': 'kimi-k2-turbo'})
    assert resp.status_code == 202
    task = resp.get_json()['task']
    assert task['model'] == 'kimi-k2-turbo'
    tid = task['id']
    assert env.client.get('/api/tasks/%s' % tid, headers=H).get_json()[
        'task']['model'] == 'kimi-k2-turbo'


def test_model_rejected_when_not_in_whitelist(env, monkeypatch):
    """§7.1：节点声明了白名单时，不在列表内的模型 400 + 可选列表。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'nodeModelDecl', lambda name='': {
        'name': name or 'local', 'models': ['m-fast', 'm-strong'],
        'default': 'm-fast'})
    resp = create(env, {'prompt': 'x', 'model': 'm-nope'})
    body = resp.get_json()
    assert resp.status_code == 400
    assert body['models'] == ['m-fast', 'm-strong']
    assert body['defaultModel'] == 'm-fast'
    resp = create(env, {'prompt': 'x', 'model': 'm-strong'})
    assert resp.status_code == 202


def test_model_bad_format_rejected(env):
    assert create(env, {'prompt': 'x', 'model': 'x' * 101}).status_code == 400


def test_nodes_list_includes_model_info(env, monkeypatch):
    """§7.2：nodes 里每个执行点带 models/defaultModel/modelSelectable。"""
    import modules.tasks_federation as fedMod
    monkeypatch.setattr(fedMod.config, 'getAgentNodes', lambda: [
        {'name': 'ai-xmyz', 'via': 'h1', 'models': ['k2'], 'model': 'k2'}])
    monkeypatch.setattr(fedMod.config, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(fedMod.config, 'getRemotes', lambda: [
        {'name': 'win4710', 'models': [], 'model': ''}])
    resp = env.client.get('/api/tasks/nodes', headers=H).get_json()
    byName = {n['name']: n for n in resp['nodes']}
    assert byName['ai-xmyz']['models'] == ['k2']
    assert byName['ai-xmyz']['modelSelectable'] is True
    assert byName['win4710']['modelSelectable'] is False   # §7.5 固定模型要能看出来


def test_kimi_acp_launch_with_model(monkeypatch, tmp_path):
    """§7.3：model 传到 kimi CLI 的 --model 参数。"""
    import modules.kimi_acp as kacp
    captured = {}

    def fakePopen(cmd, **kw):
        captured['cmd'] = cmd
        raise RuntimeError('stop-here')   # 不真的启动
    monkeypatch.setattr(kacp.subprocess, 'Popen', fakePopen)
    monkeypatch.setattr(kacp.shutil if hasattr(kacp, 'shutil') else kacp,
                        'which', lambda name: 'kimi.exe', raising=False)
    client = kacp.KimiAcp(cwd=str(tmp_path), model='m-fast')
    import shutil as _shutil
    origWhich = _shutil.which
    monkeypatch.setattr(_shutil, 'which', lambda name: 'kimi.exe')
    try:
        client.start()
    except RuntimeError as e:
        assert 'stop-here' in str(e)
    assert captured['cmd'] == ['kimi.exe', '--model', 'm-fast', 'acp']


def test_federation_start_forwards_model(monkeypatch, tmp_path):
    """§7.4：联邦转发把 model 带给对端 /api/tasks。"""
    import modules.tasks_federation as fedMod
    client = fedMod.FederationTaskClient(cwd=str(tmp_path), member='b',
                                         first_prompt='hi', model='m-strong')
    calls = {}

    def fakeCall(method, path, payload=None, timeout=20):
        calls.setdefault(method, payload)
        if method == 'GET':   # 镜像轮询立刻落定，别让它空转
            return 200, {'status': 'waiting', 'events': []}
        return 202, {'task': {'id': 'bid1'}}
    monkeypatch.setattr(client, '_call', fakeCall)
    assert client.start() == 'bid1'
    assert calls['POST']['model'] == 'm-strong'
    client.close()


def test_remote_client_sends_model(monkeypatch):
    """node 链路：model 随 /api/sessions 下发。"""
    import modules.agent_nodes as nodesMod
    client = nodesMod.RemoteAcpClient(cwd='/tmp', node='ai-xmyz', model='m-strong')
    sent = {}

    def fakeRequest(node, method, path, payload=None, timeout=30):
        sent['payload'] = payload
        return 200, {'sid': 's1', 'kimiSessionId': 'k1'}
    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeRequest)
    monkeypatch.setattr(nodesMod, 'findNode', lambda name: {'name': name, 'url': 'x'})
    monkeypatch.setattr(nodesMod, 'tunnelPort', lambda node: 0, raising=False)
    import threading as _th
    class _Resp:
        def __iter__(self):
            return iter([])
        def close(self):
            pass
    monkeypatch.setattr(client, '_openStream', lambda node, since, timeout=45: _Resp())
    client.start()
    assert sent['payload']['model'] == 'm-strong'


def test_discover_models_from_kimi_config(tmp_path, monkeypatch):
    """§7.2：从 kimi config.toml 自动枚举模型（web 与 node 两侧一致）。"""
    cfg = tmp_path / 'config.toml'
    cfg.write_text(
        'default_model = "zhipuai/glm-x"\n'
        '[models."coderelay/gpt-5.6-sol"]\nmodel = "gpt-5.6-sol"\n'
        '[models."coderelay-ds/deepseek-v4"]\nmodel = "deepseek-v4"\n',
        encoding='utf-8')
    import agent_node.acp_session as acpSessionMod
    result = acpSessionMod.discoverModels(str(cfg))
    assert result['default'] == 'zhipuai/glm-x'
    assert result['models'] == ['coderelay-ds/deepseek-v4', 'coderelay/gpt-5.6-sol']
    # 缺文件 → 空声明
    assert acpSessionMod.discoverModels(str(tmp_path / 'nope.toml')) == {
        'models': [], 'default': ''}


def test_local_kimi_models_cached(tmp_path, monkeypatch):
    """web 侧 stdio 回退也用本机 config.toml，60s 缓存。"""
    import modules.agent_nodes as nodesMod
    cfgDir = tmp_path / '.kimi-code'
    cfgDir.mkdir()
    cfg = cfgDir / 'config.toml'
    cfg.write_text('default_model = "m-d"\n[models."m-a"]\nmodel = "a"\n',
                   encoding='utf-8')
    monkeypatch.setattr(nodesMod, '_modelsCache', {'at': 0.0, 'data': {
        'models': [], 'default': ''}})
    import os as _os
    monkeypatch.setattr(_os.path, 'expanduser', lambda p: str(tmp_path))
    first = nodesMod.localKimiModels(maxAgeSec=60)
    assert first['models'] == ['m-a'] and first['default'] == 'm-d'
    # 缓存生效：改文件不重读
    cfg.write_text('default_model = "z"\n', encoding='utf-8')
    assert nodesMod.localKimiModels(maxAgeSec=60)['default'] == 'm-d'
    nodesMod._modelsCache['at'] = 0.0
    assert nodesMod.localKimiModels(maxAgeSec=60)['default'] == 'z'


# ---- R4-9：并发上限可配置 + 热加载 ----

def _useCfg(tmp_path, monkeypatch):
    """把 config 指到临时文件；返回可反复改写内容的路径对象。"""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'port': 6788}), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
    configMod._cache.update(mtime=None, data=None)
    return path


def test_task_max_running_hot_reload_and_429(env, tmp_path, monkeypatch):
    import time as _time
    cfg = _useCfg(tmp_path, monkeypatch)
    try:
        # 缺省 8 并回显
        assert env.client.get('/api/tasks', headers=H).get_json()['limit'] == 8
        # 热改成 1：不重启即生效；第 1 个占坑，第 2 个 429 带 holders
        cfg.write_text(json.dumps({'taskMaxRunning': 1}), encoding='utf-8')
        _time.sleep(0.05)
        assert env.client.get('/api/tasks', headers=H).get_json()['limit'] == 1
        first = create(env, {'prompt': '占用者-标题'}).get_json()['task']
        assert first['owner'] == '测试智能体'   # 令牌通道建的任务 owner=令牌名
        from modules import task_state
        with env.repo.lock:   # _startTask 在夹具里被打桩：手动推进到 running
            task_state.transition(env.repo.get(first['id']),
                                  task_state.TaskState.RUNNING)
        over = create(env, {'prompt': '第二个'})
        assert over.status_code == 429
        body = over.get_json()
        assert body['limit'] == 1 and body['running'] == 1
        assert body['holders'] == [{'id': first['id'], 'owner': '测试智能体',
                                    'title': '占用者-标题'}]
        # 热调回 2：第 2 个即可建（无需重启）
        cfg.write_text(json.dumps({'taskMaxRunning': 2}), encoding='utf-8')
        _time.sleep(0.05)
        assert create(env, {'prompt': '第二个'}).status_code == 202
        # 0 = 不限
        cfg.write_text(json.dumps({'taskMaxRunning': 0}), encoding='utf-8')
        _time.sleep(0.05)
        assert create(env, {'prompt': '第三个'}).status_code == 202
        assert create(env, {'prompt': '第四个'}).status_code == 202
        assert env.client.get('/api/tasks', headers=H).get_json()['limit'] == 0
    finally:
        configMod._cache.update(mtime=None, data=None)


def test_get_task_max_running_bad_values_fall_back(tmp_path, monkeypatch):
    path = tmp_path / 'c.json'
    path.write_text(json.dumps({'port': 6788}), encoding='utf-8')
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
    configMod._cache.update(mtime=None, data=None)
    for bad in ('abc', -3, {'x': 1}):
        path.write_text(json.dumps({'taskMaxRunning': bad}), encoding='utf-8')
        configMod._cache.update(mtime=None, data=None)
        assert configMod.getTaskMaxRunning() == 8, bad
    path.write_text(json.dumps({'taskMaxRunning': 0}), encoding='utf-8')
    configMod._cache.update(mtime=None, data=None)
    assert configMod.getTaskMaxRunning() == 0          # 0=不限，合法
    path.write_text(json.dumps({'taskMaxRunning': '3'}), encoding='utf-8')
    configMod._cache.update(mtime=None, data=None)
    assert configMod.getTaskMaxRunning() == 3          # 数字字符串接受


# ---- 第二轮反馈：close 立即回收 / 接手 Kimi 会话 / 节点模型自动上报 ----

def test_close_triggers_client_shutdown(env):
    """用户 close 后 node 侧进程立即回收（不再等 45 分钟空闲回收）。"""
    import threading
    import time as _time
    from modules import tasks_runner as runner
    calls = []

    class FakeClient:
        def __init__(self, cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0, model=''):
            pass

        def start(self, session_id=None):
            return 'sess-x1'

        def prompt(self, text, timeout=None):
            return {'stopReason': 'end_turn'}

        def set_mode(self, mode):
            pass

        def cancel(self):
            pass

        def close(self):
            calls.append('close')

        def shutdown(self):
            calls.append('shutdown')

    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
    threading.Thread(target=runner._sessionThread, args=(task, []),
                     kwargs={'clientFactory': FakeClient},
                     daemon=True).start()
    for _ in range(60):
        with env.repo.lock:
            if task.get('status') == 'waiting':
                break
        _time.sleep(0.1)
    resp = env.client.post('/api/tasks/%s/close' % tid, headers=H)
    assert resp.status_code == 200
    assert calls == ['close', 'shutdown']


def _sid(n='a'):
    return 'session_' + n * 8 + '-' + n * 4 + '-' + n * 4 + '-' + n * 4 + '-' + n * 12


def test_task_adopts_kimi_session_with_fork(env, monkeypatch):
    """§2：kimiSessionId 默认先 fork，新副本作为任务的起始会话。"""
    newId = _sid('b')
    calls = {}

    def fakeFork(sid, timeout=90):
        calls['forked'] = sid
        return newId
    monkeypatch.setattr(tasks_api.kimi_acp, 'forkSession', fakeFork)
    resp = create(env, {'prompt': '接着上个会话修 bug', 'kimiSessionId': _sid('a')})
    assert resp.status_code == 202
    tid = resp.get_json()['task']['id']
    assert calls['forked'] == _sid('a')
    with env.repo.lock:
        task = env.repo.get(tid)
        assert task['sessionId'] == newId
        assert task['kimiSessionId'] == _sid('a')   # 第三轮：原会话保留
        assert task['kimiSessionFork'] is True


def test_task_adoption_provenance_in_summary(env, monkeypatch):
    """第三轮反馈 §1：详情/列表能看出接手来源（原会话/副本/fork 与否）。"""
    newId = _sid('b')
    monkeypatch.setattr(tasks_api.kimi_acp, 'forkSession',
                        lambda sid, timeout=90: newId)
    resp = create(env, {'prompt': 'x', 'kimiSessionId': _sid('a')})
    tid = resp.get_json()['task']['id']
    detail = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()['task']
    assert detail['kimiSessionId'] == _sid('a')
    assert detail['sessionId'] == newId
    assert detail['kimiSessionFork'] is True
    lst = env.client.get('/api/tasks', headers=H).get_json()['tasks']
    mine = [t for t in lst if t['id'] == tid][0]
    assert mine['kimiSessionId'] == _sid('a')
    assert mine['kimiSessionFork'] is True


def test_task_adoption_takeover_fields(env):
    """fork:false 直接接管：sessionId 即原会话，fork 标记为 False。"""
    orig = _sid('e')
    resp = create(env, {'prompt': 'x', 'kimiSessionId': orig, 'fork': False})
    assert resp.status_code == 202
    detail = env.client.get('/api/tasks/%s' % resp.get_json()['task']['id'],
                            headers=H).get_json()['task']
    assert detail['sessionId'] == orig
    assert detail['kimiSessionId'] == orig
    assert detail['kimiSessionFork'] is False


def test_model_effective_recorded(env, monkeypatch):
    """第三轮反馈 §2：model=用户传的，modelEffective=实际生效（含默认回填）。"""
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'nodeModelDecl',
                        lambda name='': {'name': 'local', 'models': ['m1'],
                                         'default': 'm1'})
    resp = create(env, {'prompt': 'x'})
    t = resp.get_json()['task']
    assert t['model'] == '' and t['modelEffective'] == 'm1'
    resp = create(env, {'prompt': 'x', 'model': 'm1'})
    t = resp.get_json()['task']
    assert t['model'] == 'm1' and t['modelEffective'] == 'm1'


def test_federation_member_models_discovered(monkeypatch):
    """第三轮反馈 §3：成员模型经其 nodes 接口自动发现（不再只看声明）。"""
    import modules.agent_nodes as nodesMod
    import modules.tasks_federation as fedMod
    monkeypatch.setattr(nodesMod.config, 'getAgentNodes', lambda: [])
    monkeypatch.setattr(nodesMod.config, 'getLocalAgentNode', lambda: None)
    monkeypatch.setattr(nodesMod.config, 'getRemotes', lambda: [
        {'id': 'win4710', 'name': 'win4710', 'base': 'http://10.126.126.3:6789',
         'token': 't', 'terminal': True, 'apps': [], 'models': [], 'model': ''}])

    def fakeMemberCall(member, method, path, payload=None, timeout=20):
        assert method == 'GET' and path == '/api/tasks/nodes'
        return 200, {'nodes': [{'name': 'local', 'models': ['m-b'],
                                'defaultModel': 'm-b', 'modelSelectable': True}]}
    monkeypatch.setattr(fedMod, '_memberCall', fakeMemberCall)
    nodesMod._refreshHealthModels()
    cached = nodesMod._healthModelsCached(maxAgeSec=9999)
    assert cached['win4710']['models'] == ['m-b']
    models, default = nodesMod.nodeModelsFor('win4710')
    assert models == ['m-b'] and default == 'm-b'


def test_task_adopt_fork_failure_is_400_with_reason(env, monkeypatch):
    """kimi fork 失败（会话被占用/不存在）→ 400 透传原始原因。"""
    def failingFork(sid, timeout=90):
        raise RuntimeError('This session is in use by another Kimi Code CLI instance.')
    monkeypatch.setattr(tasks_api.kimi_acp, 'forkSession', failingFork)
    resp = create(env, {'prompt': 'x', 'kimiSessionId': _sid('a')})
    assert resp.status_code == 400
    assert 'in use' in resp.get_json()['error']


def test_task_adopt_bad_session_id_rejected(env, monkeypatch):
    import modules.agent_nodes as nodesMod
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name} if name == 'local' else None)
    monkeypatch.setattr(nodesMod, 'findFederationMember', lambda name: None)
    resp = create(env, {'prompt': 'x', 'kimiSessionId': 'rm -rf /'})
    assert resp.status_code == 400
    resp = create(env, {'prompt': 'x', 'engine': 'remote', 'node': 'local',
                        'cwd': '/tmp', 'kimiSessionId': _sid('a')})
    assert resp.status_code == 400
    assert 'acp' in resp.get_json()['error']


def test_task_adopt_takeover_conflict_is_409(env):
    """fork:false 直接接管已被别的任务持有的会话 → 409。"""
    with env.repo.lock:
        env.repo.put({'id': 'holder', 'status': 'waiting', 'sessionId': _sid('c'),
                      'createdAt': 0, 'events': []})
    resp = create(env, {'prompt': 'x', 'kimiSessionId': _sid('c'), 'fork': False})
    assert resp.status_code == 409


def test_kimi_sessions_list_endpoint(env, monkeypatch):
    """§2.3：列表接口返回可接手候选。"""
    items = [{'id': _sid('e'), 'title': 'T', 'workDir': 'E:\\x',
              'updatedAt': 5, 'lastPrompt': 'p'}]
    monkeypatch.setattr(tasks_api.kimi_acp, 'listSessions',
                        lambda limit=30, cwd=None: items)
    resp = env.client.get('/api/tasks/kimi-sessions', headers=H).get_json()
    assert resp['sessions'] == items
    assert 'kimiSessionId' in resp['hint']


def test_list_sessions_filters_cwd_and_sorts(monkeypatch, tmp_path):
    """listSessions：cwd 前缀过滤 + 最近更新排序 + limit。"""
    import modules.kimi_acp as kacp
    payload = [
        {'id': 'session_1', 'title': 'a', 'workDir': 'E:\\事项\\远程',
         'updatedAt': 100, 'lastPrompt': 'x'},
        {'id': 'session_2', 'title': 'b', 'workDir': 'D:\\教研',
         'updatedAt': 200, 'lastPrompt': 'y'},
        {'id': 'session_3', 'title': 'c', 'workDir': 'e:\\事项\\远程\\deskport',
         'updatedAt': 50, 'lastPrompt': 'z'},
    ]

    class P:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ''

    monkeypatch.setattr(kacp.subprocess, 'run', lambda *a, **k: P())
    out = kacp.listSessions(cwd='e:/事项/远程')
    assert [i['id'] for i in out] == ['session_1', 'session_3']
    out = kacp.listSessions(limit=1)
    assert [i['id'] for i in out] == ['session_2']   # 按 updatedAt 降序


def test_node_models_from_health_cache(monkeypatch):
    """§6：远端 node 未声明模型时，/health 自动上报的清单生效。"""
    import modules.agent_nodes as nodesMod
    fresh = {'at': nodesMod.time.monotonic(),
             'data': {'ai-xmyz': {'models': ['m1', 'm2'], 'default': 'm1'}}}
    monkeypatch.setattr(nodesMod, '_healthModels', fresh)
    models, default = nodesMod.nodeModelsFor(
        'ai-xmyz', {'name': 'ai-xmyz', 'models': [], 'model': ''})
    assert models == ['m1', 'm2'] and default == 'm1'


# ---- R4-0 任务接口默认响应瘦身 ----

def test_get_task_omits_events_by_default(env):
    """默认不带 events（几百字节级响应）；?events=1 保留旧行为。"""
    tid = create(env, {'prompt': 'x'}).get_json()['task']['id']
    body = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()
    assert 'events' not in body['task']
    assert 'sources' in body['task']
    body = env.client.get('/api/tasks/%s?events=1' % tid, headers=H).get_json()
    assert isinstance(body['task']['events'], list) and body['task']['events']


def test_list_brief_fields_and_last_text_truncation(env):
    """列表只给白名单字段；lastText 截 200 字并带 lastTextTruncated。"""
    tid = create(env, {'prompt': 'x'}).get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        task['lastText'] = 'word' * 100   # 400 字
        env.repo.save()
    body = env.client.get('/api/tasks', headers=H).get_json()
    item = [t for t in body['tasks'] if t['id'] == tid][0]
    assert item['lastText'] == 'word' * 50
    assert item['lastTextTruncated'] is True
    for key in ('id', 'title', 'status', 'engine', 'node', 'model',
                'modelEffective', 'kimiSessionId', 'createdAt', 'endedAt',
                'error', 'lastText', 'lastTextTruncated'):
        assert key in item, key
    for gone in ('prompt', 'resultFiles', 'events', 'createdLabel',
                 'lastActiveLabel'):
        assert gone not in item, gone
    full = env.client.get('/api/tasks?full=1', headers=H).get_json()
    itemFull = [t for t in full['tasks'] if t['id'] == tid][0]
    assert itemFull['prompt'] == 'x' and 'resultFiles' in itemFull
    assert 'lastTextTruncated' not in itemFull   # 完整摘要不截
    assert itemFull['createdLabel']              # 面板 task-row 依赖这三个字段
    assert isinstance(itemFull['resultFiles'], list)


def test_events_types_filter_and_max_text(env):
    """events 端点：types 过滤 + maxText 截断（truncated/textLen）；缺省不变。"""
    tid = create(env, {'prompt': 'x'}).get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        store.repo.append_event(task, 'assistant', text='long' * 100)   # 400 字
        store.repo.append_event(task, 'error', text='oops')
        store.repo.append_event(task, 'tool', name='Bash', args='ls')
    r = env.client.get('/api/tasks/%s/events?types=assistant,error' % tid,
                       headers=H).get_json()
    assert {e['type'] for e in r['events']} == {'assistant', 'error'}
    r = env.client.get('/api/tasks/%s/events?types=assistant&maxText=10' % tid,
                       headers=H).get_json()
    ev = r['events'][0]
    assert ev['text'] == 'long' * 2 + 'lo'
    assert ev['truncated'] is True and ev['textLen'] == 400
    r = env.client.get('/api/tasks/%s/events' % tid, headers=H).get_json()
    ev = [e for e in r['events'] if e['type'] == 'assistant'][0]
    assert 'truncated' not in ev and len(ev['text']) == 400   # 缺省行为不变
    assert env.client.get('/api/tasks/%s/events?maxText=-1' % tid,
                          headers=H).status_code == 400


def test_federation_resume_explicitly_requests_events(monkeypatch, tmp_path):
    """R4-0 调用方适配：联邦 resume 显式 ?events=1，增量逻辑不变。"""
    import modules.tasks_federation as fedMod
    client = fedMod.FederationTaskClient(cwd=str(tmp_path), member='b',
                                         first_prompt='hi')
    calls = {}

    def fakeCall(method, path, payload=None, timeout=20):
        calls.setdefault(method, path)
        if method == 'GET':
            return 200, {'status': 'waiting', 'events': []}
        return 202, {'task': {'id': 'bid9'}}
    monkeypatch.setattr(client, '_call', fakeCall)
    assert client.start(session_id='bid9') == 'bid9'
    assert calls['GET'] == '/api/tasks/bid9?events=1'
    client.close()


# ---- R4-1/R4-2 审批边界 ----

POLICY = {'allowCommands': [r'^echo\b'], 'denyCommands': ['6789'],
          'default': 'ask'}


def _adoptEnv(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
    agent = {'perm': None, 'permDecision': None,
             'permEvent': __import__('threading').Event()}
    return task, agent


def test_policy_auto_allows_and_records_event(env):
    """allowCommands 命中的调用自动放行：auto_approved 落卡、无挂起事件。"""
    from modules.tasks_runner import _handleAcpPermission
    tid = create(env, {'prompt': 'x', 'policy': POLICY}).get_json()['task']['id']
    task, agent = _adoptEnv(env, tid)
    params = {'toolCall': {'toolCallId': 'c1', 'title': 'Bash', 'kind': 'execute',
                           'rawInput': {'command': 'echo hi'}},
              'options': [{'optionId': 'opt-allow', 'name': '允许',
                           'kind': 'allow_once'}]}
    assert _handleAcpPermission(task, agent, params, 'req-a') == 'opt-allow'
    types = [e['type'] for e in task['events']]
    assert 'auto_approved' in types and 'permission_request' not in types
    approved = [e for e in task['events'] if e['type'] == 'auto_approved'][0]
    assert approved['callId'] == 'c1' and approved['rule'] == 'allowCommands'


def test_policy_deny_hit_holds_with_args_object(env):
    """deny 命中 → ask：挂起事件带 callId 与原始命令对象；超时拒绝。"""
    from modules.tasks_runner import _handleAcpPermission
    tid = create(env, {'prompt': 'x', 'policy': dict(POLICY, timeoutSec=1)}
                 ).get_json()['task']['id']
    task, agent = _adoptEnv(env, tid)
    cmd = 'curl http://127.0.0.1:6789/health'
    params = {'toolCall': {'toolCallId': 'c2', 'title': 'Bash', 'kind': 'execute',
                           'rawInput': {'command': cmd}},
              'options': [{'optionId': 'opt-allow', 'name': '允许',
                           'kind': 'allow_once'}]}
    assert _handleAcpPermission(task, agent, params, 'req-b') is None
    ask = [e for e in task['events'] if e['type'] == 'permission_request'][0]
    assert ask['callId'] == 'c2' and ask['args'] == {'command': cmd}
    assert ask['name'] == 'Bash'
    assert [e for e in task['events']
            if e['type'] == 'permission_result'][-1]['status'] == 'timeout'


def test_policy_ask_waits_for_decision_no_timeout_when_zero(env):
    """timeoutSec=0 = 不超时：晚到的决策（1.2s）仍然生效。"""
    import threading
    import time as _time
    from modules.tasks_runner import _handleAcpPermission
    tid = create(env, {'prompt': 'x',
                       'policy': {'default': 'ask', 'timeoutSec': 0}}
                 ).get_json()['task']['id']
    task, agent = _adoptEnv(env, tid)
    params = {'toolCall': {'toolCallId': 'c3', 'title': 'Edit', 'kind': 'edit',
                           'rawInput': {'file_path': 'E:/any/x.py'}},
              'options': [{'optionId': 'opt-allow', 'name': '允许',
                           'kind': 'allow_once'}]}

    def lateApprove():
        _time.sleep(1.2)
        if agent.get('perm'):
            agent['permDecision']['value'] = 'opt-allow'
            agent['permEvent'].set()
    threading.Thread(target=lateApprove, daemon=True).start()
    assert _handleAcpPermission(task, agent, params, 'req-c') == 'opt-allow'
    assert 'permission_request' in [e['type'] for e in task['events']]


def test_no_policy_task_behaves_exactly_as_before(env):
    """无 policy：与旧行为完全一致（挂起 → 决策 → approved，无自动事件）。"""
    from modules.tasks_runner import _handleAcpPermission
    tid = create(env, {'prompt': 'x'}).get_json()['task']['id']
    task, agent = _adoptEnv(env, tid)
    _approveSoon(agent, 'opt-a')
    params = {'toolCall': {'toolCallId': 'c4', 'title': 'Bash',
                           'rawInput': {'command': 'anything'}},
              'options': [{'optionId': 'opt-a', 'name': '允许',
                           'kind': 'allow_once'}]}
    assert _handleAcpPermission(task, agent, params, 'req-d') == 'opt-a'
    types = [e['type'] for e in task['events']]
    assert 'auto_approved' not in types and 'auto_denied' not in types


def test_create_and_update_policy_route(env):
    """create 校验非法正则 400；详情回显 policy；/policy 更新并落事件。"""
    resp = create(env, {'prompt': 'x', 'policy': {'allowCommands': ['(bad']}})
    assert resp.status_code == 400
    assert 'allowCommands' in resp.get_json()['error']

    tid = create(env, {'prompt': 'x',
                       'policy': {'allowPaths': ['E:\w'], 'default': 'ask'}}
                ).get_json()['task']['id']
    detail = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()['task']
    assert detail['policy']['default'] == 'ask'
    assert detail['policy']['allowPaths'] == ['E:\w']

    resp = env.client.post('/api/tasks/%s/policy' % tid, headers=H,
                           data=json.dumps({'policy': {'default': 'allow'}}),
                           content_type='application/json')
    assert resp.status_code == 200
    detail = env.client.get('/api/tasks/%s' % tid, headers=H).get_json()['task']
    assert detail['policy'] == {'allowPaths': [], 'allowCommands': [],
                                'denyCommands': [], 'default': 'allow',
                                'timeoutSec': None}
    resp = env.client.post('/api/tasks/%s/policy' % tid, headers=H,
                           data=json.dumps({'policy': {'default': 'bad'}}),
                           content_type='application/json')
    assert resp.status_code == 400


def test_federation_client_forwards_policy(monkeypatch, tmp_path):
    import modules.tasks_federation as fedMod
    client = fedMod.FederationTaskClient(cwd=str(tmp_path), member='b',
                                         first_prompt='hi',
                                         policy={'default': 'ask'})
    calls = {}

    def fakeCall(method, path, payload=None, timeout=20):
        calls.setdefault(method, []).append((path, payload))
        if path.endswith('/policy'):
            return 200, {'ok': True}
        if method == 'GET':
            return 200, {'status': 'waiting', 'events': []}
        return 202, {'task': {'id': 'bid8'}}
    monkeypatch.setattr(client, '_call', fakeCall)
    assert client.start() == 'bid8'
    assert calls['POST'][0][1]['policy'] == {'default': 'ask'}
    client.set_policy({'default': 'allow'})
    assert calls['POST'][-1][0] == '/api/tasks/bid8/policy'
    client.close()


# ---- R4-3：阻塞等待 + 精简结果 / R4-12 断线续接 ----

def test_wait_returns_summary_when_already_waiting(env):
    """已 waiting：立即返回；finalText 取流水全文（>MAX_TEXT 不截断）。"""
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    full = '长回答' * 2000   # 6000×? '长回答'=3 字×2000=6000；再拼 4000 超 MAX_TEXT
    full = full + '尾缀' * 2000   # 总长 >8000，事件卡里只留前 8000
    with env.repo.lock:
        task = env.repo.get(tid)
        store.repo.append_event(task, 'assistant', text=full[:store.MAX_TEXT],
                                full=full)
        task_state.transition(task, task_state.TaskState.WAITING)
        store.repo.append_event(task, 'status', text='这轮结束')
    body = env.client.get('/api/tasks/%s/wait?after=0' % tid, headers=H).get_json()
    assert body['status'] == 'waiting'
    assert body['finalText'] == full        # 全文（从流水取），未按 8000 截断
    assert body['lastSeq'] == task['seq']
    assert body['stats']['events'] == len(task['events'])
    assert body['error'] == ''


def test_wait_blocks_until_wake_not_busy_timeout(env):
    """阻塞中被状态事件唤醒，不等满 timeout（事件驱动验证）。"""
    import threading as _th
    import time as _time
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)

    def finishSoon():
        _time.sleep(0.8)
        with env.repo.lock:
            task_state.transition(task, task_state.TaskState.WAITING)
            store.repo.append_event(task, 'status', text='这轮结束')
    _th.Thread(target=finishSoon, daemon=True).start()
    t0 = _time.monotonic()
    body = env.client.get('/api/tasks/%s/wait?after=0&timeout=15' % tid,
                          headers=H).get_json()
    elapsed = _time.monotonic() - t0
    assert body['status'] == 'waiting'
    assert elapsed < 10   # timeout=15 没等满 → 条件变量唤醒生效


def test_wait_timeout_maps_running(env):
    resp = create(env, {'prompt': 'x'})   # _startTask 打桩 → 恒 queued
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        seq = env.repo.get(tid)['seq']
    body = env.client.get('/api/tasks/%s/wait?after=0&timeout=1' % tid,
                          headers=H).get_json()
    assert body['status'] == 'running'    # 规格：超时映射 running
    assert body['lastSeq'] == seq


def test_wait_reports_pending_permission_with_args(env):
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        task['pendingPermission'] = 'req-9'
        env.repo.setAgent(tid, {'perm': {'requestId': 'req-9', 'callId': 'c9',
                                         'title': 'Bash',
                                         'args': {'command': 'curl http://x'},
                                         'options': [
                                             {'optionId': 'o1', 'name': '允许',
                                              'kind': 'allow_once'}]}})
    body = env.client.get('/api/tasks/%s/wait?after=0&timeout=3' % tid,
                          headers=H).get_json()
    assert body['status'] == 'permission'
    assert body['pending']['requestId'] == 'req-9'
    assert body['pending']['args'] == {'command': 'curl http://x'}
    assert body['pending']['options'][0]['optionId'] == 'o1'


def test_wait_resume_from_old_seq_no_loss(env):
    """R4-12：只存 lastSeq，从旧 after 续接——统计不重不漏。"""
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        store.repo.append_event(task, 'tool', name='Bash', args='pwd', callId='c1')
        store.repo.append_event(task, 'auto_approved', callId='c1', rule='allowCommands')
        store.repo.append_event(task, 'error', text='一次报错示例')
        seqAfter = 1   # 跳过建卡事件；后续 3 条都计入
        total = task['seq']
    body = env.client.get('/api/tasks/%s/wait?after=%d&timeout=1' % (tid, seqAfter),
                          headers=H).get_json()
    assert body['stats']['events'] == total - seqAfter
    assert body['stats']['errors'] == 1 and body['stats']['autoApproved'] == 1
    assert body['lastSeq'] == total


def test_message_wait_returns_waiting(env):
    """续聊 wait：发完阻塞，消费线程落定后返回 waiting。"""
    import threading as _th
    import time as _time
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        task_state.transition(task, task_state.TaskState.WAITING)
        store.repo.save()
        agent = {'queue': __import__('queue').Queue(), 'perm': None,
                 'permDecision': None,
                 'permEvent': __import__('threading').Event()}
        env.repo.setAgent(tid, agent)

    def consume():
        item = agent['queue'].get(timeout=5)
        _time.sleep(0.4)
        with env.repo.lock:
            task_state.transition(task, task_state.TaskState.RUNNING)
            store.repo.append_event(task, 'user', text=item.get('text') or '')
        _time.sleep(0.3)
        with env.repo.lock:
            task_state.transition(task, task_state.TaskState.WAITING)
            store.repo.append_event(task, 'status', text='这轮结束')
    _th.Thread(target=consume, daemon=True).start()
    t0 = _time.monotonic()
    resp = env.client.post('/api/tasks/%s/message' % tid, headers=H,
                           data=json.dumps({'text': '继续', 'wait': 10}),
                           content_type='application/json')
    elapsed = _time.monotonic() - t0
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'waiting'
    assert elapsed < 9   # 10s 没等满 → 事件唤醒


def test_permission_wait_returns_after_settle(env):
    """审批 wait：决策提交后阻塞，落定返回 waiting。"""
    import threading as _th
    import time as _time
    resp = create(env, {'prompt': 'x'})
    tid = resp.get_json()['task']['id']
    with env.repo.lock:
        task = env.repo.get(tid)
        task_state.transition(task, task_state.TaskState.RUNNING)
        task['pendingPermission'] = 'req-w1'
        agent = {'perm': {'requestId': 'req-w1', 'callId': 'c1', 'title': 'Bash',
                          'args': {'command': 'echo hi'},
                          'options': [{'optionId': 'o1', 'name': '允许',
                                       'kind': 'allow_once'}]},
                 'permDecision': {'value': None},
                 'permEvent': __import__('threading').Event(),
                 'queue': __import__('queue').Queue()}
        env.repo.setAgent(tid, agent)

    def settle():
        _time.sleep(0.4)
        with env.repo.lock:
            task['pendingPermission'] = ''
            task_state.transition(task, task_state.TaskState.WAITING)
            store.repo.append_event(task, 'status', text='这轮结束')
    _th.Thread(target=settle, daemon=True).start()
    t0 = _time.monotonic()
    resp = env.client.post('/api/tasks/%s/permission' % tid, headers=H,
                           data=json.dumps({'requestId': 'req-w1',
                                            'optionId': 'o1', 'wait': 10}),
                           content_type='application/json')
    elapsed = _time.monotonic() - t0
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'waiting'
    assert elapsed < 9


def test_create_wait_maps_running_when_queued(env):
    """create 带 wait：受理后阻塞；夹具里无人执行 → 超时映射 running。"""
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x', 'wait': 1}),
                           content_type='application/json')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'running'
    assert body['lastSeq'] >= 1
