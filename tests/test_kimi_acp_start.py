# -*- coding: utf-8 -*-
"""KimiAcp.start 的启动失败归因：cwd 不存在 / Popen 失败 / kimi 缺失
三种场景必须各说各话，不能再让"工作目录不存在"冒充"找不到 kimi 命令"。"""
import pytest

from modules import kimi_acp as kacp
from modules.kimi_acp import AcpError, KimiAcp


def _noPopen(*a, **kw):
    raise AssertionError('不该走到 Popen')


def test_start_cwd_missing_reports_workdir(monkeypatch, tmp_path):
    """cwd 不存在：报"工作目录不存在"，且不得冒充"找不到 kimi 命令"。"""
    missing = tmp_path / 'no-such-dir'
    monkeypatch.setattr(kacp, '_kimiBin', lambda: 'kimi.exe')
    monkeypatch.setattr(kacp.subprocess, 'Popen', _noPopen)
    client = KimiAcp(cwd=str(missing))
    with pytest.raises(AcpError) as ei:
        client.start()
    msg = str(ei.value)
    assert '工作目录' in msg
    assert '找不到 kimi' not in msg
    assert str(missing) in msg


def test_start_popen_filenotfound_reports_env(monkeypatch, tmp_path):
    """kimi 定位成功但 Popen FileNotFoundError：归因 cwd/执行环境，
    不冒充"找不到 kimi 命令"（远端 cwd 已过 isdir 但执行仍失败时）。"""
    monkeypatch.setattr(kacp, '_kimiBin', lambda: 'kimi.exe')

    def fakePopen(cmd, **kw):
        raise FileNotFoundError('cwd gone')

    monkeypatch.setattr(kacp.subprocess, 'Popen', fakePopen)
    client = KimiAcp(cwd=str(tmp_path))
    with pytest.raises(AcpError) as ei:
        client.start()
    msg = str(ei.value)
    assert '启动 kimi 失败' in msg
    assert '找不到 kimi' not in msg


def test_start_kimi_bin_missing_still_says_no_kimi(monkeypatch, tmp_path):
    """_kimiBin 找不到：保持"找不到 kimi 命令"原文，不被上面两条吞掉。"""
    monkeypatch.setattr(kacp.shutil, 'which', lambda name: None)
    monkeypatch.setattr(kacp.subprocess, 'Popen', _noPopen)
    client = KimiAcp(cwd=str(tmp_path))
    with pytest.raises(AcpError) as ei:
        client.start()
    assert '找不到 kimi 命令' in str(ei.value)
