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
# 权限挂起不限时（反馈 R4-2）：由 Deskport 端按任务 timeoutSec 决定何时
# 提交/放弃决策；node 只在会话被删除/停止时把未决决策置为 closed，
# 避免 _handlePermission 线程随死会话泄漏
IDLE_KILL_SECONDS = 45 * 60       # 空闲进程回收；会话可 resume，不丢上下文
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
KIMI_CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.kimi-code',
                                'config.toml')


def discoverModels(configPath=None):
    """从 kimi CLI 的 config.toml 枚举可选模型（反馈 §7.2 自动发现）。

    依赖 Python ≥ 3.11 的 tomllib（agent_node 入口有版本守卫，不做旧版兼容）。
    返回 {models: [别名...], default: 别名}；文件缺失/损坏返回空。
    """
    path = configPath or KIMI_CONFIG_PATH
    try:
        import tomllib
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {'models': [], 'default': ''}
    raw = data.get('models')
    models = sorted(str(k) for k in raw.keys()) if isinstance(raw, dict) else []
    return {'models': models, 'default': str(data.get('default_model') or '')}


def nowMs():
    return int(time.time() * 1000)


def forkKimiSession(kimi_bin, sessionId, cwd=None, timeout=600):
    """kimi fork：复制已有会话并返回新 id（R4-7；532M 会话实测秒级）。

    kimi 按工作目录归属会话：cwd 缺省继承 node 进程目录，目录不属于
    会话时报 Session not found（远端实测）——调用方传 kimiSessionWorkDir
    查到的会话 workDir。失败（会话被占用/不存在/超时）抛 RuntimeError，
    原文透传给调用方。
    """
    proc = subprocess.run(
        [kimi_bin, 'fork', '-y', sessionId], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=timeout,
        cwd=cwd or None, creationflags=0x08000000 if os.name == 'nt' else 0)
    out = (proc.stdout or '') + (proc.stderr or '')
    if proc.returncode != 0:
        raise RuntimeError('fork 失败: %s' % (out.strip()[:300] or proc.returncode))
    m = re.search(r'session_[0-9a-f-]{36}', out)
    if not m:
        raise RuntimeError('fork 输出里没有新会话 id: %s' % out.strip()[:200])
    return m.group(0)


def listKimiSessions(kimi_bin, cwd=None, limit=100):
    """kimi session list --all --json → 接手候选（cwd 前缀过滤 + 最近优先）。"""
    proc = subprocess.run(
        [kimi_bin, 'session', 'list', '--all', '--json'], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=120,
        creationflags=0x08000000 if os.name == 'nt' else 0)
    if proc.returncode != 0:
        raise RuntimeError('session list 失败: %s' % (proc.stderr or '').strip()[:300])
    try:
        raw = json.loads(proc.stdout or '[]')
    except ValueError:
        raise RuntimeError('session list 输出不是 JSON')
    wanted = str(cwd or '').strip()
    if wanted:
        wanted = wanted.replace('/', '\\').rstrip('\\').lower()
    items = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not entry.get('id'):
            continue
        workDir = str(entry.get('workDir') or '')
        if wanted:
            norm = workDir.replace('/', '\\').rstrip('\\').lower()
            if norm != wanted and not norm.startswith(wanted + '\\'):
                continue
        items.append({'id': str(entry['id']),
                      'title': str(entry.get('title') or '')[:120],
                      'lastPrompt': str(entry.get('lastPrompt') or '')[:160],
                      'workDir': workDir,
                      'updatedAt': entry.get('updatedAt') or 0})
    items.sort(key=lambda x: -(x['updatedAt'] or 0))
    return items[:max(1, min(int(limit), 100))]


def kimiSessionWorkDir(kimi_bin, kimi_session_id):
    """kimi 存储里指定会话的 workDir；查不到返回 ''。

    kimi 按工作目录归属会话：fork 与 session/load 都只认会话所属目录
    （node 进程目录下直接操作报 Session not found / Unknown sessionId，
    远端实测），接手外部会话前必须先查归属目录。kimi 不可用抛
    RuntimeError，由调用方决定兜底。
    """
    proc = subprocess.run(
        [kimi_bin, 'session', 'list', '--all', '--json'], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=120,
        creationflags=0x08000000 if os.name == 'nt' else 0)
    if proc.returncode != 0:
        raise RuntimeError('session list 失败: %s' % (proc.stderr or '').strip()[:300])
    try:
        raw = json.loads(proc.stdout or '[]')
    except ValueError:
        raise RuntimeError('session list 输出不是 JSON')
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) \
                and str(entry.get('id') or '') == kimi_session_id:
            return str(entry.get('workDir') or '')
    return ''


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
        self.replayCount = 0      # session/load 回放的 update 条数（R4-5a）
        self.replayPerms = 0      # 回放中被直接取消的旧审批条数
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
        if self._loading:
            # session/load 回放里的旧审批不能成为 live 挂起（R4-5a #2）：
            # 直接取消，不落 ring、不计数为 live
            self.replayPerms += 1
            self._send({'jsonrpc': '2.0', 'id': acpReqId,
                        'result': {'outcome': {'outcome': 'cancelled'}}})
            return
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
        decision = {'value': None, 'closed': False, 'event': threading.Event()}
        with self._lock:
            self._decisions[acpReqId] = decision
        decision['event'].wait()   # 不限时：Deskport 端按任务 timeoutSec 决断
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
            # 回放结束 = session/load 返回（R4-5a #4）；统计被门禁挡下的
            # 回放 update 条数，供 web 侧写「已加载历史 N 条」
            self.replayCount = sum(1 for e in self.ring.since(0)
                                   if e.get('type') == 'update' and e.get('replay'))
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
        """硬终止 kimi 进程。绝不 wait：Windows 上进程带未决管道 I/O 时
        TerminateProcess/wait 可能长时间不完成（实测曾把 mgr 锁一起
        wedge 住，全部请求挂死）——发起终止即返回，锁由进程死亡释放。"""
        self.dead = True
        # 释放所有挂起的权限等待线程（closed 语义 → denied）
        with self._lock:
            pending = list(self._decisions.values())
        for decision in pending:
            decision['closed'] = True
            decision['event'].set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
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
