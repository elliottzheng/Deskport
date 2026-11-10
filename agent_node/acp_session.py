# -*- coding: utf-8 -*-
"""ACP 会话托管核心：Ring 事件缓冲 + AcpSession（kimi acp 子进程托管）。

纯标准库；与 HTTP 层（node_api）解耦，可独立测试。
"""
import json
import os
import re
import subprocess
import threading
import time

PROTOCOL_VERSION = 1
MAX_EVENTS = 5000
PERMISSION_TIMEOUT = 900          # node 端兜底；Deskport 端 300s 先到
IDLE_KILL_SECONDS = 45 * 60       # 空闲进程回收；会话可 resume，不丢上下文
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
KIMI_CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.kimi-code',
                                'config.toml')


def discoverModels(configPath=None):
    """从 kimi CLI 的 config.toml 枚举可选模型（反馈 §7.2 自动发现）。

    返回 {models: [别名...], default: 别名}；文件缺失/损坏返回空。
    node 可能跑在老 Python 上（系统 python3 无 tomllib）——那时退化为
    轻量正则解析，只覆盖顶层 default_model 与 [models."别名"] 段名。
    """
    path = configPath or KIMI_CONFIG_PATH
    try:
        with open(path, 'rb') as f:
            text = f.read()
    except OSError:
        return {'models': [], 'default': ''}
    try:
        import tomllib
        data = tomllib.loads(text.decode('utf-8'))
        raw = data.get('models')
        models = sorted(str(k) for k in raw.keys()) if isinstance(raw, dict) else []
        default = str(data.get('default_model') or '')
    except ImportError:
        try:
            text = text.decode('utf-8', 'replace')
        except Exception:
            return {'models': [], 'default': ''}
        models = sorted(set(re.findall(r'\[models\."([^"\]]+)"\]', text)))
        m = re.search(r'^\s*default_model\s*=\s*"([^"]+)"', text, re.M)
        default = m.group(1) if m else ''
    except Exception:
        return {'models': [], 'default': ''}
    return {'models': models, 'default': default}


def nowMs():
    return int(time.time() * 1000)


class Ring:
    """seq 单调递增的事件环形缓冲；客户端按 since 增量拉取，可阻塞等待新事件。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._items = []
        self._seq = 0

    def append(self, record):
        with self._cond:
            self._seq += 1
            record = dict(record)
            record['seq'] = self._seq
            record['at'] = nowMs()
            self._items.append(record)
            if len(self._items) > MAX_EVENTS:
                del self._items[:len(self._items) - MAX_EVENTS]
            self._cond.notify_all()
            return self._seq

    def seq(self):
        with self._cond:
            return self._seq

    def since(self, n):
        with self._cond:
            return [e for e in self._items if e.get('seq', 0) > n]

    def waitSince(self, n, timeout=25):
        """阻塞直到出现 seq>n 的事件或超时；返回 (events, latestSeq)。"""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > n, timeout)
            events = [e for e in self._items if e.get('seq', 0) > n]
            return events, self._seq


class AcpSession:
    """托管一个 kimi acp 子进程；update 透传进 ring，权限请求挂起等决策。"""

    def __init__(self, sid, cwd, mode, kimi_bin, title='', model=''):
        self.sid = sid
        self.cwd = cwd
        self.mode = mode
        self.kimi_bin = kimi_bin
        self.title = (title or '')[:60]
        self.model = str(model or '').strip()
        self.kimi_session_id = None
        self.created_at = nowMs()
        self.last_active = nowMs()
        self.running = False
        self.dead = False
        self.exit_code = None
        self.ring = Ring()
        self.proc = None
        self.stderr_lines = []
        self._id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._decisions = {}      # acpRequestId -> {'value': optionId|None, 'event': Event}
        self._turnQueue = []
        self._loading = False

    # ---- JSON-RPC transport ----
    def _send(self, obj):
        with self._lock:
            if not self.proc or not self.proc.stdin:
                raise RuntimeError('ACP 进程未启动')
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def _request(self, method, params, timeout=60):
        import concurrent.futures
        fut = concurrent.futures.Future()
        with self._lock:
            self._id += 1
            reqId = self._id
            self._pending[reqId] = fut
        self._send({'jsonrpc': '2.0', 'id': reqId, 'method': method, 'params': params})
        try:
            return fut.result(timeout=timeout or None)
        except concurrent.futures.TimeoutError:
            raise RuntimeError('ACP 请求超时: %s' % method)
        finally:
            with self._lock:
                self._pending.pop(reqId, None)

    def _readLoop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                self._dispatch(obj)
        except Exception:
            pass
        finally:
            self._onExit()

    def _dispatch(self, obj):
        if obj.get('id') is not None and 'method' not in obj:
            with self._lock:
                fut = self._pending.get(obj.get('id'))
            if fut and not fut.done():
                if obj.get('error'):
                    fut.set_exception(RuntimeError(str(obj['error'].get('message') or obj['error'])))
                else:
                    fut.set_result(obj.get('result') or {})
            return
        method = obj.get('method')
        params = obj.get('params') or {}
        if method == 'session/update':
            self.ring.append({'type': 'update', 'replay': self._loading,
                              'update': params.get('update') or {}})
        elif method == 'session/request_permission':
            self._handlePermission(params, obj.get('id'))
        elif method:
            self._send({'jsonrpc': '2.0', 'id': obj.get('id'),
                        'error': {'code': -32601, 'message': 'method not found'}})

    def _handlePermission(self, params, acpReqId):
        requestId = str(acpReqId)
        toolCall = params.get('toolCall') or {}
        options = [{'optionId': o.get('optionId') or '', 'name': o.get('name') or '',
                    'kind': o.get('kind') or ''}
                   for o in (params.get('options') or []) if isinstance(o, dict)]
        rawInput = toolCall.get('rawInput')
        argsText = rawInput if isinstance(rawInput, str) \
            else json.dumps(rawInput, ensure_ascii=False) if rawInput is not None else ''
        self.ring.append({'type': 'permission_request', 'requestId': requestId,
                          'title': toolCall.get('title') or '工具调用',
                          'callId': toolCall.get('toolCallId') or '',
                          'args': argsText[:2000],
                          'options': options})
        decision = {'value': None, 'event': threading.Event()}
        with self._lock:
            self._decisions[acpReqId] = decision
        decision['event'].wait(PERMISSION_TIMEOUT)
        with self._lock:
            self._decisions.pop(acpReqId, None)
        value = decision.get('value')
        self.last_active = nowMs()
        if value:
            self.ring.append({'type': 'permission_result', 'requestId': requestId,
                              'optionId': value, 'status': 'approved'})
            self._send({'jsonrpc': '2.0', 'id': acpReqId,
                        'result': {'outcome': {'outcome': 'selected', 'optionId': value}}})
        else:
            self.ring.append({'type': 'permission_result', 'requestId': requestId,
                              'status': 'timeout' if not decision.get('closed') else 'denied'})
            self._send({'jsonrpc': '2.0', 'id': acpReqId,
                        'result': {'outcome': {'outcome': 'cancelled'}}})

    def _readStderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip())
                if len(self.stderr_lines) > 200:
                    del self.stderr_lines[:100]
        except Exception:
            pass

    def _onExit(self):
        if self.dead:
            return
        self.dead = True
        self.running = False
        try:
            self.exit_code = self.proc.poll()
        except Exception:
            self.exit_code = -1
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(RuntimeError('ACP 进程已退出'))
        self.ring.append({'type': 'exit', 'code': self.exit_code,
                          'stderr': '\n'.join(self.stderr_lines[-8:])})

    # ---- lifecycle ----
    def start(self, resume_session_id=None):
        cmd = [self.kimi_bin]
        if self.model:
            cmd += ['--model', self.model]
        cmd.append('acp')
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            bufsize=1, cwd=self.cwd, creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._readLoop, daemon=True).start()
        threading.Thread(target=self._readStderr, daemon=True).start()
        init = self._request('initialize', {
            'protocolVersion': PROTOCOL_VERSION,
            'clientCapabilities': {},
            'clientInfo': {'name': 'deskport-agent-node', 'version': '1.0'},
        }, timeout=20)
        self.agent_info = init.get('agentInfo') or {}
        if resume_session_id:
            self._loading = True
            try:
                self._request('session/load', {
                    'sessionId': resume_session_id, 'cwd': self.cwd,
                    'mcpServers': [], 'additionalDirectories': [],
                }, timeout=90)
            finally:
                self._loading = False
            self.kimi_session_id = resume_session_id
        else:
            session = self._request('session/new', {
                'cwd': self.cwd, 'mcpServers': [], 'additionalDirectories': [],
            }, timeout=30)
            self.kimi_session_id = session.get('sessionId')
            if not self.kimi_session_id:
                raise RuntimeError('session/new 未返回 sessionId')
        if self.mode:
            try:
                self._request('session/set_mode', {
                    'sessionId': self.kimi_session_id, 'modeId': self.mode}, timeout=10)
            except RuntimeError:
                pass   # 模式不被支持时按 agent 默认行为
        threading.Thread(target=self._turnLoop, daemon=True).start()
        self.ring.append({'type': 'ready', 'kimiSessionId': self.kimi_session_id,
                          'agentInfo': self.agent_info})
        return self.kimi_session_id

    # ---- turns ----
    def _turnLoop(self):
        while not self.dead:
            item = None
            with self._lock:
                if self._turnQueue:
                    item = self._turnQueue.pop(0)
            if item is None:
                time.sleep(0.2)
                continue
            text, doneEvent, resultBox = item
            self.running = True
            self.last_active = nowMs()
            try:
                result = self._request('session/prompt', {
                    'sessionId': self.kimi_session_id,
                    'prompt': [{'type': 'text', 'text': text}]}, timeout=0)
            except Exception as e:
                result = {'stopReason': 'error', 'error': str(e)[:500]}
            self.running = False
            self.last_active = nowMs()
            self.ring.append({'type': 'turn_end',
                              'stopReason': (result or {}).get('stopReason') or '',
                              'error': (result or {}).get('error') or ''})
            resultBox['result'] = result
            if doneEvent:
                doneEvent.set()

    def enqueuePrompt(self, text):
        """非阻塞派活；返回 (doneEvent, resultBox) 或 None（忙）。"""
        with self._lock:
            if self._turnQueue or self.running:
                return None
            doneEvent = threading.Event()
            resultBox = {}
            self._turnQueue.append((text, doneEvent, resultBox))
        return doneEvent, resultBox

    def submitDecision(self, acpRequestId, optionId):
        with self._lock:
            decision = self._decisions.get(acpRequestId)
        if not decision:
            return False
        decision['value'] = optionId or ''
        if not optionId:
            decision['closed'] = True
        decision['event'].set()
        return True

    def cancelTurn(self):
        try:
            self._send({'jsonrpc': '2.0', 'method': 'session/cancel',
                        'params': {'sessionId': self.kimi_session_id}})
        except Exception:
            pass

    def setMode(self, mode):
        self.mode = mode
        return self._request('session/set_mode',
                             {'sessionId': self.kimi_session_id, 'modeId': mode},
                             timeout=10)

    def stop(self):
        self.dead = True
        try:
            self.cancelTurn()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

    def meta(self):
        return {'sid': self.sid, 'cwd': self.cwd, 'mode': self.mode,
                'title': self.title, 'model': self.model,
                'kimiSessionId': self.kimi_session_id,
                'createdAt': self.created_at, 'running': self.running,
                'dead': self.dead, 'exitCode': self.exit_code,
                'seq': self.ring.seq(), 'lastActive': self.last_active}
