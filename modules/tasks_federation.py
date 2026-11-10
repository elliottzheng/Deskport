# -*- coding: utf-8 -*-
"""联邦任务客户端：把联邦成员 Deskport 的任务模块当作执行点。

AcpClient 契约的第三个实现（KimiAcp=stdio / RemoteAcpClient=node /
FederationTaskClient=联邦成员中枢）。与前两者不同：会话编排完全发生在
对端（对端自管 node/回退/stdio），本实现只做「代理镜像」——
把对端任务事件翻译成 ACP update 喂回本机任务卡，把本机审批转发给对端。

监督面语义：本机任务卡是对端任务的代理视图；对端中枢照常自监督。
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from flask import jsonify

import config
from modules import task_engines
from modules.agent_nodes import blueprint, findNode, nodeRequest
from modules.agent_tunnel import tunnelPort, dropTunnelsFor

log = logging.getLogger('federation-tasks')


# ---------------------------------------------------------------- 节点呈现
# agent node 与联邦成员统一呈现在 /api/tasks/nodes，供前端徽标与健康探测。

@blueprint.route('/api/tasks/nodes')
def nodesList():
    nodes = [{'name': n['name'], 'via': n['via']}
             for n in config.getAgentNodes()]
    for member in config.getRemotes():
        nodes.append({'name': member['name'], 'via': 'federation'})
    return jsonify({'nodes': nodes})


@blueprint.route('/api/tasks/nodes/<name>/health')
def nodeHealth(name):
    if findNode(name) is not None:
        return _agentNodeHealth(name)
    member = findFederationMember(name)
    if member is None:
        return jsonify({'error': '节点不存在'}), 404
    try:
        status, data = _memberCall(member, 'GET', '/health', timeout=8)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]})
    if status != 200:
        return jsonify({'ok': False, 'error': 'HTTP %s' % status})
    return jsonify({'ok': True, 'kind': 'federation'})


def _agentNodeHealth(name):
    try:
        status, data = nodeRequest(findNode(name), 'GET', '/health', timeout=8)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]})
    if status != 200:
        return jsonify({'ok': False, 'error': 'HTTP %s' % status})
    data.pop('ok', None)
    data['ok'] = True
    return jsonify(data)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def findFederationMember(name):
    """按 id 或 name 查联邦成员；返回 config.getRemotes() 条目或 None。"""
    wanted = str(name or '').strip()
    if not wanted:
        return None
    import config as _config
    for item in _config.getRemotes():
        if item['id'] == wanted or item['name'] == wanted:
            return item
    return None


def _memberCall(member, method, path, payload=None, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') \
        if payload is not None else None
    req = urllib.request.Request(member['base'] + path, data=data,
                                 method=method)
    req.add_header('X-Deskport-Token', member.get('token') or '')
    req.add_header('Content-Type', 'application/json')
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace'))
        except ValueError:
            return e.code, {}


class FederationTaskClient:
    """对端任务 REST → AcpClient 契约的代理适配器。"""

    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=1800, member='', first_prompt=''):
        self.cwd = cwd                      # 不跨机；对端 workspace 自管
        self.on_update = on_update or (lambda update: None)
        self.on_permission = on_permission or (lambda params, rid: '')
        self.mode = mode
        self.prompt_timeout = prompt_timeout
        self.member = member
        self.first_prompt = first_prompt
        self.bid = ''                       # 对端任务 id
        self.stderr_lines = []
        self.exit_code = None
        self._after = 0
        self._turnEvent = threading.Event()
        self._stopReason = ''
        self._failError = ''
        self._firstPending = True
        self._closed = False

    # ---- transport ----
    def _member(self):
        member = findFederationMember(self.member)
        if member is None:
            raise RuntimeError('联邦成员不存在: %s' % self.member)
        return member

    def _call(self, method, path, payload=None, timeout=20):
        return _memberCall(self._member(), method, path, payload, timeout)

    # ---- lifecycle ----
    def start(self, session_id=None):
        if session_id:
            # resume：镜像对端既有任务，从当前 seq 之后开始只推新事件
            self.bid = session_id
            self._firstPending = False
            status, data = self._call('GET', '/api/tasks/%s' % self.bid,
                                      timeout=20)
            if status != 200:
                raise RuntimeError('联邦任务不存在(%s): %s'
                                   % (self.bid, data.get('error') or data))
            self._after = max((e.get('seq', 0)
                               for e in data.get('events') or []), default=0)
        else:
            status, data = self._call(
                'POST', '/api/tasks',
                {'prompt': self.first_prompt, 'mode': self.mode}, timeout=60)
            if status != 202:
                raise RuntimeError('联邦任务创建失败(%s): %s'
                                   % (status, data.get('error') or data))
            self.bid = data['task']['id']
        threading.Thread(target=self._mirrorLoop, daemon=True).start()
        return self.bid

    # ---- 镜像 ----
    def _mirrorLoop(self):
        fails = 0
        while not self._closed:
            try:
                status, data = self._call(
                    'GET', '/api/tasks/%s/events?after=%d' % (self.bid, self._after),
                    timeout=15)
                if status == 404:
                    time.sleep(10)
                    continue
                if status != 200:
                    raise RuntimeError('events %s' % status)
                fails = 0
                peerStatus = data.get('status') or ''
                for event in data.get('events') or []:
                    self._after = max(self._after, event.get('seq', 0))
                    self._mirror(event)
                if peerStatus in ('waiting', 'paused', 'failed', 'closed',
                                  'cancelled', 'interrupted'):
                    self._onPeerSettled(peerStatus)
            except Exception as e:
                fails += 1
                log.warning('镜像断连(%d): %s', fails, e)
                time.sleep(min(fails, 10))

    def _onPeerSettled(self, peerStatus):
        """对端回合落定 → 解除本端 prompt 阻塞。"""
        mapping = {'waiting': 'end_turn', 'paused': 'end_turn',
                   'failed': 'error', 'interrupted': 'error',
                   'closed': 'cancelled', 'cancelled': 'cancelled'}
        self._stopReason = mapping.get(peerStatus, peerStatus)
        task = self._snapshot()
        if peerStatus in ('failed', 'interrupted'):
            self._failError = task.get('error') or '对端任务失败'
        if peerStatus in ('closed', 'cancelled'):
            self.exit_code = 0
        self._turnEvent.set()

    def _snapshot(self):
        try:
            status, data = self._call('GET', '/api/tasks/%s' % self.bid,
                                      timeout=10)
            if status == 200:
                return data.get('task') or {}
        except Exception:
            pass
        return {}

    def _mirror(self, event):
        kind = event.get('type')
        update = None
        if kind == 'assistant':
            update = {'sessionUpdate': 'agent_message_chunk',
                      'content': {'type': 'text', 'text': event.get('text') or ''}}
        elif kind == 'tool':
            update = {'sessionUpdate': 'tool_call',
                      'toolCallId': event.get('callId') or '',
                      'title': event.get('name') or 'tool',
                      'kind': 'execute', 'status': event.get('status') or '',
                      'rawInput': event.get('args') or ''}
        elif kind == 'tool_result':
            update = {'sessionUpdate': 'tool_call_update',
                      'toolCallId': event.get('callId') or '',
                      'status': 'completed',
                      'content': {'type': 'text', 'text': event.get('text') or ''},
                      'rawOutput': event.get('text') or ''}
        elif kind == 'plan':
            entries = [{'status': 'pending', 'content': line}
                       for line in (event.get('text') or '').splitlines()
                       if line.strip()]
            update = {'sessionUpdate': 'plan', 'entries': entries}
        elif kind == 'permission_request':
            params = {'toolCall': {'title': event.get('title') or ''},
                      'options': event.get('options') or []}
            requestId = event.get('requestId') or ''
            try:
                optionId = self.on_permission(params, requestId)
            except TypeError:
                optionId = self.on_permission(params)
            payload = {'requestId': requestId,
                       'optionId': optionId or '', 'deny': not optionId}
            try:
                status, data = self._call(
                    'POST', '/api/tasks/%s/permission' % self.bid,
                    payload, timeout=15)
                if status != 200:
                    raise RuntimeError('HTTP %s: %s'
                                       % (status, data.get('error') or data))
            except Exception as e:
                note = '[federation] 权限决策转发失败: %s' % e
                self.stderr_lines.append(note)
                log.warning('%s requestId=%s', note, requestId)
            return
        if update is not None:
            try:
                self.on_update(update)
            except Exception as e:
                log.warning('on_update 异常: %s', e)

    # ---- turns ----
    def prompt(self, text, timeout=None):
        self._turnEvent.clear()
        self._stopReason = ''
        self._failError = ''
        if self._firstPending:
            self._firstPending = False   # 首轮已随建任务发给对端，只等结果
        else:
            status, data = self._call(
                'POST', '/api/tasks/%s/message' % self.bid, {'text': text},
                timeout=30)
            if status not in (200, 202):
                raise RuntimeError('联邦续聊失败(%s): %s'
                                   % (status, data.get('error') or data))
        wait = timeout or self.prompt_timeout or None
        if wait == 0:
            wait = None
        if not self._turnEvent.wait(wait):
            raise RuntimeError('联邦回合超时')
        if self._failError:
            raise RuntimeError(self._failError)
        return {'stopReason': self._stopReason or 'end_turn'}

    def set_mode(self, mode):
        self.mode = mode
        status, data = self._call('POST', '/api/tasks/%s/mode' % self.bid,
                                  {'mode': mode}, timeout=15)
        if status != 200:
            raise RuntimeError('set_mode 失败(%s): %s'
                               % (status, data.get('error') or data))

    def cancel(self):
        try:
            self._call('POST', '/api/tasks/%s/cancel' % self.bid, {},
                       timeout=10)
        except Exception:
            pass

    def close(self):
        """只断开镜像；对端任务继续由对端中枢托管。"""
        self._closed = True
        self._turnEvent.set()

    @property
    def pid(self):
        return 0

    def poll(self):
        return self.exit_code if self.exit_code is not None else 0
