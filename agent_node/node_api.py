# -*- coding: utf-8 -*-
"""Agent Node HTTP 层：SessionManager（会话存档）+ HTTP Handler（路由/认证）。"""
import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from acp_session import AcpSession, IDLE_KILL_SECONDS, PROTOCOL_VERSION, nowMs, \
    discoverModels, forkKimiSession, listKimiSessions

_MANAGER = None
_TOKEN = ''
_STARTED = nowMs()


def init(manager, token):
    global _MANAGER, _TOKEN, _STARTED
    _MANAGER = manager
    _TOKEN = token
    _STARTED = nowMs()


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

    def create(self, cwd, mode, title, resume_session_id=None, model='',
               kimi_session_id='', fork=True):
        if resume_session_id:
            with self._lock:
                for session in self._sessions.values():
                    if isinstance(session, AcpSession) \
                            and session.kimi_session_id == resume_session_id \
                            and not session.dead:
                        # 该会话进程仍存活：直接复用（多端接手同一会话）
                        session.last_active = nowMs()
                        return session
        if kimi_session_id:
            # R4-7 远端接手：先 fork 副本（避免与原进程并发写同一会话），
            # 再对副本做 session/load（回放走 _loading 门禁）
            resume_session_id = forkKimiSession(self.kimi_bin, kimi_session_id)
        sid = uuid.uuid4().hex[:12]
        session = AcpSession(sid, cwd, mode, self.kimi_bin, title, model)
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

    def deleteByAny(self, sidOrKimiId):
        """按 node sid 或 kimiSessionId 删除并终止进程。

        web 重启后任务卡上只剩 kimiSessionId（agent 内存态已清）——close
        释放会话锁必须能用它找到会话（R4-5 余项 A）。stop 在 mgr 锁外执行。"""
        with self._lock:
            if sidOrKimiId in self._sessions:
                target = sidOrKimiId
            else:
                target = None
                for s in self._sessions.values():
                    kid = s.get('kimiSessionId') if isinstance(s, dict) \
                        else getattr(s, 'kimi_session_id', None)
                    if kid == sidOrKimiId:
                        target = s.sid if hasattr(s, 'sid') else s.get('sid')
                        break
        if not target:
            return False
        return self.delete(target)

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
            models = discoverModels()
            return self._json(200, {'ok': True, 'service': 'deskport-agent-node',
                                    'protocolVersion': PROTOCOL_VERSION,
                                    'sessions': len(sessions), 'alive': alive,
                                    'uptime': nowMs() - _STARTED,
                                    'models': models['models'],
                                    'defaultModel': models['default']})
        if not self._authed():
            return self._json(401, {'error': '认证失败'})
        if path == '/api/kimi-sessions':
            # R4-7：远端接手候选列表（kimi session list + cwd 过滤）
            qs = parse_qs(urlparse(self.path).query)
            cwd = (qs.get('cwd') or [''])[0].strip()
            try:
                limit = int((qs.get('limit') or ['30'])[0])
            except ValueError:
                limit = 30
            try:
                sessions = listKimiSessions(_MANAGER.kimi_bin,
                                            cwd=cwd or None, limit=limit)
            except RuntimeError as e:
                return self._json(502, {'error': str(e)})
            except subprocess.TimeoutExpired:
                return self._json(502, {'error': 'session list 超时'})
            return self._json(200, {'sessions': sessions})
        if path == '/api/sessions':
            return self._json(200, {'sessions': _MANAGER.list()})
        if path.startswith('/api/sessions/'):
            sid = self._sid()
            session = _MANAGER.get(sid)
            if not session:
                return self._json(404, {'error': '会话不存在'})
            if path.endswith('/events/stream'):
                return self._streamEvents(session, self._querySince())
            if path.endswith('/events'):
                ring = session.ring
                return self._json(200, {'events': ring.since(self._querySince()),
                                        'seq': ring.seq(),
                                        'running': session.running,
                                        'dead': session.dead})
            return self._json(200, {'session': session.meta()})
        return self._json(404, {'error': 'not found'})

    def _querySince(self):
        qs = parse_qs(urlparse(self.path).query)
        try:
            return int((qs.get('since') or ['0'])[0])
        except ValueError:
            return 0

    def _streamEvents(self, session, since):
        """SSE：一条连接持续推送事件；25s 无事件发心跳，断开即退出。

        事件留在 ring 里，客户端重连带 since 即补差量。
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        ring = session.ring
        try:
            while True:
                events, latest = ring.waitSince(since, timeout=25)
                for event in events:
                    since = max(since, event.get('seq', 0))
                    self.wfile.write(('data: %s\n\n' % json.dumps(
                        event, ensure_ascii=False)).encode('utf-8'))
                if session.dead and latest == since:
                    self.wfile.write(b'event: end\n\n')
                    self.wfile.flush()
                    return
                self.wfile.write(b': keepalive\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

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
            model = str(data.get('model') or '').strip()
            resume = str(data.get('sessionId') or '').strip() or None
            kimiSessionId = str(data.get('kimiSessionId') or '').strip()
            fork = data.get('fork', True)
            fork = fork is not False and str(fork).lower() != 'false'
            try:
                session = _MANAGER.create(cwd, mode, title, resume, model,
                                          kimi_session_id=kimiSessionId,
                                          fork=fork)
            except RuntimeError as e:
                # fork 失败（会话被占用/不存在）按 400 透传原文（R4-7）
                return self._json(400, {'error': str(e)})
            except Exception as e:
                return self._json(502, {'error': 'ACP 启动失败: %s' % e})
            return self._json(200, {'sid': session.sid,
                                    'kimiSessionId': session.kimi_session_id,
                                    'attachSeq': session.ring.seq(),
                                    'replayCount': getattr(session, 'replayCount', 0)})
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
            ok = _MANAGER.deleteByAny(self._sid())
            return self._json(200 if ok else 404, {'ok': ok})
        return self._json(404, {'error': 'not found'})
