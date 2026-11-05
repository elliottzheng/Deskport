# -*- coding: utf-8 -*-
"""termcore 附着泵的提取函数回归（行为不变的拆分验收）。"""
import json
import queue
import threading

from modules import termcore


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.fail = False

    def send(self, data):
        if self.fail:
            raise OSError('broken pipe')
        self.sent.append(data)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.sessionLock = threading.Lock()
        self.attached = None
        self.outQueue = queue.Queue()


def test_drain_data_sends_text():
    session, ws = FakeSession(), FakeWs()
    session.outQueue.put(('data', 'hello'))
    assert termcore._drainOutbox(ws, session) is False
    assert ws.sent == ['hello']


def test_drain_exit_sends_frame_and_stops():
    session, ws = FakeSession(), FakeWs()
    session.outQueue.put(('exit', 7))
    assert termcore._drainOutbox(ws, session) is True
    assert json.loads(ws.sent[-1]) == {'type': 'exit', 'code': 7}
    assert ws.closed is True


def test_drain_error_sends_frame_and_stops():
    session, ws = FakeSession(), FakeWs()
    session.outQueue.put(('error', 'boom'))
    assert termcore._drainOutbox(ws, session) is True
    assert 'boom' in json.loads(ws.sent[-1])['msg']
    assert ws.closed is True


def test_drain_send_failure_stops():
    session, ws = FakeSession(), FakeWs()
    ws.fail = True
    session.outQueue.put(('data', 'hello'))
    assert termcore._drainOutbox(ws, session) is True


def test_preempted_detects_detach():
    session, ws = FakeSession(), FakeWs()
    session.attached = ws
    assert termcore._preempted(session, ws) is False
    session.attached = object()
    assert termcore._preempted(session, ws) is True
    assert json.loads(ws.sent[-1]) == {'type': 'preempted'}
    assert ws.closed is True


def test_replay_buffer_is_bounded():
    term = termcore.TermSession('s1', 'local', 'pwsh', backend=None)
    for _ in range(termcore.REPLAY_MAX // 1024 + 50):
        term.enqueueOut(('data', 'x' * 1024))
    assert term.replaySize <= termcore.REPLAY_MAX
    assert len(term.replayBuf) <= termcore.REPLAY_MAX // 1024


def test_replay_snapshot_takes_tail_only():
    """附着回放只发尾部: 旧数据丢弃, 尾部完整保留, 总量受 REPLAY_TAIL 约束。"""
    import collections
    buf = collections.deque()
    buf.append('old' * 4000)                       # 应被丢掉的旧输出
    tail = 'z' * (termcore.REPLAY_TAIL + 2048)
    buf.append(tail)
    snap = termcore._replaySnapshot(buf)
    joined = ''.join(snap)
    assert 'old' not in joined
    assert joined.endswith(tail[-200:])
    assert termcore.REPLAY_TAIL <= len(joined) < termcore.REPLAY_MAX
