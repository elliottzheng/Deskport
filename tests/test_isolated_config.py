# -*- coding: utf-8 -*-
"""isolatedConfig 自检：会话配置根必须"每次全新"，脏 basetemp 伤不到它。

背景：旧实现用 tmp_path_factory.mktemp('wp-test-config')，上次跑挂留下的
.pytest-tmp 残留会让 mkdir 抛 FileExistsError（WinError 183），整套测试
20 个 ERROR 假红。现改用 _freshConfigRoot()（mkdtemp）——本测试钉住
"basetemp 被占用也自动换新目录，不静默失败"。
"""
import shutil
from pathlib import Path
from types import SimpleNamespace

import config
import conftest


def test_fresh_root_ignores_occupied_dir(tmp_path):
    """basetemp 里躺着旧实现会用的同名脏目录时，照样拿到全新的空目录。"""
    stale = tmp_path / 'wp-test-config0'
    stale.mkdir()
    (stale / 'config.json').write_text('{"port": 1}', encoding='utf-8')
    root = conftest._freshConfigRoot()
    try:
        assert root.is_dir()
        assert root != stale
        assert list(root.iterdir()) == []   # 不继承残留里的任何东西
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fresh_root_is_unique_each_call():
    first = conftest._freshConfigRoot()
    second = conftest._freshConfigRoot()
    try:
        assert first != second
        assert first.is_dir() and second.is_dir()
    finally:
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_session_config_root_is_fresh_dir():
    """session fixture 实际在用的根：真实存在、带着最小 config.json。"""
    root = Path(config.CONFIG_PATH).parent
    assert root.is_dir()
    assert (root / 'config.json').is_file()
    assert root.name.startswith('wp-test-config-')


def test_basetemp_missing_parents_are_created(tmp_path):
    """--basetemp 的父目录不存在也自动建（pytest 自己 mkdir 不带 parents）。"""
    target = tmp_path / 'no' / 'such' / 'parent' / 'bt'
    conftest.pytest_configure(SimpleNamespace(option=SimpleNamespace(basetemp=target)))
    assert target.is_dir()
