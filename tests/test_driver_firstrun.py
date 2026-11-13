# -*- coding: utf-8 -*-
"""driver.py 第一次被调用（批次 N）：--help 不碰状态、新别名说"先 new"、
state.json 损坏 / driver.json 缺失缺键 / 证书错误都翻成一句人话，无 traceback。"""
import importlib.util
import json
import pathlib
import ssl
import urllib.error

import pytest

DRIVER_PATH = (pathlib.Path(__file__).resolve().parents[1]
               / '.agents' / 'skills' / 'deskport' / 'driver.py')

MINI = {'root': 'E:/proj/app',
        'danger': r'rm\s+-rf|\bkill\b',
        'safe': r'^\s*(ls|cat)\b'}


@pytest.fixture
def load(tmp_path, monkeypatch):
    """按临时目录重载 driver.py（RUN_ROOT 在 import 时求值，必须重载）。

    load()         写一份最小 driver.json 再加载
    load(None)      不写 driver.json（测缺失）
    load({...})     写指定配置（测缺键/坏值）
    """
    run_dir, cfg_path = tmp_path / 'run', tmp_path / 'driver.json'

    def _load(config=MINI):
        if config is None:
            cfg_path.unlink(missing_ok=True)
        else:
            cfg_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        monkeypatch.setenv('DRIVER_RUN_DIR', str(run_dir))
        monkeypatch.setenv('DRIVER_CONFIG', str(cfg_path))
        monkeypatch.delenv('DRIVER_DEBUG', raising=False)
        spec = importlib.util.spec_from_file_location('driver_firstrun', DRIVER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    return _load


def _both(capsys):
    out = capsys.readouterr()
    return out.out, out.err


def test_help_and_no_args_exit_zero_without_state(load, tmp_path, capsys):
    """--help/无参数：打印真用法、exit 0、不创建任何别名目录（别去碰 state）。"""
    mod = load()
    for argv in (['--help'], ['-h'], []):
        assert mod.main(argv) == 0, argv
    out, err = _both(capsys)
    assert 'driver.py <别名> new' in out and 'driver.example.json' in out
    assert not (tmp_path / 'run').exists()
    assert err == ''


def test_fresh_alias_says_new_first(load, capsys):
    """全新别名跑 st：一句"还没有任务，先 new"、exit 3、不联网、无栈。"""
    mod = load()
    assert mod.main(['t1', 'st']) == 3
    out, err = _both(capsys)
    assert '还没有任务' in out and 'new' in out
    assert 'Traceback' not in out and err == ''


def test_corrupt_state_explains_reset(load, capsys):
    """state.json 写成 {oops：说损坏、说删掉即可重置、exit 2。"""
    mod = load()
    alias = mod.RUN_ROOT / 't1'
    alias.mkdir(parents=True)
    (alias / 'state.json').write_text('{oops', encoding='utf-8')
    assert mod.main(['t1', 'st']) == 2
    out, err = _both(capsys)
    assert 'state.json 损坏' in out and '删掉它即可重置这个别名' in out
    assert 'Traceback' not in out and err == ''


def test_missing_config_points_to_example(load, capsys):
    """driver.json 不在：说缺什么、指向 driver.example.json、exit 2。"""
    mod = load(None)
    assert mod.main(['t1', 'st']) == 2
    out, err = _both(capsys)
    assert '找不到边界配置' in out and 'driver.example.json' in out
    assert 'Traceback' not in out and err == ''


def test_config_missing_key_names_it(load, capsys):
    """driver.json 缺 danger：点名缺哪个键、指向 example、exit 2。"""
    mod = load({'root': 'E:/proj/app', 'safe': r'^\s*ls\b'})
    assert mod.main(['t1', 'st']) == 2
    out, err = _both(capsys)
    assert '缺键 danger' in out and 'driver.example.json' in out
    assert 'Traceback' not in out and err == ''


def test_cert_error_uses_batch_m_wording(load, monkeypatch, capsys):
    """证书错误：与批次 M 给 deskport.py 的话术一致（driver: 前缀 + 三种处理），exit 2。"""
    mod = load()

    def _cert_fail(*a, **kw):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(
            1, 'certificate verify failed: self-signed certificate'))

    monkeypatch.setattr(mod.deskport, 'connect', _cert_fail)
    alias = mod.RUN_ROOT / 't1'
    alias.mkdir(parents=True)
    (alias / 'state.json').write_text('{"task": "x"}', encoding='utf-8')
    assert mod.main(['t1', 'st']) == 2
    out, err = _both(capsys)
    assert 'driver: 对端证书校验失败' in out and '三种处理' in out
    assert 'Traceback' not in out and err == ''


def test_example_file_is_loadable_config(load):
    """包里的 driver.example.json 本身就是一份合法配置（必填三键齐）。"""
    mod = load()
    cfg = json.loads((mod.EXAMPLE).read_text(encoding='utf-8'))
    assert all(cfg.get(k) for k in mod.REQUIRED_KEYS)
