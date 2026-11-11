# -*- coding: utf-8 -*-
"""R5-2 敏感值不进事件流：注册表收集/热刷新、事件与 jsonl 脱敏、
policy 内建 deny（config.json / 令牌文件）。全部用合成令牌，不碰真配置。"""
import hashlib
import json

import pytest

import config as configMod
from modules import sensitive_values as sv
from modules import task_policy
from modules.tasks_store import TaskRepository

TOKEN = 'tok-remotes-' + 'a' * 14          # remotes[].token
TOKEN2 = 'node-' + 'b' * 16                # agentNodes[].token
FED = 'fed-' + 'c' * 16
KEY = 'key-' + 'd' * 16
NODETOK = 'nodetok-' + 'e' * 16
H = {'X-Agent-Token': 'tok-' + 'h' * 24}


@pytest.fixture
def reg(monkeypatch, tmp_path):
    """注册表换成合成配置：真实 config.json 永不参与断言。"""
    cfg = {'remotes': [{'id': 'm1', 'name': '成员A', 'token': TOKEN}],
           'agentNodes': [{'name': 'n1', 'token': TOKEN2}],
           'federationToken': FED, 'password': 'shortpw',
           'decisionApiKey': KEY}
    monkeypatch.setattr(configMod, 'load', lambda: cfg)
    tokfile = tmp_path / 'token'
    tokfile.write_text(NODETOK, encoding='utf-8')
    monkeypatch.setattr(sv, 'TOKEN_FILE', str(tokfile))
    monkeypatch.setattr(sv, '_statKey', lambda: ('test',))
    monkeypatch.setattr(sv, '_cache', {'key': None, 'pairs': ()})
    return cfg


def test_sources_labels_and_min_len(reg):
    labels = sv.sources()
    assert 'remotes.成员A.token' in labels
    assert 'agentNodes.n1.token' in labels
    assert 'federationToken' in labels
    assert 'decisionApiKey' in labels
    assert 'localNode.token' in labels
    assert 'password' not in labels          # <12 字符不纳入


def test_redact_text_and_longest_first(reg):
    text = '调用 %s 和 %s 完成' % (TOKEN, TOKEN2)
    out = sv.redactText(text)
    assert TOKEN not in out and TOKEN2 not in out
    assert '<redacted:remotes.成员A.token>' in out
    assert '<redacted:agentNodes.n1.token>' in out
    # 长值包含短值：先替换长的，不会留下残段
    long_ = TOKEN + '-tail'
    out2 = sv.redactText('x ' + long_ + ' y')
    assert long_ not in out2 and TOKEN not in out2


def test_redact_value_recursive(reg):
    obj = {'args': {'command': 'cat f', 'path': TOKEN},
           'options': [{'optionId': TOKEN2, 'n': 3}], 'num': 7}
    out = sv.redactValue(obj)
    assert TOKEN not in json.dumps(out) and TOKEN2 not in json.dumps(out)
    assert out['num'] == 7 and out['options'][0]['n'] == 3


def test_refresh_follows_config_change(reg, monkeypatch):
    stat = {'k': 'k1'}
    monkeypatch.setattr(sv, '_statKey', lambda: (stat['k'],))
    sv.refresh()
    assert sv.redactText('v=' + TOKEN) == 'v=<redacted:remotes.成员A.token>'
    reg['remotes'][0]['token'] = 'fresh-' + 'z' * 16   # config 热改
    stat['k'] = 'k2'
    sv.refresh()
    assert sv.redactText('v=' + TOKEN) == 'v=' + TOKEN  # 旧令牌已出册：原样保留
    assert 'fresh-' + 'z' * 16 not in sv.redactText('v=fresh-' + 'z' * 16)


# ---------------------------------------------------------------- 事件流

def _repo(tmp_path):
    return TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))


def test_append_event_redacts_tool_result_and_jsonl(reg, tmp_path):
    repo = _repo(tmp_path)
    task = {'id': 't1', 'seq': 0, 'events': [], 'lastText': ''}
    ev = repo.append_event(task, 'tool_result', text='配置: ' + TOKEN,
                           full='全文带令牌: ' + TOKEN, truncated=False,
                           callId='c1', name='Bash')
    assert ev['text'] == '配置: <redacted:remotes.成员A.token>'
    with open(repo.log_path(task), encoding='utf-8') as f:
        raw = f.read()
    assert TOKEN not in raw                       # jsonl 流水同样脱敏
    assert '全文带令牌: <redacted:remotes.成员A.token>' in raw


def test_append_event_redacts_assistant_lastText(reg, tmp_path):
    repo = _repo(tmp_path)
    task = {'id': 't2', 'seq': 0, 'events': [], 'lastText': ''}
    repo.append_event(task, 'assistant', text=TOKEN2, full=TOKEN2)
    assert task['lastText'] == '<redacted:agentNodes.n1.token>'


def test_user_events_stay_verbatim(reg, tmp_path):
    """主控显式写入（user/status）不脱敏——spec 只圈了执行侧四类事件。"""
    repo = _repo(tmp_path)
    task = {'id': 't3', 'seq': 0, 'events': [], 'lastText': ''}
    ev = repo.append_event(task, 'user', text='请用 %s 部署' % TOKEN)
    assert ev['text'] == '请用 %s 部署' % TOKEN


# ---------------------------------------------------------------- policy

def test_builtin_deny_execute_hits_ask(reg):
    policy = {'allowCommands': [r'.*'], 'default': 'allow'}
    assert task_policy.evaluate(policy, kind='execute',
                                args={'command': 'cat config.json'}) \
        == ('ask', 'deny:sensitive-file')
    assert task_policy.evaluate(policy, kind='execute',
                                args={'command': 'cat ~/.agent_node_token'})[0] == 'ask'
    assert task_policy.evaluate(policy, kind='execute',
                                args={'command': r'type C:\u\.deskport-agent-node\token'})[0] == 'ask'
    assert task_policy.evaluate(policy, kind='execute',
                                args={'command': 'cat /home/u/.agent-token'})[0] == 'ask'


def test_builtin_deny_file_tools_before_readonly(reg):
    policy = {'default': 'allow'}
    assert task_policy.evaluate(policy, kind='read', name='Read',
                                args={'path': 'E:\\x\\config.json'}) \
        == ('ask', 'deny:sensitive-file')
    # 只读工具的非敏感路径照常放行
    assert task_policy.evaluate(policy, kind='read', name='Read',
                                args={'path': 'E:\\x\\notes.md'}) \
        == ('allow', 'read-only')


def test_builtin_deny_survives_heredoc_strip(reg):
    """heredoc 正文是数据不参与判定（协作原则 §3）——正文提 config.json 不误伤。"""
    policy = {'allowCommands': [r'^cat\b'], 'default': 'ask'}
    cmd = 'cat <<EOF\nhello config.json world\nEOF'
    assert task_policy.evaluate(policy, kind='execute',
                                args={'command': cmd}) == ('allow', 'allowCommands')


def test_normal_commands_unaffected(reg):
    policy = {'allowCommands': [r'^ls\b'], 'default': 'ask'}
    assert task_policy.evaluate(policy, kind='execute',
                                args={'command': 'ls src'}) \
        == ('allow', 'allowCommands')


# ---------------------------------------------------------------- API 层

@pytest.fixture
def webEnv(tmp_path, monkeypatch, reg):
    from types import SimpleNamespace
    import modules.agent_nodes as nodesMod
    from modules import events as eventsMod
    from modules import tasks_api
    from modules import tasks_store as store
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试',
                                  'tokenHash': hashlib.sha256(
                                      H['X-Agent-Token'].encode()).hexdigest()}])
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    import time as _time
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': _time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo)


def test_events_and_log_api_never_leak_token(webEnv):
    """spec #4 回归：tool_result 含 remotes 令牌 → events/log 全是
    <redacted:…>，原文在响应任何位置都不出现。"""
    resp = webEnv.client.post('/api/tasks', headers=H,
                              data=json.dumps({'prompt': '查配置'}),
                              content_type='application/json')
    assert resp.status_code == 202
    taskId = resp.get_json()['task']['id']
    task = webEnv.repo.get(taskId)
    webEnv.repo.append_event(task, 'tool_result', text='remotes = ' + TOKEN,
                             full='remotes 完整输出 ' + TOKEN, truncated=False,
                             callId='c9', name='Bash')
    detail = webEnv.client.get('/api/tasks/%s?events=1' % taskId,
                               headers=H).get_data(as_text=True)
    assert TOKEN not in detail
    assert '<redacted:remotes.' in detail
    log = webEnv.client.get('/api/tasks/%s/log' % taskId,
                            headers=H).get_data(as_text=True)
    assert TOKEN not in log
    assert '<redacted:remotes.' in log
