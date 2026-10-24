# -*- coding: utf-8 -*-
"""WS 收尾：先 close 帧，再 SHUT_WR，阻断 teardown 补写 HTTP 头。"""
import socket

from modules.termcore import sealSocketAfterClose


class FakeSock:
    def __init__(self):
        self.shutdowns = []

    def shutdown(self, how):
        self.shutdowns.append(how)


class FakeWs:
    def __init__(self, connected=True):
        self.connected = connected
        self.closed = False
        self.sock = FakeSock()

    def close(self):
        self.closed = True


def test_seal_closes_frame_and_write_side():
    ws = FakeWs()
    sealSocketAfterClose(ws)
    assert ws.closed is True
    assert ws.sock.shutdowns == [socket.SHUT_WR]


def test_seal_still_shuts_write_when_already_closed():
    ws = FakeWs(connected=False)
    sealSocketAfterClose(ws)
    assert ws.closed is False
    assert ws.sock.shutdowns == [socket.SHUT_WR]
