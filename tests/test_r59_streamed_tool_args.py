# -*- coding: utf-8 -*-
"""参数后到的流式工具事件（R5-9）：tool 事件与审批里的 args 不再为空。

现象（2026-11-12 实测，22:00 前后复现）：服务端 policy 全部失效，tool 事件
args 恒为空串、permission_request 也是空 args，policy 判不了、一律挂起等人工。
好任务（19:5x）与坏任务（21:0x）跑的是同一份服务端代码与同一个 kimi.exe；
差别在 kimi 会话绑的模型：glm-5.3-flash 在 tool_call 里一次给全 rawInput，
deepseek-v4.1-flash-1017 只给一个空壳 tool_call，参数随后在 tool_call_update
的 content 文本（逐段 JSON 快照）里流式补，rawInput 对象甚至晚于
request_permission 才到。

本文件用真实 ACP 事件形状（见 acp 探针实测）复现：tool_call 无 rawInput →
content 文本逐段 → request_permission 不带 rawInput → rawInput 才到。脱敏
注册表里放真实长度的令牌，确认补参数这条路径也走脱敏。
"""
import json
import queue
import threading
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import sensitive_values as sv
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN_H = 'tok-' + 'h' * 24
TOKEN_HASH = __import__('hashlib').sha256(TOKEN_H.encode()).hexdigest()
H = {'X-Agent-Token': TOKEN_H}

SECRET = 'sk-' + 'q' * 30          # 真实长度的敏感值（36 字符）

CMD = 'echo hi'


def _content(text):
    return [{'type': 'content', 'content': {'type': 'text', 'text': text}}]


# 真实 ACP 形状（kimi 2.5.3 + Alibaba/deepseek-v4.1-flash-1017 实测）
TOOL_CALL = {'sessionUpdate': 'tool_call', 'toolCallId': 'call-1',
             'title': 'Bash', 'kind': 'execute', 'status': 'pending',
             'content': _content('')}
STREAM_1 = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-1',
            'status': 'in_progress', 'content': _content('{')}
STREAM_2 = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-1',
            'status': 'in_progress',
            'content': _content('{"command": "%s"}' % CMD)}
LATE_RAWINPUT = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-1',
                 'title': 'Running: ' + CMD, 'kind': 'execute',
                 'status': 'in_progress', 'rawInput': {'command': CMD},
                 'content': _content('{"command":"%s"}' % CMD)}
DONE = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-1',
        'status': 'completed', 'content': _content('hi\n'),
        'rawOutput': 'hi\n'}


def permParams(callId='call-1', rawInput=None):
    toolCall = {'toolCallId': callId, 'title': 'Bash', 'content': _content('')}
    if rawInput is not None:
        toolCall['rawInput'] = rawInput
    return {'sessionId': 'sess_x', 'toolCall': toolCall,
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
    # 敏感值注册表：真实长度的值，确认补参数路径同样脱敏
    monkeypatch.setattr(sv, 'pairs', lambda: ((SECRET, 'remotes.test.token'),))
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


def create(env, policy):
    return env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x', 'policy': policy}),
                           content_type='application/json'
                           ).get_json()['task']['id']


def adopt(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
    agent = {'perm': None, 'permDecision': None, 'permEvent': threading.Event()}
    return task, agent


def approveSoon(agent, optionId):
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


def toolEvents(task):
    return [e for e in task['events'] if e['type'] == 'tool']


# ---------------------------------------------------------------- 事件流

def test_tool_call_without_rawinput_is_backfilled_from_stream(env):
    """tool_call 无参数 → content 流式文本 → 参数到齐后已落卡的 tool 事件补全。"""
    from modules.tasks_runner import _handleAcpUpdate
    tid = create(env, {'default': 'ask'})
    task, _agent = adopt(env, tid)
    for update in (TOOL_CALL, STREAM_1, STREAM_2, LATE_RAWINPUT, DONE):
        _handleAcpUpdate(task, dict(update))
    tools = toolEvents(task)
    assert len(tools) == 1
    assert json.loads(tools[0]['args']) == {'command': CMD}


def test_completed_output_text_not_mistaken_for_args(env):
    """completed 的 content 是输出文本：不能当参数，也不能污染按 callId 的记录。"""
    from modules.tasks_runner import _handleAcpUpdate
    tid = create(env, {'default': 'ask'})
    task, _agent = adopt(env, tid)
    for update in (TOOL_CALL, STREAM_1, STREAM_2, LATE_RAWINPUT, DONE):
        _handleAcpUpdate(task, dict(update))
    assert task['_toolArgs']['call-1'] == {'command': CMD}
    with open(store.repo.log_path(task), encoding='utf-8') as f:
        assert '{"command": "hi' not in f.read()


# ---------------------------------------------------------------- policy

def test_streamed_args_reach_policy_allow(env):
    """参数只在流式文本里：policy 按 allowCommands 自动放行，不再一律挂起。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'allowCommands': [r'^echo\b'], 'default': 'ask',
                       'timeoutSec': 1})
    task, agent = adopt(env, tid)
    for update in (TOOL_CALL, STREAM_1, STREAM_2):
        _handleAcpUpdate(task, dict(update))
    assert _handleAcpPermission(task, agent, permParams(), 'req-1') == 'opt-allow'
    types = [e['type'] for e in task['events']]
    assert 'auto_approved' in types and 'permission_request' not in types


def test_pending_args_is_object_with_command(env):
    """policy 判不了要挂起时，审批事件与 pending.args 必须是含命令的对象。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'default': 'ask', 'timeoutSec': 5})
    task, agent = adopt(env, tid)
    for update in (TOOL_CALL, STREAM_1, STREAM_2):
        _handleAcpUpdate(task, dict(update))
    approveSoon(agent, 'opt-allow')
    assert _handleAcpPermission(task, agent, permParams(), 'req-2') == 'opt-allow'
    ev = [e for e in task['events'] if e['type'] == 'permission_request'][0]
    assert ev['args'] == {'command': CMD}


def test_permission_rawinput_not_shadowed_by_empty_call_record(env):
    """tool_call 记下的空参数不能盖掉审批自带的 rawInput（旧实现"空也认"）。"""
    from modules.tasks_runner import _handleAcpPermission, _handleAcpUpdate
    tid = create(env, {'allowCommands': [r'^echo\b'], 'default': 'ask',
                       'timeoutSec': 1})
    task, agent = adopt(env, tid)
    _handleAcpUpdate(task, dict(TOOL_CALL))
    assert _handleAcpPermission(
        task, agent, permParams(rawInput={'command': CMD}), 'req-3') == 'opt-allow'
    assert 'permission_request' not in [e['type'] for e in task['events']]


# ---------------------------------------------------------------- 脱敏

def test_streamed_args_redacted_before_event_write(env):
    """补参数走脱敏：真实长度令牌出现在命令里 → 事件里是 <redacted:…>。"""
    from modules.tasks_runner import _handleAcpUpdate
    tid = create(env, {'default': 'ask'})
    task, _agent = adopt(env, tid)
    cmd = 'curl -H "Authorization: Bearer %s" https://x' % SECRET
    for update in (TOOL_CALL,
                   {'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-1',
                    'status': 'in_progress',
                    'content': _content(json.dumps({'command': cmd}))},
                   {'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-1',
                    'status': 'in_progress', 'rawInput': {'command': cmd}}):
        _handleAcpUpdate(task, dict(update))
    args = toolEvents(task)[0]['args']
    assert SECRET not in args
    assert '<redacted:remotes.test.token>' in args
    assert json.loads(args)['command'].count('<redacted:remotes.test.token>') == 1