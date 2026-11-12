# -*- coding: utf-8 -*-
"""driver.py 的 run 目录：默认技能包内 run/，DRIVER_RUN_DIR 可覆盖。

driver.py 在 import 时求值 RUN_ROOT，所以每个用例用 importlib 重新加载
（不改生产逻辑，只按环境变量重算）。
"""
import importlib.util
import pathlib

DRIVER_PATH = (pathlib.Path(__file__).resolve().parents[1]
               / '.agents' / 'skills' / 'deskport' / 'driver.py')


def _load(name):
    spec = importlib.util.spec_from_file_location(name, DRIVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_root_defaults_to_package(monkeypatch):
    monkeypatch.delenv('DRIVER_RUN_DIR', raising=False)
    mod = _load('driver_runroot_default')
    assert mod.RUN_ROOT == DRIVER_PATH.parent / 'run'


def test_run_root_env_override(monkeypatch, tmp_path):
    alias = tmp_path / '别名工作区'
    monkeypatch.setenv('DRIVER_RUN_DIR', str(alias))
    mod = _load('driver_runroot_env')
    assert mod.RUN_ROOT == alias
    # 别名目录都从 RUN_ROOT 拼出来（msg.txt/last.txt/log/state.json 一起迁移）
    assert mod.RUN_ROOT / 'a' / 'msg.txt' == alias / 'a' / 'msg.txt'