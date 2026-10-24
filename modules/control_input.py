# -*- coding: utf-8 -*-
"""控制台输入：电源/按键/文本。"""
import ctypes
import subprocess
import time
from functools import wraps

from flask import jsonify, request

from modules.control_base import blueprint, _audit

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

POWER_COMMANDS = {
    'shutdown': ['shutdown', '/s', '/t'],
    'restart': ['shutdown', '/r', '/t'],
    'abort': ['shutdown', '/a'],
}


def _dryRunPreview(data):
    action = data.get('action')
    preview = {'ok': True, 'dryRun': True}
    if action in POWER_COMMANDS:
        command = list(POWER_COMMANDS[action])
        if action != 'abort':
            try:
                delay = max(0, min(int(data.get('delay') or 0), 3600))
            except (TypeError, ValueError):
                delay = 0
            command.append(str(delay))
        preview['cmd'] = command
    if action:
        preview['action'] = action
    if 'text' in data:
        preview['length'] = len(data.get('text') or '')
        preview['submit'] = bool(data.get('submit'))
    return preview


def dryRunAction(fn):
    """dryRun 统一在入口返回，动作函数不再各写一遍分支。"""
    @wraps(fn)
    def wrapper():
        data = request.get_json(force=True, silent=True) or {}
        if data.get('dryRun'):
            return jsonify(_dryRunPreview(data))
        return fn(data)
    return wrapper


@blueprint.route('/api/control/power', methods=['POST'])
@dryRunAction
def power(data):
    action = data.get('action')
    try:
        delay = max(0, min(int(data.get('delay') or 0), 3600))
    except (TypeError, ValueError):
        delay = 0
    commands = {name: list(cmd) for name, cmd in POWER_COMMANDS.items()}
    commands['shutdown'].append(str(delay))
    commands['restart'].append(str(delay))
    if action in commands:
        cmd = commands[action]
        try:
            subprocess.run(cmd, capture_output=True, timeout=8,
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        except (OSError, subprocess.TimeoutExpired) as e:
            _audit('power.' + action, result='error', detail=str(e))
            return jsonify({'error': '执行失败: %s' % e}), 500
        _audit('power.' + action, delay)
        return jsonify({'ok': True, 'action': action, 'delay': delay})
    if action == 'lock':
        user32.LockWorkStation()
        _audit('power.lock')
        return jsonify({'ok': True})
    if action == 'sleep':
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        _audit('power.sleep')
        return jsonify({'ok': True})
    if action == 'display_off':
        user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)   # HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER
        _audit('power.display_off')
        return jsonify({'ok': True})
    return jsonify({'error': '未知动作'}), 400


@blueprint.route('/api/control/keys', methods=['POST'])
@dryRunAction
def keys(data):
    action = data.get('action')
    try:
        from pynput.keyboard import Controller, Key
    except ImportError as e:
        return jsonify({'error': 'pynput 不可用: %s' % e}), 500
    mapping = {
        'volume_up': Key.media_volume_up,
        'volume_down': Key.media_volume_down,
        'mute': Key.media_volume_mute,
        'play_pause': Key.media_play_pause,
        'next': Key.media_next,
        'prev': Key.media_previous,
    }
    key = mapping.get(action)
    if not key:
        return jsonify({'error': '未知按键'}), 400
    kb = Controller()
    repeats = max(1, min(int(data.get('repeat') or 1), 10))
    for _ in range(repeats):
        kb.press(key)
        kb.release(key)
    _audit('keys.' + action, repeats)
    return jsonify({'ok': True})


def _setClipboard(text):
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    data = text.encode('utf-16-le') + b'\x00\x00'
    user32.OpenClipboard(None)
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


@blueprint.route('/api/control/text', methods=['POST'])
def sendText():
    """把一段文字粘贴到当前/指定窗口, 可选回车发送 (微信回复等)。"""
    data = request.get_json(force=True, silent=True) or {}
    text = data.get('text') or ''
    submit = bool(data.get('submit'))
    hwnd = data.get('hwnd')
    if not text.strip():
        return jsonify({'error': '文本为空'}), 400
    if data.get('dryRun'):
        return jsonify({'ok': True, 'dryRun': True, 'length': len(text), 'submit': submit})
    if hwnd:
        try:
            hwnd = int(hwnd)
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.4)
        except (TypeError, ValueError):
            pass
    try:
        from pynput.keyboard import Controller, Key
    except ImportError as e:
        return jsonify({'error': 'pynput 不可用: %s' % e}), 500
    _setClipboard(text)
    kb = Controller()
    kb.press(Key.ctrl)
    kb.press('v')
    kb.release('v')
    kb.release(Key.ctrl)
    time.sleep(0.25)
    if submit:
        kb.press(Key.enter)
        kb.release(Key.enter)
    _audit('text.send', hwnd or 'active', 'ok', 'len=%d submit=%s' % (len(text), submit))
    return jsonify({'ok': True, 'length': len(text), 'submit': submit})

