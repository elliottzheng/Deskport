# -*- coding: utf-8 -*-
"""终端 WS 帧协议：编解码、控制帧、回放、附着泵送。

从 termcore 拆出（2026-11-05）。协议形状集中在 modules/contracts.py，
本文件只放"怎么发/怎么解/怎么泵"，不持有任何会话状态。

硬约束（实测教训，不可违背）：
    - ws.send 全部收敛到附着主循环线程
    - handler 退出时 seal 阻断 werkzeug teardown 补写
    - 回放结束才放行客户端输入, 否则 xterm 对终端能力查询的自动应答
      会泄漏进 shell 输入行 (TERM-BUG-01)

依赖方向：wsproto → termcore（terminateSession/detach）→ termprobe（探针）。
"""
import json
import queue
import socket
import time

import modules.internal_api as internal_api
from modules import contracts, termcore, termprobe

# 附着时只下发尾部: 客户端 xterm 默认 scrollback 1000 行, 发多了也被丢;
# 而全量回放在手机上要逐行渲染, 视觉上就是"疯狂滚动"
REPLAY_TAIL = 192 * 1024
SPAWN_WAIT_SECONDS = 15


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


def decodeFrame(msg):
    """区分控制帧(带 type 的 JSON)与原始输入。返回 (payload, isControl)。"""
    text = msg if isinstance(msg, str) else msg.decode('utf-8', errors='replace')
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and 'type' in obj:
            return obj, True
    except (ValueError, TypeError):
        pass
    return text, False


def handleControl(ws, session, backend, payload):
    ctype = payload.get('type')
    if ctype == 'resize':
        try:
            backend.resize(int(payload.get('rows', 24)), int(payload.get('cols', 80)))
        except Exception:
            pass
    elif ctype == 'ping':
        sendJson(ws, {'type': 'pong'})
    elif ctype == 'close':
        termcore.terminateSession(session)


def replaySnapshot(replayBuf):
    """附着回放快照: 只取尾部 REPLAY_TAIL 字节。

    全量回放(会话缓冲最大 REPLAY_MAX)在手机上要逐行渲染, 视觉上就是
    "恢复会话时从头滚到尾"; 客户端 xterm 默认 scrollback 1000 行, 发多了
    也被丢弃, 因此只发尾部。
    """
    acc, size = [], 0
    for chunk in reversed(replayBuf):
        acc.append(chunk)
        size += len(chunk if isinstance(chunk, bytes) else chunk.encode())
        if size >= REPLAY_TAIL:
            break
    return list(reversed(acc))


def sendJson(ws, obj):
    try:
        ws.send(json.dumps(obj, ensure_ascii=False))
        return True
    except Exception:
        return False


def sendText(ws, text):
    try:
        ws.send(text)
        return True
    except Exception:
        return False


def closeWs(ws):
    try:
        ws.close()
    except Exception:
        pass


def _failAttach(ws, session, message):
    """附着失败: 发 error 帧、关闭、摘除附着、封锁写方向。"""
    sendJson(ws, {'type': 'error', 'msg': message})
    closeWs(ws)
    termcore.detach(session, ws)
    sealSocketAfterClose(ws)


def _preempted(session, ws):
    """被其他连接抢占时通知旧客户端并返回 True。"""
    with session.sessionLock:
        if session.attached is ws:
            return False
    sendJson(ws, {'type': 'preempted'})
    closeWs(ws)
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
            sendJson(ws, {'type': 'error', 'msg': '启动失败: ' + str(payload)})
            closeWs(ws)
            return True
        if kind == 'exit':
            sendJson(ws, {'type': 'exit', 'code': payload})
            closeWs(ws)
            return True
        text = payload if isinstance(payload, str) else payload.decode(
            'utf-8', errors='replace')
        if not sendText(ws, text):
            return True


def attachAndPump(ws, session, resumed=False, wantReplay=False):
    """附着会话并泵 IO: spawned 等待 → hello → 回放 → 输出转发 → 输入/控制帧。

    本地与 SSH 会话共用; 后端差异全部封装在 backend 接口内。
    """
    with session.sessionLock:
        session.attached = ws
    # 等 spawn 就绪; 期间 reader 可能入队 error 帧 (spawn 失败), 优先透传真实原因
    deadline = time.time() + SPAWN_WAIT_SECONDS
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

    replaySnapshotChunks = []
    if wantReplay:
        replaySnapshotChunks = replaySnapshot(session.replayBuf)
        while True:
            try:
                session.outQueue.get_nowait()
            except queue.Empty:
                break

    fgInfo = termprobe.foregroundInfo(session)
    hello: contracts.HelloFrame = {
        'type': 'hello', 'session': session.sessionID,
        'resumed': resumed, 'label': session.label,
        'kind': session.kind,
        'cwd': getattr(session, 'cwd', None),
        'cwdLabel': termprobe.cwdLabel(session),
        'foreground': fgInfo.get('name', ''),
        'foregroundCwd': fgInfo.get('cwd', ''),
        'foregroundCwdLabel': termprobe.cwdLabelFor(fgInfo.get('cwd', '')),
        'proto': internal_api.PROTO,
        'caps': internal_api.CAPS,
    }
    sendJson(ws, hello)
    for chunk in replaySnapshotChunks:
        if not sendText(ws, chunk if isinstance(chunk, str)
                        else chunk.decode('utf-8', errors='replace')):
            break
    if wantReplay:
        # 回放结束标记: 客户端收到前丢弃全部本地输入
        sendJson(ws, {'type': 'replay-end'})

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
            payload, isCtrl = decodeFrame(msg)
            if isCtrl:
                handleControl(ws, session, session.backend, payload)
                continue
            try:
                session.backend.write(payload)
            except Exception:
                break
    except Exception:
        pass
    finally:
        termcore.detach(session, ws)
        sealSocketAfterClose(ws)
