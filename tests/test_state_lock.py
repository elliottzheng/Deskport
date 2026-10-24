# -*- coding: utf-8 -*-
"""state 的跨进程锁：拿不到锁必须显式失败，不能静默继续。"""
import pytest

from modules import state


def test_file_lock_times_out_when_held(tmp_path):
    path = str(tmp_path / 'audit.jsonl')
    with state._FileLock(path):
        with pytest.raises(TimeoutError):
            with state._FileLock(path):
                pass


def test_append_jsonl_writes_lines(tmp_path):
    path = tmp_path / 'audit.jsonl'
    state.appendJsonl(str(path), {'n': 1})
    state.appendJsonl(str(path), {'n': 2})
    assert [line for line in path.read_text(encoding='utf-8').splitlines()] == [
        '{"n": 1}', '{"n": 2}']
