#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deskport Agent Node — 在远端主机托管 kimi acp 会话，暴露 HTTP API。

纯标准库实现（零第三方依赖），拷到目标机即可运行：
    python3 agent_node.py --port 8622 --token <TOKEN>

认证：除 /health 外全部要求请求头 X-Agent-Token 与 --token 一致。
会话模型：一个逻辑会话 = 一个 `kimi acp` 子进程；进程可因空闲被回收，
kimiSessionId 持久化在 sessions.json，随时可用 session/load 接续。
事件模型：内存环形缓冲（seq 单调递增），客户端用 since=<seq> 增量拉取。
"""
import argparse
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PROTOCOL_VERSION = 1
MAX_EVENTS = 5000
PERMISSION_TIMEOUT = 900          # node 端兜底；Deskport 端 300s 先到
IDLE_KILL_SECONDS = 45 * 60       # 空闲进程回收；会话可 resume，不丢上下文
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def nowMs():
    return int(time.time() * 1000)


class Ring:
    """seq 单调递增的事件环形缓冲；客户端按 since 增量拉取。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []
        self._seq = 0

    def append(self, record):
        with self._lock:
            self._seq += 1
            record = dict(record)
            record['seq'] = self._seq
            record['at'] = nowMs()
            self._items.append(record)
            if len(self._items) > MAX_EVENTS:
                del self._items[:len(self._items) - MAX_EVENTS]
            return self._seq

    def seq(self):
        with self._lock:
            return self._seq

    def since(self, n):
        with self._lock:
            return [e for e in self._items if e.get('seq', 0) > n]


class AcpSession:
    """托管一个 kimi acp 子进程；update 透传进 ring，权限请求挂起等决策。"""

    def __init__(self, sid, cwd, mode, kimi_bin, title=''):
        self.sid = sid
        self.cwd = cwd
        self.mode = mode
        self.kimi_bin = kimi_bin
        self.title = (title or '')[:60]
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
        self._turnLock = threading.Lock()
        self._decisions = {}      # acpRequestId -> {'value': optionId|None, 'event': Event}
        self._turnQueue = []
        self._turnWorker = None

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
            self.ring.append({'type': 'update', 'replay': getattr(self, '_loading', False),
                              'update': params.get('update') or {}})
        elif method == 'session/request_permission':
            self._handlePermission(params, obj.get('id'))
        elif method:
            self._send({'jsonrpc': '2.0', 'id': obj.get('id'),
                        'error': {'code': -32601, 'message': 'method not found'}})

    def _handlePermission(self, params, acpReqId):
        requestId = str(acpReqId)
        options = [{'optionId': o.get('optionId') or '', 'name': o.get('name') or '',
                    'kind': o.get('kind') or ''}
                   for o in (params.get('options') or []) if isinstance(o, dict)]
        self.ring.append({'type': 'permission_request', 'requestId': requestId,
                          'title': (params.get('toolCall') or {}).get('title') or '工具调用',
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
        self.proc = subprocess.Popen(
            [self.kimi_bin, 'acp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
            with self._lock:
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
        self._turnWorker = threading.Thread(target=self._turnLoop, daemon=True)
        self._turnWorker.start()
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
        """非阻塞派活；返回是否受理（同一会话同时只跑一轮）。"""
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
                'title': self.title, 'kimiSessionId': self.kimi_session_id,
                'createdAt': self.created_at, 'running': self.running,
                'dead': self.dead, 'exitCode': self.exit_code,
                'seq': self.ring.seq(), 'lastActive': self.last_active}


class SessionManager:
    def __init__(self, kimi_bin, data_dir):
        self.kimi_bin = kimi_bin
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._sessions = {}
        self._load()

    def _storePath(self):
        return os.path.join(self.data_dir, 'sessions.json')

    def _load(self):
        try:
            with open(self._storePath(), 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get('kimiSessionId'):
                continue
            sid = str(item.get('sid') or uuid.uuid4().hex[:12])
            meta = {'sid': sid, 'cwd': item.get('cwd') or os.path.expanduser('~'),
                    'mode': item.get('mode') or 'auto', 'title': item.get('title') or '',
                    'kimiSessionId': item.get('kimiSessionId'),
                    'createdAt': item.get('createdAt') or nowMs(),
                    'running': False, 'dead': True, 'exitCode': None,
                    'seq': 0, 'lastActive': item.get('lastActive') or nowMs(),
                    'archived': True}
            with self._lock:
                self._sessions[sid] = meta

    def _save(self):
        with self._lock:
            items = [s.meta() if isinstance(s, AcpSession) else dict(s)
                     for s in self._sessions.values()]
        tmp = self._storePath() + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._storePath())
        except OSError:
            pass

    def create(self, cwd, mode, title, resume_session_id=None):
        if resume_session_id:
            with self._lock:
                for session in self._sessions.values():
                    if isinstance(session, AcpSession) \
                            and session.kimi_session_id == resume_session_id \
                            and not session.dead:
                        # 该会话进程仍存活：直接复用（多端接手同一会话）
                        session.last_active = nowMs()
                        return session
        sid = uuid.uuid4().hex[:12]
        session = AcpSession(sid, cwd, mode, self.kimi_bin, title)
        session.start(resume_session_id)
        with self._lock:
            self._sessions[sid] = session
        self._save()
        return session

    def get(self, sid):
        with self._lock:
            return self._sessions.get(sid)

    def list(self):
        with self._lock:
            items = [s.meta() if isinstance(s, AcpSession) else dict(s)
                     for s in self._sessions.values()]
        return sorted(items, key=lambda x: -x.get('createdAt', 0))

    def delete(self, sid):
        with self._lock:
            session = self._sessions.pop(sid, None)
        if isinstance(session, AcpSession):
            session.stop()
        self._save()
        return session is not None

    def reapIdle(self):
        cutoff = nowMs() - IDLE_KILL_SECONDS * 1000   # nowMs 是毫秒
        with self._lock:
            targets = [s for s in self._sessions.values()
                       if isinstance(s, AcpSession) and not s.dead
                       and not s.running and s.last_active < cutoff]
        for s in targets:
            s.stop()
            s.ring.append({'type': 'notice', 'text': '空闲超时，进程已回收（会话可 resume）'})
        if targets:
            self._save()

    def shutdown(self):
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            if isinstance(s, AcpSession):
                s.stop()
        self._save()


_MANAGER = None
_TOKEN = ''


class Handler(BaseHTTPRequestHandler):
    server_version = 'DeskportAgentNode/1.0'

    def log_message(self, fmt, *args):
        sys.stdout.write('[%s] %s\n' % (time.strftime('%m-%d %H:%M:%S'),
                                        fmt % args))

    # ---- helpers ----
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not _TOKEN:
            return True
        got = self.headers.get('X-Agent-Token') or ''
        return hmac.compare_digest(got, _TOKEN)

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length > 2 * 1024 * 1024:
                return None
            return json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        except (ValueError, UnicodeDecodeError):
            return None

    def _sid(self):
        parts = urlparse(self.path).path.split('/')
        return parts[3] if len(parts) > 3 else ''

    # ---- routing ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            sessions = _MANAGER.list()
            alive = sum(1 for s in sessions if isinstance(s.get('dead'), bool) and not s['dead'])
            return self._json(200, {'ok': True, 'service': 'deskport-agent-node',
                                    'protocolVersion': PROTOCOL_VERSION,
                                    'sessions': len(sessions), 'alive': alive,
                                    'uptime': nowMs() - _STARTED})
        if not self._authed():
            return self._json(401, {'error': '认证失败'})
        if path == '/api/sessions':
            return self._json(200, {'sessions': _MANAGER.list()})
        if path.startswith('/api/sessions/'):
            sid = self._sid()
            session = _MANAGER.get(sid)
            if not session:
                return self._json(404, {'error': '会话不存在'})
            if path.endswith('/events'):
                qs = parse_qs(urlparse(self.path).query)
                try:
                    since = int((qs.get('since') or ['0'])[0])
                except ValueError:
                    since = 0
                ring = session.ring
                return self._json(200, {'events': ring.since(since),
                                        'seq': ring.seq(),
                                        'running': session.running,
                                        'dead': session.dead})
            return self._json(200, {'session': session.meta()})
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        if not self._authed():
            return self._json(401, {'error': '认证失败'})
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._json(400, {'error': '请求体不是合法 JSON'})
        if path == '/api/sessions':
            cwd = str(data.get('cwd') or '').strip() or os.path.expanduser('~')
            mode = str(data.get('mode') or 'auto').strip()
            title = str(data.get('title') or '').strip()
            resume = str(data.get('sessionId') or '').strip() or None
            try:
                session = _MANAGER.create(cwd, mode, title, resume)
            except Exception as e:
                return self._json(502, {'error': 'ACP 启动失败: %s' % e})
            return self._json(200, {'sid': session.sid,
                                    'kimiSessionId': session.kimi_session_id})
        sid = self._sid()
        session = _MANAGER.get(sid)
        if not session:
            return self._json(404, {'error': '会话不存在'})
        if isinstance(session, dict):
            return self._json(409, {'error': '会话已归档，请用 sessionId 新建并 resume'})
        if path.endswith('/prompt'):
            if session.dead:
                return self._json(410, {'error': '进程已回收，请用 sessionId 重新接续'})
            text = str(data.get('text') or '').strip()
            if not text:
                return self._json(400, {'error': 'text 不能为空'})
            accepted = session.enqueuePrompt(text)
            if accepted is None:
                return self._json(409, {'error': '会话正在执行，请稍候'})
            return self._json(202, {'accepted': True})
        if path.endswith('/permission'):
            requestId = str(data.get('requestId') or '')
            optionId = str(data.get('optionId') or '')
            try:
                acpReqId = int(requestId)
            except ValueError:
                return self._json(400, {'error': 'requestId 不合法'})
            ok = session.submitDecision(acpReqId, optionId)
            return self._json(200 if ok else 404, {'ok': ok})
        if path.endswith('/cancel'):
            session.cancelTurn()
            return self._json(200, {'ok': True})
        if path.endswith('/mode'):
            mode = str(data.get('mode') or '').strip()
            if not mode:
                return self._json(400, {'error': 'mode 不能为空'})
            try:
                session.setMode(mode)
            except RuntimeError as e:
                return self._json(502, {'error': str(e)})
            return self._json(200, {'ok': True, 'mode': mode})
        return self._json(404, {'error': 'not found'})

    def do_DELETE(self):
        if not self._authed():
            return self._json(401, {'error': '认证失败'})
        if urlparse(self.path).path.startswith('/api/sessions/'):
            ok = _MANAGER.delete(self._sid())
            return self._json(200 if ok else 404, {'ok': ok})
        return self._json(404, {'error': 'not found'})


_STARTED = nowMs()


def main():
    global _MANAGER, _TOKEN
    parser = argparse.ArgumentParser(description='Deskport Agent Node')
    parser.add_argument('--port', type=int, default=8622)
    parser.add_argument('--token', default=os.environ.get('AGENT_NODE_TOKEN', ''))
    parser.add_argument('--kimi', default=os.environ.get('KIMI_BIN') or shutil.which('kimi'))
    parser.add_argument('--data', default=os.path.join(os.path.expanduser('~'), '.deskport-agent-node'))
    args = parser.parse_args()
    if not args.kimi:
        print('找不到 kimi 可执行文件，请用 --kimi 指定', file=sys.stderr)
        sys.exit(2)
    os.makedirs(args.data, exist_ok=True)
    _TOKEN = args.token
    _MANAGER = SessionManager(args.kimi, args.data)

    def reap():
        while True:
            time.sleep(60)
            try:
                _MANAGER.reapIdle()
            except Exception:
                pass
    threading.Thread(target=reap, daemon=True).start()

    def onTerm(signum, frame):
        _MANAGER.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, onTerm)
    signal.signal(signal.SIGINT, onTerm)

    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.daemon_threads = True
    print('[agent-node] listening on 0.0.0.0:%d kimi=%s data=%s'
          % (args.port, args.kimi, args.data), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
