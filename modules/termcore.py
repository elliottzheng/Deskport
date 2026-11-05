# -*- coding: utf-8 -*-
"""终端会话核心 — IO 后端抽象 + 会话注册表 + 生命周期

本地 ConPTY 与 SSH 远程会话共用本模块。这是**跨特性共享内核**（terminal 与
ssh 两个特性都依赖它），不是 terminal 的私有实现。

硬约束（实测教训，不可违背）：
    - spawn/read 必须同线程（ConPTY 线程亲和；SSH channel 亦建议）
    - ws.send 全部收敛到附着主循环线程（见 modules/wsproto.py）
    - pywinpty 的空串返回是内部忽略帧，不是 EOF
    - handler 退出时 seal 阻断 werkzeug teardown 补写（wsproto.sealSocketAfterClose）

IO 后端接口（duck typing）：
    read()                -> None(暂无数据) | str/bytes(输出) | 抛 EOFError(结束)
    write(data)           -> 写入
    resize(rows, cols)    -> 调整尺寸
    close()               -> 终止

拆分记录（2026-11-05）：原 568 行混了五个变化理由，已拆为
    modules/termprobe.py   Win32 前台进程/cwd 探测（纯查询）
    modules/wsproto.py     WS 帧协议与附着泵送
    modules/termcore.py    本文件：后端抽象、注册表、生命周期
"""
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque

import config
from modules import termprobe, winenv

log = logging.getLogger('termcore')

# ---- 会话保活参数 ----
# 无人附着宽限期由 config.json 的 terminalIdleTimeoutHours 控制:
#   缺省 0 = 永不回收 (终端/SSH 会话像 tmux 一样常驻, 直到进程自己退出或手动关闭)
REAPER_INTERVAL = 10     # 回收线程扫描周期(秒)
MAX_BACKLOG = 20000      # 无人附着时输出队列上限(条)
REPLAY_MAX = 512 * 1024  # 回放缓冲上限(服务端会话内scrollback)


class ConPTYBackend:
    """Windows ConPTY 后端 (pywinpty)。read 返回 str; 关闭时抛 EOFError。"""

    def __init__(self, cmd, cwd):
        self.cmd = cmd
        self.cwd = cwd
        self.proc = None

    def spawn(self):
        from winpty import PtyProcess
        # 环境按 Windows 语义从注册表重建, 不用守护进程的 os.environ ——
        # 后者是它启动那一刻的快照, 用户后来改的环境变量看不到 (见 winenv)
        env = winenv.freshEnvironment()
        env['TERM'] = 'xterm-256color'
        self.proc = PtyProcess.spawn(
            self.cmd, cwd=self.cwd or os.path.expanduser('~'),
            env=env, dimensions=(24, 80))

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


def detach(session, ws):
    """摘除附着关系并记录断开时刻 (供回收线程判定空闲)。"""
    with session.sessionLock:
        if session.attached is ws:
            session.attached = None
            session.lastDetachAt = time.time()


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
        fg = termprobe.foregroundInfo(session)
        items.append({'id': session.sessionID, 'kind': session.kind,
                      'cmd': session.label, 'label': session.label,
                      'hostId': session.hostId, 'attached': attached,
                      'cwd': getattr(session, 'cwd', None),
                      'cwdLabel': termprobe.cwdLabel(session),
                      'foreground': fg.get('name', ''),
                      'foregroundCwd': fg.get('cwd', ''),
                      'foregroundCwdLabel': termprobe.cwdLabelFor(fg.get('cwd', ''))})
    return items


def newSessionID():
    return uuid.uuid4().hex
