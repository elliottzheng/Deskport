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


_modelsCache = {'at': 0.0, 'data': {'models': [], 'default': ''}}
_healthModels = {'at': 0.0, 'data': {}}
_healthModelsLock = threading.Lock()


def _refreshHealthModels():
    """拉取各 node /health 的 models 字段（新版 node 自动上报 kimi 模型清单）。

    联邦成员额外查它自己的 /api/tasks/nodes——成员的 local 节点模型即
    成员可提供的模型（第三轮反馈：i7-4710MQ 之前一直 selectable=false）。
    """
    result = {}
    targets = list(config.getAgentNodes())
    local = config.getLocalAgentNode()
    if local is not None:
        targets.append(local)
    for node in targets:
        try:
            status, data = nodeRequest(node, 'GET', '/health', timeout=5)
        except Exception:
            continue
        if status == 200 and isinstance(data, dict):
            result[node['name']] = {'models': data.get('models') or [],
                                    'default': data.get('defaultModel') or ''}
    for member in config.getRemotes():
        try:
            from modules.tasks_federation import _memberCall
            status, data = _memberCall(member, 'GET', '/api/tasks/nodes',
                                       timeout=8)
        except Exception:
            continue
        if status == 200 and isinstance(data, dict):
            own = next((n for n in data.get('nodes') or []
                        if n.get('name') == 'local'), None)
            if own and (own.get('models') or own.get('defaultModel')):
                result[member['name']] = {'models': own.get('models') or [],
                                          'default': own.get('defaultModel') or ''}
    with _healthModelsLock:
        _healthModels['data'] = result
        _healthModels['at'] = time.monotonic()


def _healthModelsCached(maxAgeSec=60):
    """返回缓存；过期则触发后台刷新（接口立即返回旧值，不阻塞）。"""
    if time.monotonic() - _healthModels['at'] > maxAgeSec:
        threading.Thread(target=_refreshHealthModels, daemon=True).start()
    with _healthModelsLock:
        return dict(_healthModels['data'])


def nodeModelsFor(name, entry=None):
    """执行点的模型清单：config 声明 → node /health 上报 →（仅本机）config.toml。"""
    if entry is None:
        entry = findNode(name) or findFederationMember(name) or {}
    if entry.get('models') or entry.get('model'):
        return list(entry.get('models') or []), str(entry.get('model') or '')
    cached = _healthModelsCached().get(name or '', {})
    if cached.get('models') or cached.get('default'):
        return list(cached.get('models') or []), str(cached.get('default') or '')
    if (name or 'local') == 'local':
        models = localKimiModels()
        return models['models'], models['default']
    return [], ''


def localKimiModels(maxAgeSec=60):
    """本机 kimi CLI 的可选模型（config.toml 自动发现，60s 缓存）。

    本机 agent node 未启用（stdio 直跑）时 Deskport 自己解析；与
    agent_node/acp_session.discoverModels 是同一份 kimi 配置。
    """
    import time as _time
    now = _time.monotonic()
    if now - _modelsCache['at'] > maxAgeSec:
        import os as _os
        path = _os.path.join(_os.path.expanduser('~'), '.kimi-code', 'config.toml')
        try:
            import tomllib
            with open(path, 'rb') as f:
                data = tomllib.load(f)
            raw = data.get('models')
            result = {'models': sorted(str(k) for k in raw.keys())
                      if isinstance(raw, dict) else [],
                      'default': str(data.get('default_model') or '')}
        except (OSError, ValueError):
            result = {'models': [], 'default': ''}
        _modelsCache['at'] = now
        _modelsCache['data'] = result
    return _modelsCache['data']


def nodeModelDecl(name=''):
    """执行点的模型声明：{name, models 可选清单, default}。

    name 空 = engine=acp 的执行点（本机 node 或 web 内 stdio）。
    优先级：config 显式声明 → node /health 自动上报（新版 node）→
    本机 kimi config.toml 自动发现。全空 = 该节点不能选模型（§7.5）。
    """
    wanted = str(name or '').strip()
    if not wanted or wanted == 'local':
        local = config.getLocalAgentNode()
        if local is None:
            models, default = nodeModelsFor('local', {'name': 'local'})
            return {'name': 'local', 'models': models, 'default': default}
        models, default = nodeModelsFor(local['name'], local)
        return {'name': local['name'], 'models': models, 'default': default}
    target = findNode(wanted) or findFederationMember(wanted)
    if target is None:
        return {'name': wanted, 'models': [], 'default': ''}
    models, default = nodeModelsFor(wanted, target)
    return {'name': target.get('name') or wanted,
            'models': models, 'default': default}


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
                 mode='auto', prompt_timeout=1800, node='', model=''):
        self.cwd = cwd
        self.on_update = on_update or (lambda update: None)
        self.on_permission = on_permission or self._defaultPermission
        self.mode = mode
        self.model = str(model or '').strip()
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
                                    'sessionId': session_id or '',
                                    'model': self.model}, timeout=90)
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
            params = {'toolCall': {'title': event.get('title') or '',
                                   'toolCallId': event.get('callId') or '',
                                   'rawInput': event.get('args') or ''},
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

    def shutdown(self):
        """用户明确结束任务：让 node 立刻回收 ACP 进程（第二轮反馈 §3）。"""
        sid = self.node_sid
        if not sid:
            return
        try:
            nodeRequest(self._node(), 'DELETE', '/api/sessions/%s' % sid,
                        timeout=10)
        except Exception:
            pass

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
    taskModel = task.get('model') or ''

    def factory(cwd, on_update=None, on_permission=None,
                mode='auto', prompt_timeout=0):
        return RemoteAcpClient(cwd=remoteCwd or cwd,
                               on_update=on_update,
                               on_permission=on_permission,
                               mode=mode, prompt_timeout=prompt_timeout,
                               node=node, model=taskModel)
    return factory


def findFederationMember(name):
    """按 id 或 name 查联邦成员 Deskport（可作任务执行点）。"""
    from modules.tasks_federation import findFederationMember as _find
    return _find(name)


def clientFactoryFor(task):
    """统一客户端选择：remote→指定执行点（agent node 或联邦成员）；
    acp→本机节点（启用时）否则 web 内 stdio。

    本机 node 化后 engine=acp 的会话也由本机 agent node 托管，
    web 重启不再打断任务；未启用（localAgentNodePort=0）时回退 KimiAcp。
    """
    from modules.kimi_acp import KimiAcp
    engine = (task.get('engine') or 'acp').lower()
    taskModel = task.get('model') or ''
    if engine == 'remote':
        nodeName = task.get('node') or ''
        if findFederationMember(nodeName) is not None:
            from modules.tasks_federation import FederationTaskClient
            firstPrompt = task.get('prompt') or ''

            def fedFactory(cwd, on_update=None, on_permission=None,
                           mode='auto', prompt_timeout=0):
                return FederationTaskClient(
                    cwd=cwd, on_update=on_update,
                    on_permission=on_permission,
                    mode=task.get('mode') or mode,
                    prompt_timeout=prompt_timeout,
                    member=nodeName, first_prompt=firstPrompt,
                    model=taskModel, policy=task.get('policy') or {})
            return fedFactory
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
                                   node=nodeName, model=taskModel)
        return factory

    def stdioFactory(cwd, on_update=None, on_permission=None,
                     mode='auto', prompt_timeout=0):
        return KimiAcp(cwd=cwd, on_update=on_update,
                       on_permission=on_permission,
                       mode=mode, prompt_timeout=prompt_timeout,
                       model=taskModel)
    return stdioFactory


def _remoteSessionThread(task, manifest):
    from modules import tasks_runner
    tasks_runner._sessionThread(task, manifest, clientFactory=clientFactoryFor(task))


task_engines.register('remote', task_engines.ThreadEngine(_remoteSessionThread))


def _acpSessionThread(task, manifest):
    from modules import tasks_runner
    tasks_runner._sessionThread(task, manifest, clientFactory=clientFactoryFor(task))


task_engines.register('acp', task_engines.ThreadEngine(_acpSessionThread))


# ---------------------------------------------------------------- API
# 节点列表与健康检查路由在 modules/tasks_federation.py
# （agent_nodes 与联邦成员统一呈现，agent_nodes 行数预算紧张）。
