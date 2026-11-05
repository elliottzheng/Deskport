# -*- coding: utf-8 -*-
"""termcore 会话实体与回放缓冲的回归。"""
from modules import termcore


def test_replay_buffer_is_bounded():
    term = termcore.TermSession('s1', 'local', 'pwsh', backend=None)
    for _ in range(termcore.REPLAY_MAX // 1024 + 50):
        term.enqueueOut(('data', 'x' * 1024))
    assert term.replaySize <= termcore.REPLAY_MAX
    assert len(term.replayBuf) <= termcore.REPLAY_MAX // 1024


def test_registry_add_get_remove():
    reg = termcore.SessionRegistry()
    term = termcore.TermSession('s1', 'local', 'pwsh', backend=None)
    reg.add(term)
    assert reg.get('s1') is term
    assert reg.values() == [term]
    reg.remove(term)
    assert reg.get('s1') is None
    assert reg.values() == []
