# -*- coding: utf-8 -*-
"""SessionRegistry：并发安全的增删查。"""
from modules.termcore import SessionRegistry


class S:
    def __init__(self, sid):
        self.sessionID = sid


def test_add_get_remove_values():
    reg = SessionRegistry()
    a, b = S('a'), S('b')
    reg.add(a); reg.add(b)
    assert reg.get('a') is a
    assert {s.sessionID for s in reg.values()} == {'a', 'b'}
    reg.remove(a)
    assert reg.get('a') is None
    assert [s.sessionID for s in reg.values()] == ['b']
