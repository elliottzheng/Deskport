# -*- coding: utf-8 -*-
"""winenv: 新终端必须拿到"当前"环境, 而不是守护进程启动时的快照。

背景: 会话守护进程长期运行, `os.environ` 是它启动那一刻的快照。用户在系统
设置里改过环境变量后快照不更新, 表现为"桌面 Terminal 能看到, 平台里新建的
会话看不到"。

Windows 语义 (本模块的核心):
    - 用户/系统环境变量的权威来源是注册表, 进程创建时 OS 再注入一批易变变量;
    - Path / PATHEXT 是"系统段 + 用户段"拼接, 不是用户段覆盖系统段 ——
      覆盖会丢掉 %SystemRoot%\\system32, 新终端里 cmd.exe 都会找不到;
    - 其余键用户段覆盖系统段 (如 TEMP)。
"""
import winreg

import pytest

from modules import winenv

SYSTEM_NAME = 'SYSTEM'
USER_NAME = 'USER'


class _Hive:
    """支持 with 语句的假 HKEY; with 出来后是它的名字字符串。"""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self.name

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fakeRegistry(monkeypatch):
    """替换注册表读取: OpenKey 返回假 HKEY, EnumValue 按名字选表。

    不能只换 OpenKey —— winreg.EnumValue 会校验参数是真 PyHKEY, 假对象直接
    TypeError, 所以两个一起换。注意 with 语句交出的是 name 字符串,
    fakeEnum 要比字符串。
    """
    system = {'Path': r'%SystemRoot%\system32;%SystemRoot%',
              'TEMP': r'C:\Windows\Temp', 'OS': 'Windows_NT',
              'NUMBER_OF_PROCESSORS': '16', 'PATHEXT': '.COM;.EXE;.BAT'}
    user = {'Path': r'%USERPROFILE%\bin', 'OneDrive': r'C:\Users\x\OneDrive',
            'TEMP': r'C:\Users\x\AppData\Local\Temp'}

    monkeypatch.setattr(winenv.winreg, 'OpenKey',
                        lambda root, sub: (_Hive(SYSTEM_NAME)
                                           if root == winreg.HKEY_LOCAL_MACHINE
                                           else _Hive(USER_NAME)))

    def fakeEnum(key, i):
        table = system if key == SYSTEM_NAME else user
        items = sorted(table.items())
        if i >= len(items):
            raise OSError('no more values')
        return items[i][0], items[i][1], 1

    monkeypatch.setattr(winenv.winreg, 'EnumValue', fakeEnum)
    return system, user


SYSTEM_PLUS_USER = (r'C:\Windows\system32;C:\Windows;' + r'C:\Users\ZYL\bin')


def test_path_is_concatenated_not_overridden(fakeRegistry, monkeypatch):
    """Path = 系统段 + 用户段。用户段覆盖系统段会丢掉 system32。"""
    monkeypatch.setenv('Path', 'STALE-PATH')
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    env = winenv.freshEnvironment()
    assert env['PATH'] == SYSTEM_PLUS_USER


def test_registry_wins_over_stale_snapshot(fakeRegistry, monkeypatch):
    """注册表定义过的键, 用注册表的值, 不用守护进程的陈旧快照。"""
    monkeypatch.setenv('OneDrive', '')
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    env = winenv.freshEnvironment()
    assert env['ONEDRIVE'] == r'C:\Users\x\OneDrive'
    # TEMP 不在拼接集, 用户段覆盖系统段
    assert env['TEMP'] == r'C:\Users\x\AppData\Local\Temp'


def test_system_only_keys_survive(fakeRegistry, monkeypatch):
    """只在系统段有的键必须活下来 (PATHEXT/OS 等)。"""
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    env = winenv.freshEnvironment()
    assert env['PATHEXT'] == '.COM;.EXE;.BAT'
    assert env['OS'] == 'Windows_NT'


def test_user_hive_overrides_system_for_non_additive(fakeRegistry, monkeypatch):
    monkeypatch.delenv('Path', raising=False)
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    env = winenv.freshEnvironment()
    assert env['PATH'] == SYSTEM_PLUS_USER
    assert env['TEMP'] == r'C:\Users\x\AppData\Local\Temp'


def test_expand_vars_uses_merged_env(fakeRegistry, monkeypatch):
    """REG_EXPAND_SZ 存的是未展开的 %VAR%, 要按合并后的环境展开。"""
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    monkeypatch.delenv('Path', raising=False)
    env = winenv.freshEnvironment()
    assert env['PATH'] == SYSTEM_PLUS_USER


def test_expand_vars_unknown_left_alone(fakeRegistry, monkeypatch):
    _system, user = fakeRegistry
    user['Path'] = r'%NOT_A_REAL_VAR%\x'
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    env = winenv.freshEnvironment()
    assert env['PATH'] == (r'C:\Windows\system32;C:\Windows;'
                           + r'%NOT_A_REAL_VAR%\x')


def test_system_segment_not_dropped(fakeRegistry, monkeypatch):
    """系统段里独有的条目必须在最终 PATH 里 —— 本模块回归最多的坑。"""
    system, _user = fakeRegistry
    system['Path'] = r'%SystemRoot%\system32;D:\only-in-system'
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    monkeypatch.setenv('USERPROFILE', r'C:\Users\ZYL')
    env = winenv.freshEnvironment()
    assert env['PATH'].startswith(r'C:\Windows\system32;')
    assert r'D:\only-in-system' in env['PATH']
    assert env['PATH'].endswith(r'C:\Users\ZYL\bin')


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


def test_real_registry_keeps_system_root(fakeRegistry, monkeypatch):
    """不打桩读真实注册表: PATH 必须含 system32, 否则新终端全是坏壳。"""
    monkeypatch.undo()          # 撤掉桩, 用真注册表
    env = winenv.freshEnvironment()
    path = (env.get('PATH') or env.get('PATH') or '').lower()
    assert 'system32' in path, '真实 PATH 缺 system32: %s' % path[:200]
    assert env.get('SystemRoot') or env.get('SYSTEMROOT')
