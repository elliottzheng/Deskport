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


def test_engine_p_deprecated_alias(env):
    resp = create(env, {'prompt': 'x', 'engine': 'p'})
    assert resp.status_code == 202
    assert resp.get_json()['task']['engine'] == 'acp'
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
    assert 'pwd' in ev['args']


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
        assert env.repo.get(tid)['sessionId'] == newId
        assert env.repo.get(tid)['kimiSessionLoad'] is True


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
