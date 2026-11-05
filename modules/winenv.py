# -*- coding: utf-8 -*-
r"""Windows 环境变量重建 — 让新终端拿到"当前"环境而不是守护进程的陈旧快照。

问题: 会话守护进程是长期运行的, `os.environ` 是它启动那一刻的快照。用户在
系统设置/`setx` 里改过环境变量后, 快照不会更新 —— 表现为"桌面 Terminal 能看到,
平台里新建的会话看不到"。

Windows 的语义: 用户/系统环境变量的权威来源是注册表, 进程创建时由 OS 注入一批
易变变量。所以这里每次 spawn 都按同样顺序重建:

    注册表 系统环境 (HKLM ...Session Manager\Environment)
      -> 注册表 用户环境 (HKCU\Environment, 优先)
      -> OS 注入的易变变量 (注册表里没有, 从 os.environ 取)
      -> 守护进程自身的增量 (注册表没定义的键才保留; shell/会话专属的噪音剔除)

REG_EXPAND_SZ 在注册表里是未展开存的, 这里按合并后的结果展开 %VAR%。
"""
import os
import re
import winreg

_ENV_HIVES = (
    (winreg.HKEY_LOCAL_MACHINE,
     r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
    (winreg.HKEY_CURRENT_USER, r'Environment'),
)

# Windows 每次创建进程都会注入、但注册表里没有的易变变量。
# 少了它们 PowerShell/程序会行为异常 (例如找不到 SYSTEMROOT)。
_ENV_VOLATILE = {
    'ALLUSERSPROFILE', 'APPDATA', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)',
    'COMMONPROGRAMFILES(W6432)', 'COMMONPROGRAMW6432', 'COMPUTERNAME', 'COMSPEC',
    'DRIVERDATA', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'LOGONSERVER',
    'NUMBER_OF_PROCESSORS', 'OS', 'PATHEXT', 'PROCESSOR_ARCHITECTURE',
    'PROCESSOR_ARCHITECTURE(W6432)', 'PROCESSOR_IDENTIFIER', 'PROCESSOR_LEVEL',
    'PROCESSOR_REVISION', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
    'PROGRAMW6432', 'PUBLIC', 'SESSIONNAME', 'SYSTEMDRIVE', 'SYSTEMROOT',
    'TEMP', 'TMP', 'USERDOMAIN', 'USERDOMAIN_ROAMINGPROFILE', 'USERNAME',
    'USERPROFILE', 'WINDIR',
}

# 启动守护进程的那个 shell 留下的会话专属变量, 不该泄漏进新终端。
# (MSYS/Git-Bash 的 MSYSTEM/SHELL/PWD, Flask dev server 的 WERKZEUG_SERVER_FD 等)
_ENV_NOISE = {
    'MSYSTEM', 'MSYS2_ARG_CONV_EXCL', 'MINGW_CHOST', 'MINGW_PREFIX', 'MINGW64',
    'SHELL', 'PWD', 'OLDPWD', 'SHLVL', 'HOME', 'EXEPATH', 'PLINK_PROTOCOL',
    'GIT_TERMINAL_PROMPT', 'NO_COLOR', 'TERM', 'PROMPT', '_', 'ORIGINAL_PATH',
    'WERKZEUG_SERVER_FD', 'WT_SESSION', 'WT_PROFILE_ID', 'PSMODULEPATH',
    'VIRTUAL_ENV', 'FPS_BROWSER_APP_PROFILE_STRING', 'FPS_BROWSER_USER_PROFILE_STRING',
}

_VAR_RE = re.compile(r'%([A-Za-z_][A-Za-z0-9_]*)%')


def _readHive(root, sub):
    out = {}
    try:
        with winreg.OpenKey(root, sub) as key:
            i = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                out[name] = value if isinstance(value, str) else str(value)
                i += 1
    except OSError:
        pass          # 无权限/键不存在: 该层为空, 不影响其它层
    return out


def _expand(value, env):
    """展开 %VAR%; 最多几轮, 遇到自引用/未知名就停下。"""
    for _ in range(6):
        if '%' not in value:
            break
        nxt = _VAR_RE.sub(
            lambda m: str(env.get(m.group(1), os.environ.get(m.group(1), m.group(0)))),
            value)
        if nxt == value:
            break
        value = nxt
    return value


def freshEnvironment():
    """返回一份按 Windows 语义重建的环境字典 (每次调用都重新读注册表)。"""
    env = {}
    for root, sub in _ENV_HIVES:
        env.update(_readHive(root, sub))
    for key in _ENV_VOLATILE:
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    for key, value in os.environ.items():
        if key in env or key in _ENV_NOISE:
            continue
        env[key] = value          # 守护进程自身的增量 (虚拟环境/临时注入)
    return {k: _expand(v, env) for k, v in env.items()}
