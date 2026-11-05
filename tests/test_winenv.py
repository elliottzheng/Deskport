# -*- coding: utf-8 -*-
"""winenv: 新终端必须拿到"当前"环境, 而不是守护进程启动时的快照。

背景: 会话守护进程长期运行, `os.environ` 是它启动那一刻的快照。用户在系统
设置里改过环境变量后快照不更新, 表现为"桌面 Terminal 能看到, 平台里新建的
会话看不到"。
"""
import winreg

import pytest

from modules import winenv

class _Hive:
    """支持 with 语句的假 HKEY; 身份用 name 区分。"""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self.name

    def __exit__(self, *a):
        return False


SYSTEM_SENTINEL = _Hive('SYSTEM')
USER_SENTINEL = _Hive('USER')


@pytest.fixture()
def fakeRegistry(monkeypatch):
    """替换注册表读取: OpenKey 返回哨兵, EnumValue 按哨兵选表。

    不能只换 OpenKey —— winreg.EnumValue 会校验参数是真 PyHKEY, 假对象直接
    TypeError, 所以两个一起换。
    """
    system = {'Path': r'C:\Windows\system32', 'TEMP': r'C:\Windows\Temp',
              'OS': 'Windows_NT', 'NUMBER_OF_PROCESSORS': '16'}
    user = {'Path': r'%USERPROFILE%\bin;C:\Windows\system32',
            'OneDrive': r'C:\Users\x\OneDrive'}

    monkeypatch.setattr(winenv.winreg, 'OpenKey',
                        lambda root, sub: (SYSTEM_SENTINEL
                                           if root == winreg.HKEY_LOCAL_MACHINE
                                           else USER_SENTINEL))

    def fakeEnum(key, i):
        table = system if key == SYSTEM_SENTINEL else user
        items = sorted(table.items())
        if i >= len(items):
            raise OSError('no more values')
        return items[i][0], items[i][1], 1

    monkeypatch.setattr(winenv.winreg, 'EnumValue', fakeEnum)
    return system, user


def test_registry_wins_over_stale_snapshot(fakeRegistry, monkeypatch):
    """注册表定义过的键, 必须用注册表的值覆盖守护进程的陈旧快照。"""
    monkeypatch.setenv('Path', 'STALE-PATH')
    monkeypatch.setenv('OneDrive', '')
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    monkeypatch.setenv('SYSTEMROOT', r'C:\Windows')      # 易变变量, 注册表没有
    env = winenv.freshEnvironment()
    # 注册表值覆盖陈旧快照; %USERPROFILE% 按合并后的环境展开
    assert env['Path'] == r'C:\Users\ZYL\bin;C:\Windows\system32'
    assert env['OneDrive'] == r'C:\Users\x\OneDrive'
    assert env['SYSTEMROOT'] == r'C:\Windows'


def test_user_hive_overrides_system(fakeRegistry, monkeypatch):
    monkeypatch.delenv('Path', raising=False)
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    env = winenv.freshEnvironment()
    assert env['Path'] == r'C:\Users\ZYL\bin;C:\Windows\system32'
    assert env['OS'] == 'Windows_NT'


def test_expand_vars_uses_merged_env(fakeRegistry, monkeypatch):
    """REG_EXPAND_SZ 存的是未展开的 %VAR%, 要按合并后的环境展开。"""
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    monkeypatch.delenv('Path', raising=False)
    env = winenv.freshEnvironment()
    assert env['Path'].startswith(r'C:\Users\ZYL\bin;')


def test_expand_vars_unknown_left_alone(fakeRegistry):
    _system, user = fakeRegistry
    user['Path'] = r'%NOT_A_REAL_VAR%\x'
    env = winenv.freshEnvironment()
    assert env['Path'] == r'%NOT_A_REAL_VAR%\x'


def test_session_noise_not_leaked(fakeRegistry, monkeypatch):
    """启动守护进程的那个 shell 的会话专属变量不该进新终端。"""
    for name in ('MSYSTEM', 'SHELL', 'PWD', 'OLDPWD', 'SHLVL', 'TERM',
                 'WERKZEUG_SERVER_FD', 'PROMPT'):
        monkeypatch.setenv(name, 'noise-' + name)
    env = winenv.freshEnvironment()
    for name in ('MSYSTEM', 'SHELL', 'PWD', 'OLDPWD', 'SHLVL', 'TERM',
                 'WERKZEUG_SERVER_FD', 'PROMPT'):
        assert name not in env, name


def test_daemon_own_additions_survive(fakeRegistry, monkeypatch):
    """注册表没定义、又不是噪音的键 (虚拟环境等) 要保留。"""
    monkeypatch.setenv('MY_TOOLCHAIN_HOME', r'D:\toolchain')
    env = winenv.freshEnvironment()
    assert env['MY_TOOLCHAIN_HOME'] == r'D:\toolchain'


def test_volatile_vars_present(fakeRegistry, monkeypatch):
    """OS 每次建进程都注入的易变变量, 注册表里没有也要补上。"""
    monkeypatch.setenv('COMPUTERNAME', 'MY-PC')
    monkeypatch.setenv('USERNAME', 'ZYL')
    env = winenv.freshEnvironment()
    assert env['COMPUTERNAME'] == 'MY-PC'
    assert env['USERNAME'] == 'ZYL'


def test_real_registry_read_does_not_raise():
    """不打桩直接读真实注册表: 不能抛异常, 且 PATH 非空。"""
    env = winenv.freshEnvironment()
    assert env.get('Path') or env.get('PATH')
    assert env.get('SystemRoot') or env.get('SYSTEMROOT')
