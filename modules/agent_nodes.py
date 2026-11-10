# -*- coding: utf-8 -*-
"""远端智能体节点：客户端 + 任务引擎注册 + 节点 API。

传输:
    直连 node url；配置 via=<SSH主机id> 时经 SSH 隧道（agent_tunnel）转发。
客户端:
    RemoteAcpClient 与 KimiAcp 同接口（start/prompt/cancel/set_mode/close），
    传输为 HTTP + 事件轮询；权限决策、事件展示、续跑全部复用任务层。
引擎:
    engine='remote' 的任务在远端节点执行；workspace 仍是本机任务卡目录，
    远端 cwd 用 task['remoteCwd']。会话活在 node 侧，web 重启可 resume。
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

import paramiko
from flask import Blueprint, jsonify

import config
from modules import task_engines
from modules.agent_tunnel import tunnelPort, dropTunnelsFor

blueprint = Blueprint('agent_nodes', __name__)
log = logging.getLogger('agent-nodes')

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 节点寻址

def findNode(name):
    wanted = str(name or '').strip()
    local = config.getLocalAgentNode()
    if local is not None and local['name'] == wanted:
        return local
    for item in config.getAgentNodes():
        if item['name'] == wanted:
            return item
    return None


def listNodeNames():
    return [n['name'] for n in config.getAgentNodes()]


def nodeRequest(node, method, path, payload=None, timeout=30):
    """node API 调用；返回 (status, data)。

    死连接（SSH 空闲被掐 / 隧道断开）自动重建隧道并重试一次：
    请求未达 node 时重发无害；已达但响应丢失时 node 端 409 兜底。
    """
    try:
        return _nodeRequestOnce(node, method, path, payload, timeout)
    except (OSError, EOFError, paramiko.SSHException):
        dropTunnelsFor(node)
        return _nodeRequestOnce(node, method, path, payload, timeout)


def _nodeRequestOnce(node, method, path, payload, timeout):
    base = node['url']
    if node.get('via'):
        base = 'http://127.0.0.1:%d' % tunnelPort(node)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') \
        if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('X-Agent-Token', node.get('token') or '')
    req.add_header('Content-Type', 'application/json')
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace'))
        except ValueError:
            return e.code, {}


# ---------------------------------------------------------------- 客户端

class RemoteAcpClient:
    """KimiAcp 的 HTTP 传输版本：接口一致，任务层零改动复用。"""

    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=1800, node=''):
        self.cwd = cwd
        self.on_update = on_update or (lambda update: None)
        self.on_permission = on_permission or self._defaultPermission
        self.mode = mode
        self.prompt_timeout = prompt_timeout
        self.node_name = node
        self.node_sid = ''
        self.session_id = ''
        self.agent_info = {}
        self.capabilities = {}
        self.stderr_lines = []
        self.exit_code = None
        self._turnEvent = threading.Event()
        self._stopReason = ''
        self._closed = False
        self._lock = threading.Lock()

    @staticmethod
    def _defaultPermission(params):
        options = params.get('options') or []
        for kind in ('allow_always', 'allow_once'):
            for opt in options:
                if opt.get('kind') == kind:
                    return opt.get('optionId')
        return None

    def _node(self):
        node = findNode(self.node_name)
        if node is None:
            raise RuntimeError('agent node 不存在: %s' % self.node_name)
        return node

    # ---- lifecycle ----
    def start(self, session_id=None):
        node = self._node()
        status, data = nodeRequest(node, 'POST', '/api/sessions',
                                   {'cwd': self.cwd, 'mode': self.mode,
                                    'sessionId': session_id or ''}, timeout=90)
        if status != 200:
            raise RuntimeError('远端会话创建失败(%s): %s'
                               % (status, data.get('error') or data))
        self.node_sid = data.get('sid') or ''
        self.session_id = data.get('kimiSessionId') or ''
        if not self.node_sid or not self.session_id:
            raise RuntimeError('远端会话创建返回不完整: %s' % data)
        threading.Thread(target=self._streamLoop, daemon=True).start()
        return self.session_id

    def _openStream(self, node, since, timeout=45):
        """打开 SSE 事件流；返回未读完的 response（逐行迭代）。"""
        base = node['url']
        if node.get('via'):
            base = 'http://127.0.0.1:%d' % tunnelPort(node)
        req = urllib.request.Request(
            base + '/api/sessions/%s/events/stream?since=%d' % (self.node_sid, since))
        req.add_header('X-Agent-Token', node.get('token') or '')
        return _opener.open(req, timeout=timeout)

    def _streamLoop(self):
        """SSE 长连接：事件到达即回调；断开自动重连（since 补差）。"""
        since = 0
        fails = 0
        while not self._closed:
            sessionEnded = False
            try:
                resp = self._openStream(self._node(), since)
                fails = 0
                for raw in resp:
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line or line.startswith(':'):
                        continue   # 空行 / 心跳
                    if line.startswith('event:'):
                        sessionEnded = True   # node 标记会话已结束且事件推完
                        continue
                    if line.startswith('data: '):
                        try:
                            event = json.loads(line[6:])
                        except ValueError:
                            continue
                        since = max(since, event.get('seq', 0))
                        self._handleEvent(event)
                if sessionEnded:
                    return   # 进程已退出且事件推完，无需再连
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # 会话已被 node 侧删除：安静待命直至 close
                    time.sleep(10)
                    continue
            except Exception:
                pass
            # 断连（NAT 掐断/隧道死）是暂时的：重建隧道、放慢节奏重连，
            # 事件从 since 补差，不会丢失
            try:
                dropTunnelsFor(self._node())
            except Exception:
                pass
            fails += 1
            time.sleep(min(fails, 10))

    def _handleEvent(self, event):
        kind = event.get('type')
        if kind == 'ready':
            self.agent_info = event.get('agentInfo') or {}
        elif kind == 'update':
            if event.get('replay'):
                return   # resume 重放的历史，任务卡里已有
            try:
                self.on_update(event.get('update') or {})
            except Exception:
                pass
        elif kind == 'permission_request':
            params = {'toolCall': {'title': event.get('title') or ''},
                      'options': event.get('options') or []}
            requestId = event.get('requestId') or ''
            try:
                optionId = self.on_permission(params, requestId)
            except TypeError:
                optionId = self.on_permission(params)
            try:
                status, data = nodeRequest(
                    self._node(), 'POST',
                    '/api/sessions/%s/permission' % self.node_sid,
                    {'requestId': requestId, 'optionId': optionId or ''},
                    timeout=10)
                if status != 200:
                    raise RuntimeError('HTTP %s: %s'
                                       % (status, data.get('error') or data))
            except Exception as e:
                # 决策丢失会让 node 端权限挂到超时、两端状态分叉——必须留痕
                note = '[agent-nodes] 权限决策提交失败: %s' % e
                self.stderr_lines.append(note)
                try:
                    log.warning('%s requestId=%s', note, requestId)
                except Exception:
                    pass
        elif kind == 'turn_end':
            self._stopReason = event.get('stopReason') or ''
            self._turnEvent.set()
        elif kind == 'exit':
            err = event.get('stderr') or ''
            if err:
                self.stderr_lines.extend(err.splitlines()[-10:])
            self.exit_code = event.get('code')
            self._turnEvent.set()

    # ---- turns ----
    def prompt(self, text, timeout=None):
        self._turnEvent.clear()
        self._stopReason = ''
        node = self._node()
        status, data = nodeRequest(
            node, 'POST', '/api/sessions/%s/prompt' % self.node_sid,
            {'text': text}, timeout=30)
        if status == 409:
            raise RuntimeError('远端会话正在执行，请稍候')
        if status != 202:
            raise RuntimeError('远端派活失败(%s): %s'
                               % (status, data.get('error') or data))
        wait = timeout or self.prompt_timeout or None
        if wait == 0:
            wait = None   # 常驻会话：不限时等待回合结束
        if not self._turnEvent.wait(wait):
            raise RuntimeError('远端回合超时')
        if self.exit_code is not None and not self._stopReason:
            raise RuntimeError('远端 ACP 进程已退出 (code=%s)' % self.exit_code)
        return {'stopReason': self._stopReason}

    def set_mode(self, mode):
        self.mode = mode
        status, data = nodeRequest(
            self._node(), 'POST', '/api/sessions/%s/mode' % self.node_sid,
            {'mode': mode}, timeout=15)
        if status != 200:
            raise RuntimeError('set_mode 失败(%s): %s'
                               % (status, data.get('error') or data))

    def cancel(self):
        try:
            nodeRequest(self._node(), 'POST',
                        '/api/sessions/%s/cancel' % self.node_sid,
                        {}, timeout=10)
        except Exception:
            pass

    def close(self):
        """只断开事件通道；远端会话进程保持存活以供随时接手。"""
        self._closed = True
        self._turnEvent.set()

    @property
    def pid(self):
        return 0

    def poll(self):
        return self.exit_code if self.exit_code is not None else 0


# ---------------------------------------------------------------- 引擎

def remoteClientFactory(task):
    """按任务生成与 KimiAcp 同签名的客户端工厂（cwd 换成远端目录）。"""
    node = task.get('node') or ''
    remoteCwd = task.get('remoteCwd') or ''

    def factory(cwd, on_update=None, on_permission=None,
                mode='auto', prompt_timeout=0):
        return RemoteAcpClient(cwd=remoteCwd or cwd,
                               on_update=on_update,
                               on_permission=on_permission,
                               mode=mode, prompt_timeout=prompt_timeout,
                               node=node)
    return factory


def clientFactoryFor(task):
    """统一客户端选择：remote→指定节点；acp→本机节点（启用时）否则 web 内 stdio。

    本机 node 化后 engine=acp 的会话也由本机 agent node 托管，
    web 重启不再打断任务；未启用（localAgentNodePort=0）时回退 KimiAcp。
    """
    from modules.kimi_acp import KimiAcp
    engine = (task.get('engine') or 'acp').lower()
    if engine == 'remote':
        return remoteClientFactory(task)
    local = config.getLocalAgentNode()
    if local is not None:
        nodeName = local['name']

        def factory(cwd, on_update=None, on_permission=None,
                    mode='auto', prompt_timeout=0):
            return RemoteAcpClient(cwd=cwd,
                                   on_update=on_update,
                                   on_permission=on_permission,
                                   mode=mode, prompt_timeout=prompt_timeout,
                                   node=nodeName)
        return factory
    return KimiAcp


def _remoteSessionThread(task, manifest):
    from modules import tasks_runner
    tasks_runner._sessionThread(task, manifest,
                                clientFactory=remoteClientFactory(task))


task_engines.register('remote', task_engines.ThreadEngine(_remoteSessionThread))


def _acpSessionThread(task, manifest):
    from modules import tasks_runner
    tasks_runner._sessionThread(task, manifest, clientFactory=clientFactoryFor(task))


task_engines.register('acp', task_engines.ThreadEngine(_acpSessionThread))


# ---------------------------------------------------------------- API

@blueprint.route('/api/tasks/nodes')
def nodesList():
    return jsonify({'nodes': [{'name': n['name'], 'via': n['via']}
                              for n in config.getAgentNodes()]})


@blueprint.route('/api/tasks/nodes/<name>/health')
def nodeHealth(name):
    node = findNode(name)
    if node is None:
        return jsonify({'error': '节点不存在'}), 404
    try:
        status, data = nodeRequest(node, 'GET', '/health', timeout=8)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]})
    if status != 200:
        return jsonify({'ok': False, 'error': 'HTTP %s' % status})
    data.pop('ok', None)
    data['ok'] = True
    return jsonify(data)
