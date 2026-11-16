# -*- coding: utf-8 -*-
"""state 的跨进程锁：拿不到锁必须显式失败，不能静默继续。"""
import json
import os

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


def test_atomic_write_json_retries_transient_replace_denial(tmp_path,
                                                            monkeypatch):
    """批 PS2 追件：atomicWriteJson 与 store_io.saveSnapshot 共用
    state.replaceWithRetry——瞬时 PermissionError（WinError 5/32，安全软件/
    索引器对目标的瞬间占用）重试后成功，不抛异常、不留临时文件。这正是
    并行全量里收件箱游标偶发不推进（被 sweep 的 except 吞掉）的根因。"""
    path = str(tmp_path / 'leads.json')
    real = os.replace
    calls = {'n': 0}

    def flaky(src, dst):
        if str(dst) == path:
            calls['n'] += 1
            if calls['n'] <= 2:
                raise PermissionError('[WinError 5] 拒绝访问。')
        return real(src, dst)

    monkeypatch.setattr(state.os, 'replace', flaky)
    state.atomicWriteJson(path, {'lead': 'L', 'inboxSeq': 1})
    assert calls['n'] == 3                 # 两次败 + 第三次成
    assert json.loads(open(path, encoding='utf-8').read()) \
        == {'lead': 'L', 'inboxSeq': 1}
    assert not list(tmp_path.glob('.tmp-*'))


def test_atomic_write_json_gives_up_after_four_retries(tmp_path, monkeypatch):
    """一直 PermissionError → 退避重试 4 次（20/40/80/160ms）后抛给调用方，
    临时文件清掉（不泄露 .tmp-*），调用方的失败处理不受牵连。"""
    path = str(tmp_path / 'leads.json')
    real = os.replace
    calls = {'n': 0}

    def alwaysDeny(src, dst):
        if str(dst) == path:
            calls['n'] += 1
            raise PermissionError('[WinError 32] 另一个程序正在使用此文件。')
        return real(src, dst)

    monkeypatch.setattr(state.os, 'replace', alwaysDeny)
    with pytest.raises(PermissionError):
        state.atomicWriteJson(path, {'a': 1})
    assert calls['n'] == 5                 # 首发 + 4 次退避重试
    assert not list(tmp_path.glob('.tmp-*'))
