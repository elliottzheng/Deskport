# -*- coding: utf-8 -*-
"""ACP 客户端对拍契约：同一份假 kimi acp 交互脚本喂给两个实现，
断言事件路由、权限回写、回合语义完全一致——防协议漂移。"""
import io
import json
import queue
import time

import pytest

from modules.kimi_acp import KimiAcp

DONE = object()
PERM_REQ_ID = 900
OPTION_ID = 'allow-once-1'

UPDATES = [
    {'sessionUpdate': 'agent_message_chunk',
     'content': {'type': 'text', 'text': '你好'}},
    {'sessionUpdate': 'tool_call', 'toolCallId': 't1',
     'title': 'Bash', 'kind': 'execute', 'status': 'in_progress'},
    {'sessionUpdate': 'plan', 'entries': [{'status': 'done', 'content': '步骤一'}]},
]
PERMISSION_PARAMS = {
    'sessionId': 'sess_golden', 'toolCall': {'title': 'Bash'},
    'options': [{'optionId': OPTION_ID, 'name': 'Approve once',
                 'kind': 'allow_once'},
                {'optionId': 'reject-1', 'name': 'Reject', 'kind': 'reject_once'}],
}


class FakeAcpProc:
    """假 kimi acp 子进程：stdin 写入驱动预排响应/通知输出。

    实现了 Popen 产物被消费的全部接口：stdin（write/flush）、
    stdout（行迭代）、stderr、poll/terminate/kill/wait。
    """

    def __init__(self, updates, permission=False):
        self.q = queue.Queue()
        self.sent = []
        self.updates = updates
        self.permission = permission
        self.stdin = self
        self.stdout = self
        self.stderr = io.StringIO()
        self.pid = 4242

    def write(self, text):
        line = text.strip()
        if not line:
            return
        self.sent.append(line)
        obj = json.loads(line)
        method = obj.get('method', '')
        rid = obj.get('id')
        if method == 'initialize':
            self._reply(rid, {'protocolVersion': 1, 'agentCapabilities': {},
                              'agentInfo': {'name': 'fake', 'version': '0'}})
        elif method == 'session/new':
            self._reply(rid, {'sessionId': 'sess_golden',
                              'modes': {'available': []}})
        elif method == 'session/set_mode':
            self._reply(rid, {})
        elif method == 'session/prompt':
            for update in self.updates:
                self._notify('session/update',
                             {'sessionId': 'sess_golden', 'update': update})
            if self.permission:
                self._notify('session/request_permission',
                             dict(PERMISSION_PARAMS), reqId=PERM_REQ_ID)
            self._reply(rid, {'stopReason': 'end_turn'})
            self.q.put(DONE)

    def flush(self):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        item = self.q.get(timeout=15)
        if item is DONE:
            raise StopIteration
        return item

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def _reply(self, rid, result):
        self.q.put(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': result},
                              ensure_ascii=False) + '\n')

    def _notify(self, method, params, reqId=None):
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if reqId is not None:
            payload['id'] = reqId
        self.q.put(json.dumps(payload, ensure_ascii=False) + '\n')


@pytest.fixture
def patch_popen(monkeypatch):
    """把 subprocess.Popen 换成预排脚本的假进程（测试结束自动恢复）。"""
    import subprocess

    def _install(updates=(), permission=False):
        proc = FakeAcpProc(list(updates), permission)

        def patched(*_args, **_kwargs):
            return proc

        monkeypatch.setattr(subprocess, 'Popen', patched)
        return proc

    return _install


def _sentReply(proc, reqId):
    out = []
    for line in proc.sent:
        obj = json.loads(line)
        if obj.get('id') == reqId and 'result' in obj:
            out.append(obj['result']['outcome'])
    return out


def _waitTurnEnd(session, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if [e for e in session.ring.since(0) if e['type'] == 'turn_end']:
            return True
        time.sleep(0.2)
    return False


def test_update_stream_parity(patch_popen):
    """无权限场景：update 路由与回合结束两边一致。"""
    proc1 = patch_popen(UPDATES)
    seen = []
    kimi = KimiAcp(cwd='.', on_update=seen.append, mode='auto')
    kimi.start()
    result = kimi.prompt('做点事')
    kimi.close()

    proc2 = patch_popen(UPDATES)
    from agent_node.acp_session import AcpSession
    session = AcpSession('sid-x', cwd='.', mode='auto', kimi_bin='unused')
    session.start()
    session.enqueuePrompt('做点事')
    assert _waitTurnEnd(session)
    session.stop()

    # 对拍：update 序列逐字段一致（kimi 全收；node 透传进 ring）
    assert seen == UPDATES
    node_updates = [e['update'] for e in session.ring.since(0)
                    if e['type'] == 'update' and not e.get('replay')]
    assert node_updates == UPDATES
    assert result.get('stopReason') == 'end_turn'
    # 协议协商序列一致：initialize → session/new（→ set_mode）
    methods1 = [json.loads(s).get('method') for s in proc1.sent]
    methods2 = [json.loads(s).get('method') for s in proc2.sent]
    assert methods1[:2] == methods2[:2] == ['initialize', 'session/new']


def test_permission_parity(patch_popen):
    """权限场景：默认决策策略与回写报文两边一致。"""
    proc1 = patch_popen(UPDATES[:1], permission=True)
    kimi = KimiAcp(cwd='.', on_update=lambda u: None, mode='auto')
    kimi.start()
    result = kimi.prompt('需要权限的事')
    kimi.close()

    proc2 = patch_popen(UPDATES[:1], permission=True)
    from agent_node.acp_session import AcpSession
    session = AcpSession('sid-x', cwd='.', mode='auto', kimi_bin='unused')
    session.start()
    session.enqueuePrompt('需要权限的事')
    option = None
    deadline = time.time() + 15
    while time.time() < deadline and option is None:
        for event in session.ring.since(0):
            if event['type'] == 'permission_request':
                for opt in event['options']:
                    if opt['kind'] == 'allow_once':
                        option = opt['optionId']
        if option is None:
            time.sleep(0.2)
    assert option == OPTION_ID
    assert session.submitDecision(PERM_REQ_ID, option)
    assert _waitTurnEnd(session)
    session.stop()

    outcome1 = _sentReply(proc1, PERM_REQ_ID)
    outcome2 = _sentReply(proc2, PERM_REQ_ID)
    assert outcome1 == outcome2 == [{'outcome': 'selected', 'optionId': OPTION_ID}]
    assert result.get('stopReason') == 'end_turn'
