# -*- coding: utf-8 -*-
"""后台任务幽灵审批循环（R5-7）：run_in_background 服务端拒绝 + 轮间孤儿审批。

现象（2026-11-12 实测）：执行方 run_in_background 跑长命令后，本轮立刻结束，
之后不断冒出 args 为空、callId 在事件流里查不到的审批；主控每拒绝一个又来
一个，message 被 409 挡住，任务卡死只能 close 重开。

修复：policy.allowBackground 缺省 false → 带 run_in_background: true 的调用
服务端直接拒绝（auto_denied rule=background，不交给主控）；轮次之间收到、
callId 在本任务 tool 事件里查不到的审批自动拒绝（auto_denied rule=orphan），
不挂起、不挡 message。测试用真实 Kimi 事件形状：permission 请求不带 kind、
不带 rawInput，tool_call 的 rawInput 可能是 JSON 字符串。
"""
import json
import queue
import threading
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

POLICY = {'allowCommands': [r'^echo\b'], 'default': 'ask'}

BACKGROUND_CMD = 'python -m http.server 8123'

# 真实链路：Bash 工具带 run_in_background 的 tool_call update（rawInput 对象
# 或 JSON 字符串两种都出现过）
TOOL_CALL_OBJ = {'sessionUpdate': 'tool_call', 'toolCallId': 'call-bg-1',
                 'title': 'Bash', 'kind': 'execute', 'status': 'in_progress',
                 'rawInput': {'command': BACKGROUND_CMD, 'run_in_background': True}}
TOOL_CALL_JSON = dict(TOOL_CALL_OBJ,
                      rawInput=json.dumps({'command': BACKGROUND_CMD,
                                           'run_in_background': True}))
# 真实链路：request_permission 不带 kind、不带 rawInput（按 callId 回查）
def permParams(callId, title='Bash'):
    return {'sessionId': 'sess_x',
            'toolCall': {'title': title, 'toolCallId': callId},
            'options': [{'optionId': 'opt-allow', 'name': 'Allow once',
                         'kind': 'allow_once'},
                        {'optionId': 'opt-reject', 'name': 'Reject',
                         'kind': 'reject_once'}]}


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


def _adopt(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
    agent = {'perm': None, 'permDecision': None,
             'permEvent': threading.Event()}
    return task, agent


def _approveSoon(agent, optionId):
    """模拟审批方：等处理器挂起 perm 后写入决策并唤醒。"""
    import time as _time

    def run():
        deadline = _time.time() + 3
        while _time.time() < deadline:
            if agent.get('perm'):
                agent['permDecision']['value'] = optionId
                agent['permEvent'].set()
                return
            _time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


# ---- 规则 1：run_in_background 服务端直接拒绝 ----

def test_background_call_auto_denied_not_sent_to_controller(env):
    """run_in_background 的调用：auto_denied(rule=background) 落卡带原因，
    不产生 permission_request、不挂起。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'prompt': 'x', 'policy': dict(POLICY, timeoutSec=1)}
                 ).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    _handleAcpUpdate(task, dict(TOOL_CALL_OBJ))   # 真实形状：先 tool_call update
    assert _handleAcpPermission(task, agent, permParams('call-bg-1'),
                                'req-bg') is None
    denied = [e for e in task['events'] if e['type'] == 'auto_denied']
    assert len(denied) == 1
    assert denied[0]['rule'] == 'background'
    assert denied[0]['callId'] == 'call-bg-1'
    assert denied[0]['text'] == '后台任务被禁用，请在前台运行'
    assert 'permission_request' not in [e['type'] for e in task['events']]
    assert not task.get('pendingPermission')


def test_background_args_json_string_shape_also_denied(env):
    """真实链路 tool_call 的 rawInput 是 JSON 字符串：同样按 background 拒绝。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'prompt': 'x', 'policy': dict(POLICY, timeoutSec=1)}
                 ).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    _handleAcpUpdate(task, dict(TOOL_CALL_JSON))
    assert _handleAcpPermission(task, agent, permParams('call-bg-1'),
                                'req-bg') is None
    denied = [e for e in task['events'] if e['type'] == 'auto_denied']
    assert denied and denied[0]['rule'] == 'background'


def test_allow_background_true_keeps_old_flow(env):
    """allowBackground: true：不触发 background 拒绝，照常走审批（超时拒绝）。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'prompt': 'x', 'policy': {'allowBackground': True,
                                                 'default': 'ask',
                                                 'timeoutSec': 1}}
                 ).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    _handleAcpUpdate(task, dict(TOOL_CALL_OBJ))
    assert _handleAcpPermission(task, agent, permParams('call-bg-1'),
                                'req-bg') is None   # 超时拒绝
    types = [e['type'] for e in task['events']]
    assert 'permission_request' in types
    assert 'auto_denied' not in types


def test_no_policy_background_call_unchanged(env):
    """没有 policy：行为不变——后台调用照样推给主控审批。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'prompt': 'x'}).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    _handleAcpUpdate(task, dict(TOOL_CALL_OBJ))
    _approveSoon(agent, 'opt-allow')
    assert _handleAcpPermission(task, agent, permParams('call-bg-1'),
                                'req-bg') == 'opt-allow'
    types = [e['type'] for e in task['events']]
    assert 'auto_denied' not in types and 'permission_request' in types


# ---- 规则 2：轮间孤儿审批自动拒绝 ----

def test_orphan_permission_between_turns_auto_denied(env):
    """轮次之间收到、callId 查不到的审批：auto_denied(rule=orphan)，
    不产生 permission_request、pendingPermission 不置位、message 不被 409 挡。"""
    from modules.tasks_runner import _handleAcpPermission
    tid = create(env, {'prompt': 'x'}).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    with env.repo.lock:
        task['status'] = 'waiting'   # 上一轮已结束 = 轮次之间
        env.repo.save()
    assert _handleAcpPermission(task, agent, permParams('ghost-1'),
                                'req-ghost') is None
    denied = [e for e in task['events'] if e['type'] == 'auto_denied']
    assert len(denied) == 1 and denied[0]['rule'] == 'orphan'
    assert denied[0]['callId'] == 'ghost-1'
    assert 'permission_request' not in [e['type'] for e in task['events']]
    assert not task.get('pendingPermission')

    # 幽灵审批拒绝后 message 不再被 409（有等待处理的审批）挡住
    live = {'queue': queue.Queue(), 'client': None, 'perm': None,
            'permDecision': None, 'permEvent': threading.Event(), 'ready': True}
    store.repo.setAgent(tid, live)
    resp = env.client.post('/api/tasks/%s/message' % tid, headers=H,
                           data=json.dumps({'text': '继续'}),
                           content_type='application/json')
    assert resp.status_code == 202, resp.get_json()
    assert live['queue'].get_nowait()['text'] == '继续'


def test_orphan_rule_not_applied_mid_turn(env):
    """执行轮次中 callId 查不到：保持旧行为推给主控（R4-3c #1），不误杀。"""
    from modules.tasks_runner import _handleAcpPermission
    tid = create(env, {'prompt': 'x', 'policy': dict(POLICY, timeoutSec=1)}
                 ).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    with env.repo.lock:
        task['status'] = 'running'
        env.repo.save()
    assert _handleAcpPermission(task, agent, permParams('mid-turn-1'),
                                'req-mid') is None   # 1s 超时拒绝
    types = [e['type'] for e in task['events']]
    assert 'permission_request' in types
    assert 'auto_denied' not in types


def test_orphan_rule_not_applied_when_callid_known(env):
    """轮次之间但 callId 在 tool 事件里查得到：不是孤儿，照常走审批流程。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'prompt': 'x', 'policy': dict(POLICY, timeoutSec=1)}
                 ).get_json()['task']['id']
    task, agent = _adopt(env, tid)
    with env.repo.lock:
        task['status'] = 'waiting'
        env.repo.save()
    _handleAcpUpdate(task, {'sessionUpdate': 'tool_call',
                            'toolCallId': 'call-known', 'title': 'Bash',
                            'kind': 'execute', 'status': 'in_progress',
                            'rawInput': {'command': 'echo hi'}})
    # callId 查得到 → 不按 orphan 拒绝：echo hi 命中 allowCommands 自动放行
    assert _handleAcpPermission(task, agent, permParams('call-known'),
                                'req-known') == 'opt-allow'
    types = [e['type'] for e in task['events']]
    assert 'auto_denied' not in types and 'permission_request' not in types


# ---- policy 校验与 evaluate 单元行为 ----

def test_validate_policy_normalizes_allow_background():
    from modules import task_policy
    assert task_policy.validatePolicy(None) == {}
    assert task_policy.validatePolicy({}) == {}   # 空 policy = 无 policy
    p = task_policy.validatePolicy({'allowBackground': True})
    assert p['allowBackground'] is True
    p = task_policy.validatePolicy({'default': 'ask'})
    assert p['allowBackground'] is False   # 缺省 false
    assert task_policy.DEFAULT_POLICY['allowBackground'] is False


def test_evaluate_background_rules():
    from modules import task_policy
    assert task_policy.evaluate(POLICY, 'execute', 'Bash',
                                {'command': 'x', 'run_in_background': True}
                                ) == ('deny', 'background')
    assert task_policy.evaluate(POLICY, 'execute', 'Bash',
                                '{"command": "x", "run_in_background": true}'
                                ) == ('deny', 'background')
    assert task_policy.evaluate(POLICY, 'execute', 'Bash',
                                {'command': 'x', 'run_in_background': False}
                                )[0] == 'ask'
    assert task_policy.evaluate(dict(POLICY, allowBackground=True),
                                'execute', 'Bash',
                                {'command': 'x', 'run_in_background': True}
                                ) == ('ask', 'default')
