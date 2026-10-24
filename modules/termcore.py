# -*- coding: utf-8 -*-
"""终端会话核心 — IO 后端抽象 + 会话注册表 + 回收线程

本地 ConPTY 与 SSH 远程会话共用本模块。硬约束（实测教训，不可违背）：
    - spawn/read 必须同线程（ConPTY 线程亲和；SSH channel 亦建议）
    - ws.send 全部收敛到附着主循环线程
    - pywinpty 的空串返回是内部忽略帧，不是 EOF
    - handler 退出时 seal 阻断 werkzeug teardown 补写

IO 后端接口（duck typing）：
    read()                -> None(暂无数据) | str/bytes(输出) | 抛 EOFError(结束)
    write(data)           -> 写入
    resize(rows, cols)    -> 调整尺寸
    close()               -> 终止
"""
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from collections import deque

import psutil

import config
import modules.internal_api as internal_api
from modules import contracts

log = logging.getLogger('termcore')

# ---- 会话保活参数 ----
# 无人附着宽限期由 config.json 的 terminalIdleTimeoutHours 控制:
#   缺省 0 = 永不回收 (终端/SSH 会话像 tmux 一样常驻, 直到进程自己退出或手动关闭)
REAPER_INTERVAL = 10     # 回收线程扫描周期(秒)
MAX_BACKLOG = 20000      # 无人附着时输出队列上限(条)
REPLAY_MAX = 512 * 1024  # 回放缓冲上限


class ConPTYBackend:
    """Windows ConPTY 后端 (pywinpty)。read 返回 str; 关闭时抛 EOFError。"""

    def __init__(self, cmd, cwd):
        self.cmd = cmd
        self.cwd = cwd
        self.proc = None

    def spawn(self):
        from winpty import PtyProcess
        self.proc = PtyProcess.spawn(
            self.cmd, cwd=self.cwd or os.path.expanduser('~'),
            env={**os.environ, 'TERM': 'xterm-256color'},
            dimensions=(24, 80))

    def read(self):
        try:
            data = self.proc.read()
        except (EOFError, OSError):
            raise EOFError
        # pywinpty 内部哨兵帧转成的空串 = 忽略帧, 不是 EOF
        if isinstance(data, str) and data == '':
            return None
        return data

    def write(self, data):
        # Windows 控制台约定: 回车 CR; 粘贴里的 LF 统一转 CR
        if isinstance(data, bytes):
            data = data.decode('utf-8', 'replace')
        self.proc.write(data.replace('\r\n', '\r').replace('\n', '\r'))

    def resize(self, rows, cols):
        # pwsh 7.6.6 在 ConPTY 收到 1x1 这类退化尺寸会卡死 (曾表现为 totalWidth 负值 FailFast)。
        # 浏览器初次 fit 时容器可能还是 0 宽, 必须在这里设下限。
        try:
            rows = max(5, int(rows))
            cols = max(20, int(cols))
        except (TypeError, ValueError):
            return
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:
            pass

    @property
    def exitcode(self):
        return getattr(self.proc, 'exitcode', None)

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.close()
        except Exception:
            pass


class TermSession:
    """终端会话: 与 WS 连接解耦, 进程在 WS 断开后继续存活。单附着, 抢占式接管。"""

    def __init__(self, sessionID, kind, label, backend, cwd=None, hostId=None):
        self.sessionID = sessionID
        self.kind = kind              # 'local' | 'ssh'
        self.label = label            # 侧栏显示 (本地=cmd, SSH=user@host)
        self.cwd = cwd
        self.hostId = hostId          # SSH 主机 id; 本地会话为 None
        self.backend = backend        # reader 线程内调用 spawn/read
        self.proc = None              # 兼容字段: ConPTY 时指向 backend.proc
        self.alive = False
        self.attached = None
        self.sessionLock = threading.Lock()
        self.outQueue = queue.Queue()
        self.stop = threading.Event()
        self.spawned = threading.Event()
        self.lastDetachAt = time.time()
        self.replayBuf = deque()
        self.replaySize = 0

    def __repr__(self):
        return '<TermSession %s %s %r>' % (self.sessionID[:8], self.kind, self.label)

    def enqueueOut(self, item):
        """输出入队 + 队列封顶 + 回放缓冲维护 (仅 reader 线程调用)。"""
        kind, payload = item
        if kind == 'data':
            self.replayBuf.append(payload)
            self.replaySize += len(payload)
            while self.replaySize > REPLAY_MAX and self.replayBuf:
                removed = self.replayBuf.popleft()
                self.replaySize -= len(removed)
        self.outQueue.put(item)
        while self.outQueue.qsize() > MAX_BACKLOG:
            try:
                self.outQueue.get_nowait()
            except queue.Empty:
                break


class SessionRegistry:
    """sessionID -> TermSession；只负责并发安全的注册/查找/移除。"""

    def __init__(self):
        self.map = {}
        self.lock = threading.Lock()

    def add(self, session):
        with self.lock:
            self.map[session.sessionID] = session

    def get(self, sessionID):
        with self.lock:
            return self.map.get(sessionID)

    def remove(self, session):
        with self.lock:
            self.map.pop(session.sessionID, None)

    def values(self):
        with self.lock:
            return list(self.map.values())


registry = SessionRegistry()
reaperStarted = False
reaperLock = threading.Lock()


def newSession(session):
    """注册会话并启动 reader 线程 (backend.spawn 在 reader 线程内调用)。"""
    registry.add(session)
    _startReaper()
    threading.Thread(target=_reader, args=(session,), daemon=True).start()
    return session


def getSession(sessionID):
    return registry.get(sessionID)


def _removeSession(session):
    registry.remove(session)


def _startReaper():
    global reaperStarted
    with reaperLock:
        if reaperStarted:
            return
        reaperStarted = True
    threading.Thread(target=_sessionReaper, daemon=True).start()


def terminateSession(session):
    session.stop.set()
    try:
        session.backend.close()
    except Exception:
        pass
    session.alive = False
    _removeSession(session)
    log.info('terminate session=%s label=%s', session.sessionID, session.label)


def _reader(session):
    """后台线程: spawn 后端并持续读取输出入队; 不直接调用 ws.send。"""
    try:
        session.backend.spawn()
    except Exception as e:
        session.alive = False
        _removeSession(session)
        session.enqueueOut(('error', str(e)))
        log.warning('spawn failed session=%s err=%s', session.sessionID, e)
        return
    session.alive = True
    session.spawned.set()
    log.info('spawn ok session=%s label=%s', session.sessionID, session.label)

    exitCode = None
    while not session.stop.is_set():
        try:
            data = session.backend.read()
        except EOFError:
            break
        except Exception:
            break
        if data is None:
            continue
        if isinstance(data, str) and data == '':
            continue
        if isinstance(data, bytes) and not data:
            exitCode = session.backend.exitcode
            break
        session.enqueueOut(('data', data))

    exitCode = exitCode if exitCode is not None else session.backend.exitcode
    session.alive = False
    session.enqueueOut(('exit', exitCode))
    _removeSession(session)
    log.info('process exited session=%s code=%s', session.sessionID, exitCode)


def _sessionReaper():
    while True:
        time.sleep(REAPER_INTERVAL)
        timeout = config.getIdleTimeoutSeconds()
        if timeout <= 0:
            continue   # 0 = 永不回收; 每轮重读配置, 改 config.json 即时生效
        now = time.time()
        snapshot = registry.values()
        for session in snapshot:
            with session.sessionLock:
                idle = (session.alive
                        and session.attached is None
                        and (now - session.lastDetachAt) > timeout)
            if idle:
                log.info('reap session=%s idle_timeout', session.sessionID)
                terminateSession(session)


def _cwdLabelFor(path):
    """路径短名: 家目录返回空, 其余取末级目录 (盘根返回盘符)。"""
    if not path:
        return ''
    try:
        path = os.path.abspath(path)
        home = os.path.abspath(os.path.expanduser('~'))
        if os.path.normcase(path) == os.path.normcase(home):
            return ''
        base = os.path.basename(path.rstrip('\\/'))
        return base or path
    except (OSError, ValueError):
        return ''


def _cwdLabel(session):
    return _cwdLabelFor(getattr(session, 'cwd', None))


def _foregroundInfo(session):
    """本地会话前台子进程: {name, cwd}; 空闲或 SSH 返回空字典。

    cwd 用于把"kimi/omp 在哪个项目里跑"显示到会话名上, 比 shell 名有意义。
    """
    if session.kind != 'local':
        return {}
    backend = getattr(session, 'backend', None)
    proc = getattr(backend, 'proc', None)
    if proc is None:
        return {}
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except (psutil.Error, OSError):
        return {}
    if not children:
        return {}

    def _ctime(p):
        try:
            return p.create_time()
        except (psutil.Error, OSError):
            return 0.0

    children.sort(key=_ctime)
    for child in reversed(children):
        try:
            name = child.name()
        except (psutil.Error, OSError):
            continue
        if not name or name.lower() in ('conhost.exe', 'openconsole.exe'):
            continue
        try:
            cwd = child.cwd()
        except (psutil.Error, OSError):
            cwd = ''
        return {'name': name, 'cwd': cwd or ''}
    return {}


def listSessions(kind=None):
    """存活会话列表; kind='local'/'ssh' 过滤, None 返回全部。"""
    snapshot = registry.values()
    items = []
    for session in snapshot:
        with session.sessionLock:
            alive = session.alive
            attached = (session.attached is not None)
        if not alive:
            continue
        if kind and session.kind != kind:
            continue
        hostId = session.hostId
        fg = _foregroundInfo(session)
        items.append({'id': session.sessionID, 'kind': session.kind,
                      'cmd': session.label, 'label': session.label,
                      'hostId': hostId, 'attached': attached,
                      'cwd': getattr(session, 'cwd', None),
                      'cwdLabel': _cwdLabel(session),
                      'foreground': fg.get('name', ''),
                      'foregroundCwd': fg.get('cwd', ''),
                      'foregroundCwdLabel': _cwdLabelFor(fg.get('cwd', ''))})
    return items


def sealSocketAfterClose(ws):
    """发 close 帧并封锁写方向, 阻断 werkzeug teardown 补写 HTTP 200。"""
    try:
        if getattr(ws, 'connected', False):
            try:
                ws.close()
            except Exception:
                pass
        sock_ = getattr(ws, 'sock', None)
        if sock_ is not None:
            try:
                sock_.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    except Exception:
        pass


def newSessionID():
    return uuid.uuid4().hex


def _detach(session, ws):
    with session.sessionLock:
        if session.attached is ws:
            session.attached = None
            session.lastDetachAt = time.time()


def _decodeFrame(msg):
    """区分控制帧(带 type 的 JSON)与原始输入。返回 (payload, isControl)。"""
    text = msg if isinstance(msg, str) else msg.decode('utf-8', errors='replace')
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and 'type' in obj:
            return obj, True
    except (ValueError, TypeError):
        pass
    return text, False


def _handleControl(ws, session, backend, payload):
    ctype = payload.get('type')
    if ctype == 'resize':
        try:
            backend.resize(int(payload.get('rows', 24)), int(payload.get('cols', 80)))
        except Exception:
            pass
    elif ctype == 'ping':
        try:
            ws.send(json.dumps({'type': 'pong'}))
        except Exception:
            pass
    elif ctype == 'close':
        terminateSession(session)


def _sendJson(ws, obj):
    try:
        ws.send(json.dumps(obj, ensure_ascii=False))
        return True
    except Exception:
        return False


def _sendText(ws, text):
    try:
        ws.send(text)
        return True
    except Exception:
        return False


def _closeWs(ws):
    try:
        ws.close()
    except Exception:
        pass


def _failAttach(ws, session, message):
    """附着失败: 发 error 帧、关闭、摘除附着、封锁写方向。"""
    _sendJson(ws, {'type': 'error', 'msg': message})
    _closeWs(ws)
    _detach(session, ws)
    sealSocketAfterClose(ws)


def _preempted(session, ws):
    """被其他连接抢占时通知旧客户端并返回 True。"""
    with session.sessionLock:
        if session.attached is ws:
            return False
    _sendJson(ws, {'type': 'preempted'})
    _closeWs(ws)
    return True


def _drainOutbox(ws, session):
    """排空输出队列; 返回 True 表示连接应退出 (error/exit/发送失败)。"""
    while True:
        try:
            item = session.outQueue.get_nowait()
        except queue.Empty:
            return False
        kind, payload = item
        if kind == 'error':
            _sendJson(ws, {'type': 'error', 'msg': '启动失败: ' + str(payload)})
            _closeWs(ws)
            return True
        if kind == 'exit':
            _sendJson(ws, {'type': 'exit', 'code': payload})
            _closeWs(ws)
            return True
        text = payload if isinstance(payload, str) else payload.decode(
            'utf-8', errors='replace')
        if not _sendText(ws, text):
            return True


def attachAndPump(ws, session, resumed=False, wantReplay=False):
    """附着会话并泵 IO: spawned 等待 → hello → 回放 → 输出转发 → 输入/控制帧。

    本地与 SSH 会话共用; 后端差异全部封装在 backend 接口内。
    """
    with session.sessionLock:
        session.attached = ws
    # 等 spawn 就绪; 期间 reader 可能入队 error 帧 (spawn 失败), 优先透传真实原因
    deadline = time.time() + 15
    while not session.spawned.is_set():
        try:
            item = session.outQueue.get(timeout=0.2)
        except queue.Empty:
            pass
        else:
            if item[0] == 'error':
                _failAttach(ws, session, '启动失败: ' + str(item[1]))
                return
        if time.time() >= deadline:
            break
    if not session.spawned.is_set():
        _failAttach(ws, session, '终端启动超时')
        return

    replaySnapshot = []
    if wantReplay:
        replaySnapshot = list(session.replayBuf)
        while True:
            try:
                session.outQueue.get_nowait()
            except queue.Empty:
                break

    fgInfo = _foregroundInfo(session)
    hello: contracts.HelloFrame = {
        'type': 'hello', 'session': session.sessionID,
        'resumed': resumed, 'label': session.label,
        'kind': session.kind,
        'cwd': getattr(session, 'cwd', None),
        'cwdLabel': _cwdLabel(session),
        'foreground': fgInfo.get('name', ''),
        'foregroundCwd': fgInfo.get('cwd', ''),
        'foregroundCwdLabel': _cwdLabelFor(fgInfo.get('cwd', '')),
        'proto': internal_api.PROTO,
        'caps': internal_api.CAPS,
    }
    _sendJson(ws, hello)
    for chunk in replaySnapshot:
        if not _sendText(ws, chunk if isinstance(chunk, str)
                         else chunk.decode('utf-8', errors='replace')):
            break
    if wantReplay:
        # 回放结束标记: 客户端收到前丢弃全部本地输入, 防止 xterm 对终端能力查询
        # (OSC 颜色/DA/XTGETTCAP) 的自动应答泄漏进 shell 输入行 (TERM-BUG-01)
        _sendJson(ws, {'type': 'replay-end'})

    try:
        while True:
            if _preempted(session, ws):
                break
            if _drainOutbox(ws, session):
                return
            # 带超时收帧: 轮询输出转发与输入处理
            try:
                msg = ws.receive(timeout=0.05)
            except Exception:
                break
            if msg is None:
                continue
            payload, isCtrl = _decodeFrame(msg)
            if isCtrl:
                _handleControl(ws, session, session.backend, payload)
                continue
            try:
                session.backend.write(payload)
            except Exception:
                break
    except Exception:
        pass
    finally:
        _detach(session, ws)
        sealSocketAfterClose(ws)
