# -*- coding: utf-8 -*-
"""批次 G1：submit_result 的注入（本机与远端节点两条路径 + 节点侧透传）、
审批自动放行、自动追问与 result_missing。

桩化：KimiAcp/RemoteAcpClient/node 的 kimi 子进程都不真起；env 夹具同
test_leads_inbox_store（config/仓储指 tmp）。hubMcpUrl 的地址推导在本文件
的 config 里显式给 agentMcpUrl（远端执行点没它就不注入）。
"""
import json
import sys
import threading
import time

import pytest
from types import SimpleNamespace

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import leads_inbox, task_permission, task_result, task_result_mcp
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
SUBMIT_TITLE = 'mcp__deskport__submit_result'
IMPL_OK = {'summary': '交付了 X', 'commit': 'abc1234',
           'tests': {'passed': 12, 'failed': 0, 'skipped': 1}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []
    cfgfile = tmp_path / 'config.json'
    # agentMcpUrl 显式给：远端执行点注入要走它（本机执行点走回环，见 core）
    cfgfile.write_text(json.dumps({'password': PASSWORD,
                                   'agentMcpUrl': 'http://127.0.0.1:6788'}),
                       encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def fakeLog(action, target='', result='ok', detail='', ip=''):
        audit.append({'action': action, 'target': target})

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', fakeLog)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path,
                           app=app, monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def newTask(env, expects='impl', node=None, cwd=None):
    body = {'prompt': '干活', 'expects': expects}
    if node:
        body['node'] = node
    if cwd:
        body['cwd'] = cwd
    resp = env.client.post('/api/tasks', data=json.dumps(body),
                           content_type='application/json')
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def userEvents(env, tid):
    with env.repo.lock:
        task = env.repo.get(tid)
        return [e for e in task.get('events') or [] if e.get('type') == 'user']


def stopThread(env, tid, thread, timeout=10):
    """收尾：给会话循环发 close，等线程退出（失败也不挂死测试）。"""
    agent = env.repo.agentOf(tid)
    if agent:
        agent['queue'].put({'type': 'close'})
    thread.join(timeout)


# ---------------------------------------------------------------- 桩客户端

class StubKimi:
    """本机 stdio 路径的桩：记下 runner 注入的 mcp_servers。"""
    instances = []

    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=0):
        self.cwd, self.mcp_servers, self.started = cwd, None, None
        self.stderr_lines = []
        StubKimi.instances.append(self)

    def start(self, session_id=None):
        self.started = session_id
        return 'sess-stub'

    def prompt(self, text, timeout=None):
        return {'stopReason': 'end_turn'}

    def close(self):
        pass


class CloseSpyKimi:
    """轮中挂住的桩：cancel 时记录令牌状态——那一刻会话线程一定还活着
    （prompt 里等着），所以令牌失效只能是关闭路径做的，不是线程收尾。"""
    instances = []

    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=0):
        self.mcp_servers, self.stderr_lines = None, []
        self.entered, self.release = threading.Event(), threading.Event()
        self.tokenAtCancel = 'unset'
        CloseSpyKimi.instances.append(self)

    def start(self, session_id=None):
        return 'sess-spy'

    def prompt(self, text, timeout=None):
        self.entered.set()
        assert self.release.wait(10)
        return {'stopReason': 'end_turn'}

    def cancel(self):
        # closeTaskCore 先吊销令牌再调 cancel：此刻令牌必须已经失效
        token = (self.mcp_servers or [{}])[0].get('headers', [{}])[0].get('value', '')
        self.tokenAtCancel = task_result_mcp.tokenTask(token)
        self.release.set()

    def close(self):
        pass


# ---------------------------------------------------------------- 本机路径

def test_local_path_injects_mcp_servers(env):
    """本机（web 内 stdio）：建卡 expects=impl → session 前注入 http MCP。"""
    login(env.client)
    StubKimi.instances = []
    tid = newTask(env, 'impl')
    with env.repo.lock:
        task = env.repo.get(tid)
    th = threading.Thread(target=runner._sessionThread,
                          args=(task, []), kwargs={'clientFactory': StubKimi})
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if StubKimi.instances and StubKimi.instances[0].mcp_servers is not None:
            break
        time.sleep(0.05)
    client = StubKimi.instances[0]
    injected = client.mcp_servers          # 停线程前抓快照（退出即吊销令牌）
    assert injected is not None, 'runner 没来得及注入（10s 超时）'
    assert len(injected) == 1
    tokenAtInject = injected[0]['headers'][0]['value']
    assert task_result_mcp.tokenTask(tokenAtInject) == tid   # 线程还活着：令牌绑本任务
    stopThread(env, tid, th)
    assert task_result_mcp.tokenTask(tokenAtInject) == ''    # 退出后吊销
    assert injected[0]['type'] == 'http' and injected[0]['name'] == 'deskport'
    assert injected[0]['url'] == 'http://127.0.0.1:6788/api/mcp'
    assert injected[0]['headers'][0]['name'] == task_result_mcp.TOKEN_HEADER
    # 注入发生在 start 之前（start 已被桩记录调用过）
    assert client.started is None
    # 追问文案作为 user 事件落卡（by=平台追发）
    nudges = [e for e in userEvents(env, tid) if e.get('by') == task_result.NUDGE_BY]
    assert len(nudges) == 1 and 'submit_result' in nudges[0]['text']


def test_no_expects_no_injection(env):
    """没有 expects 的任务：不注入、不发令牌。"""
    login(env.client)
    StubKimi.instances = []
    tid = newTask(env, expects=None)
    with env.repo.lock:
        task = env.repo.get(tid)
    th = threading.Thread(target=runner._sessionThread,
                          args=(task, []), kwargs={'clientFactory': StubKimi})
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and not StubKimi.instances:
        time.sleep(0.05)
    stopThread(env, tid, th)
    assert StubKimi.instances[0].mcp_servers == []
    assert task_result_mcp.tokenTask('whatever') == ''


def test_token_dies_at_close_while_session_alive(env):
    """关闭的同一时刻令牌失效（G1 返工项 2）：不依赖会话线程收尾——线程
    还卡在轮里（cancel 时才放行），令牌在 cancel 之前就已吊销。"""
    login(env.client)
    CloseSpyKimi.instances = []
    tid = newTask(env, 'impl')
    with env.repo.lock:
        task = env.repo.get(tid)
    th = threading.Thread(target=runner._sessionThread, args=(task, []),
                          kwargs={'clientFactory': CloseSpyKimi}, daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and not (CloseSpyKimi.instances
                                          and CloseSpyKimi.instances[0].mcp_servers):
        time.sleep(0.05)
    client = CloseSpyKimi.instances[0]
    token = client.mcp_servers[0]['headers'][0]['value']
    assert client.entered.wait(10)          # 轮已开：线程活着且卡在 prompt
    assert task_result_mcp.tokenTask(token) == tid
    assert env.client.post('/api/tasks/%s/close' % tid).status_code == 200
    # cancel 被调用时线程仍在 prompt 里：令牌已 '' = 关闭路径吊销的
    assert client.tokenAtCancel == ''
    resp = env.app.test_client().post(
        '/api/tasks/%s/result' % tid,
        data=json.dumps(IMPL_OK),
        content_type='application/json',
        headers={task_result_mcp.TOKEN_HEADER: token})
    assert resp.status_code == 401
    th.join(timeout=10)
    assert env.repo.get(tid)['status'] == 'closed'


def test_missing_hub_url_skips_injection(env):
    """远端执行点且没配中枢地址：不注入 + 状态事件说明退路。"""
    login(env.client)
    StubKimi.instances = []
    env.monkeypatch.setattr(task_result_mcp, 'hubMcpUrl', lambda node: '')
    tid = newTask(env, 'impl')
    with env.repo.lock:
        task = env.repo.get(tid)
    th = threading.Thread(target=runner._sessionThread,
                          args=(task, []), kwargs={'clientFactory': StubKimi})
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and not StubKimi.instances:
        time.sleep(0.05)
    stopThread(env, tid, th)
    assert StubKimi.instances[0].mcp_servers == []
    with env.repo.lock:
        texts = [e.get('text') or '' for e in env.repo.get(tid).get('events') or []]
    assert any('未注入 submit_result' in t for t in texts)


# ---------------------------------------------------------------- 远端节点路径

def test_remote_node_path_injects_via_payload(env, monkeypatch):
    """远端节点：runner 置 client.mcp_servers → POST /api/sessions 带
    mcpServers（节点侧原样转交 kimi）。"""
    login(env.client)
    monkeypatch.setattr(nodesMod, 'findNode',
                        lambda name: {'name': name, 'url': 'http://127.0.0.1:8622'})
    monkeypatch.setattr(nodesMod.RemoteAcpClient, 'prompt',
                        lambda self, text, timeout=None: {'stopReason': 'end_turn'})
    tid = newTask(env, 'impl', node='ai-xmyz', cwd='/work')
    with env.repo.lock:
        task = env.repo.get(tid)
    factory = nodesMod.clientFactoryFor(task)
    seen = {}

    def wrap(cwd, on_update=None, on_permission=None,
             mode='auto', prompt_timeout=0):
        client = factory(cwd, on_update, on_permission, mode, prompt_timeout)
        seen['client'] = client
        return client

    sent = {}

    def fakeNodeRequest(node, method, path, payload=None, timeout=30):
        if method == 'POST' and path == '/api/sessions':
            sent['payload'] = payload
            return 200, {'sid': 'n1', 'kimiSessionId': 'k1',
                         'attachSeq': 0, 'replayCount': 0}
        return 200, {}

    monkeypatch.setattr(nodesMod, 'nodeRequest', fakeNodeRequest)
    th = threading.Thread(target=runner._sessionThread,
                          args=(task, []), kwargs={'clientFactory': wrap})
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and 'payload' not in sent:
        time.sleep(0.05)
    payload = sent.get('payload')
    tokenAtInject = payload['mcpServers'][0]['headers'][0]['value'] if payload else ''
    assert task_result_mcp.tokenTask(tokenAtInject) == tid
    stopThread(env, tid, th)
    assert payload['mcpServers'][0]['type'] == 'http'
    assert payload['mcpServers'][0]['url'] == 'http://127.0.0.1:6788/api/mcp'
    # 同一只客户端上属性也置了（_ensureLive 复用时还在）
    assert seen['client'].mcp_servers == payload['mcpServers']
    assert task_result_mcp.tokenTask(tokenAtInject) == ''   # 退出后吊销


def test_node_side_passes_mcp_to_kimi(tmp_path, monkeypatch):
    """节点侧：/api/sessions 的 mcpServers → AcpSession.start → kimi 的
    session/new（桩子进程，不起真 kimi）。"""
    seen = {}
    monkeypatch.setattr(task_result_mcp, 'hubMcpUrl', lambda node: 'http://h/api/mcp')

    class FakeProc:
        stdin = stdout = stderr = []
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

    import sys
    sys.path.insert(0, 'agent_node')
    try:
        import acp_session as acpMod
        import node_api as nodeApiMod
        monkeypatch.setattr(acpMod.subprocess, 'Popen',
                            lambda cmd, **kw: FakeProc())
        session = acpMod.AcpSession('s1', '/tmp', 'auto', 'kimi')

        def fakeRequest(method, params, timeout=None):
            seen[method] = params
            if method == 'initialize':
                return {'agentInfo': {}, 'agentCapabilities': {}}
            return {'sessionId': 'session_x', 'modes': {}}
        monkeypatch.setattr(session, '_request', fakeRequest)
        servers = [{'type': 'http', 'name': 'deskport',
                    'url': 'http://h/api/mcp',
                    'headers': [{'name': 'X-Deskport-Session-Token',
                                 'value': 'tok'}]}]
        session.start(None, servers)
        assert seen['session/new']['mcpServers'] == servers
        session.start('session_x', servers)      # session/load 同带
        assert seen['session/load']['mcpServers'] == servers
        session.stop()

        # HTTP 层：create 把 mcp_servers 转交 start
        started = {}

        class StubSession:
            def __init__(self, sid, cwd, mode, kimi_bin, title='', model=''):
                from acp_session import Ring
                self.ring = Ring()

            def start(self, resume_session_id=None, mcp_servers=None):
                started['mcp'] = mcp_servers

            def stop(self):
                pass

            def meta(self):
                return {}
        monkeypatch.setattr(nodeApiMod, 'AcpSession', StubSession)
        mgr = nodeApiMod.SessionManager('kimi', str(tmp_path / 'data'))
        mgr.create('/tmp', 'auto', '', None, '', mcp_servers=servers)
        assert started['mcp'] == servers
    finally:
        sys.path.pop(0)
