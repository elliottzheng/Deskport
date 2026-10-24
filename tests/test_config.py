# -*- coding: utf-8 -*-
"""配置缓存与令牌文件。"""
import os

import config


def test_config_reloads_when_mtime_changes(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{"port": 7001}', encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', str(path))
    config._cache.update(mtime=None, data=None)
    assert config.getPort() == 7001

    path.write_text('{"port": 7002}', encoding='utf-8')
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert config.getPort() == 7002
    config._cache.update(mtime=None, data=None)


def test_sessiond_token_is_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'statePath',
                        lambda name: str(tmp_path / name))
    first = config.getSessiondToken('local')
    second = config.getSessiondToken('local')
    assert first and first == second
    assert (tmp_path / 'sessiond_token_local').read_text(encoding='utf-8') == first


def test_sessiond_port_follows_url(monkeypatch):
    monkeypatch.setattr(config, 'getSessiondUrl',
                        lambda kind='local': 'http://127.0.0.1:7001')
    assert config.getSessiondPort('local') == 7001
    monkeypatch.setattr(config, 'getSessiondUrl', lambda kind='local': '')
    assert config.getSessiondPort('ssh') == 6792
