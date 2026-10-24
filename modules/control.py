# -*- coding: utf-8 -*-
"""整机控制台 — 进程 / 窗口 / 应用 / 审计。"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

import psutil
from flask import jsonify, request

import config
import modules.internal_api as internal_api
from modules.control_base import blueprint, _audit, _readAudit
from modules import control_input  # noqa: F401  注册电源/按键/文本路由

_procCache = {'at': 0.0, 'items': []}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROTECTED_NAMES = {
    'system', 'registry', 'smss.exe', 'csrss.exe', 'wininit.exe', 'services.exe',
    'lsass.exe', 'winlogon.exe', 'fontdrvhost.exe', 'dwm.exe', 'explorer.exe',
    'sihost.exe', 'ctfmon.exe', 'taskhostw.exe', 'audiodg.exe', 'memcompression',
}
_appsCache = {'at': 0.0, 'apps': []}
_appsLock = threading.Lock()

def _disks():
    out = []
    for part in psutil.disk_partitions(all=False):
        if 'cdrom' in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        out.append({'path': part.device, 'total': usage.total,
                    'used': usage.used, 'free': usage.free,
                    'percent': usage.percent})
    return out


def _processes():
    """进程列表 (4 秒缓存)。

    psutil 在 Windows 上逐进程取 memory_info 约需 1.1-1.3s；控制台每 12s
    轮询一次、壳/旧页可能同时请求，缓存可避免重复开销。
    """
    now = time.time()
    if _procCache['items'] and now - _procCache['at'] < 4.0:
        return list(_procCache['items'])
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_percent']):
        try:
            info = p.info
            procs.append({
                'pid': info['pid'],
                'name': info['name'] or '',
                'mem': info['memory_info'].rss if info['memory_info'] else 0,
                'cpu': round(info['cpu_percent'] or 0, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    _procCache['at'] = now
    _procCache['items'] = procs
    return list(procs)


def _windows():
    """EnumWindows: 可见 + 有标题 + 非工具窗口。"""
    items = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title or title in ('Program Manager', 'Default IME', 'MSCTFIME UI'):
                return True
            exStyle = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if exStyle & 0x00000080:                    # WS_EX_TOOLWINDOW
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ''
            try:
                name = psutil.Process(pid.value).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            items.append({'hwnd': int(hwnd), 'title': title, 'pid': pid.value,
                          'process': name})
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return items


def _startMenuApps():
    now = time.time()
    with _appsLock:
        if now - _appsCache['at'] < 120 and _appsCache['apps']:
            return list(_appsCache['apps'])
    roots = [
        os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'),
                     r'Microsoft\Windows\Start Menu\Programs'),
        os.path.join(os.environ.get('APPDATA', ''),
                     r'Microsoft\Windows\Start Menu\Programs'),
    ]
    skip = ('uninstall', '卸载', 'readme', 'help', '帮助', 'website', '官网')
    seen, apps = set(), []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.lower().endswith(('.lnk', '.url')):
                    continue
                stem = os.path.splitext(name)[0]
                low = stem.lower()
                if any(k in low for k in skip) or stem in seen:
                    continue
                seen.add(stem)
                apps.append({'name': stem, 'path': os.path.join(dirpath, name)})
    apps.sort(key=lambda a: a['name'].lower())
    with _appsLock:
        _appsCache['at'] = time.time()
        _appsCache['apps'] = apps
    return list(apps)


def _pinned(apps):
    """pinnedApps 支持两种写法: 开始菜单显示名, 或直接的 .exe/.lnk 路径。"""
    pins = config.load().get('pinnedApps') or []
    byName = {a['name']: a for a in apps}
    out = []
    for pin in pins if isinstance(pins, list) else []:
        if not isinstance(pin, str) or not pin.strip():
            continue
        pin = pin.strip()
        if os.path.isfile(pin) and pin.lower().endswith(('.exe', '.lnk', '.url')):
            out.append({'name': os.path.splitext(os.path.basename(pin))[0], 'path': pin})
        elif pin in byName:
            out.append(byName[pin])
    return out


def _allowedAppPaths():
    paths = {a['path'] for a in _startMenuApps()}
    for pin in config.load().get('pinnedApps') or []:
        if isinstance(pin, str) and os.path.isfile(pin):
            paths.add(pin)
    return paths


def _services():
    services = [{'kind': 'web', 'ok': True, 'pid': os.getpid(),
                 'proto': internal_api.PROTO, 'sessions': None}]
    services.extend(internal_api.services(timeout=1))
    return services


@blueprint.route('/api/control/summary')
def summary():
    mem = psutil.virtual_memory()
    boot = int(time.time() - psutil.boot_time())
    apps = _startMenuApps()
    pins = _pinned(apps)
    pinnedNames = {a['name'] for a in pins}
    return jsonify({
        'services': _services(),
        'pcName': os.environ.get('COMPUTERNAME', ''),
        'user': os.environ.get('USERNAME', ''),
        'uptimeSeconds': boot,
        'cpu': psutil.cpu_percent(interval=None),
        'memory': {'total': mem.total, 'available': mem.available,
                   'percent': mem.percent},
        'disks': _disks(),
        'pinnedApps': pins,
        'apps': [a for a in apps if a['name'] not in pinnedNames][:60],
        'audit': _readAudit()[-20:],
    })


# ---------------- 进程 ----------------
@blueprint.route('/api/control/processes')
def processes():
    sort = request.args.get('sort') or 'mem'
    query = (request.args.get('q') or '').lower()
    procs = _processes()
    if query:
        procs = [p for p in procs if query in p['name'].lower()]
    key = 'cpu' if sort == 'cpu' else 'mem'
    procs.sort(key=lambda p: p[key], reverse=True)
    return jsonify({'processes': procs[:60], 'total': len(procs)})


@blueprint.route('/api/control/process/close', methods=['POST'])
def closeProcess():
    data = request.get_json(force=True, silent=True) or {}
    pid = data.get('pid')
    force = bool(data.get('force'))
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return jsonify({'error': 'pid 无效'}), 400
    if pid in (os.getpid(), os.getppid()):
        return jsonify({'error': '不能结束平台自身进程'}), 403
    try:
        p = psutil.Process(pid)
        name = p.name()
    except psutil.NoSuchProcess:
        return jsonify({'error': '进程不存在'}), 404
    if name.lower() in PROTECTED_NAMES:
        return jsonify({'error': '系统关键进程已保护: %s' % name}), 403
    try:
        if force:
            p.kill()
        else:
            p.terminate()
        p.wait(timeout=3)
    except psutil.TimeoutExpired:
        try:
            p.kill()
        except psutil.Error as e:
            _audit('process.close', '%s(%s)' % (name, pid), 'error', str(e))
            return jsonify({'error': '结束失败: %s' % e}), 500
    except psutil.Error as e:
        _audit('process.close', '%s(%s)' % (name, pid), 'error', str(e))
        return jsonify({'error': '结束失败: %s' % e}), 500
    _audit('process.close', '%s(%s)' % (name, pid), 'ok', 'force=%s' % force)
    return jsonify({'ok': True, 'name': name, 'pid': pid})


# ---------------- 窗口 ----------------
@blueprint.route('/api/control/windows')
def windows():
    return jsonify({'windows': _windows()})


@blueprint.route('/api/control/window', methods=['POST'])
def windowAction():
    data = request.get_json(force=True, silent=True) or {}
    action = data.get('action')
    try:
        hwnd = int(data.get('hwnd'))
    except (TypeError, ValueError):
        return jsonify({'error': 'hwnd 无效'}), 400
    if not user32.IsWindow(hwnd):
        return jsonify({'error': '窗口不存在'}), 404
    if action == 'focus':
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)     # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.SwitchToThisWindow(hwnd, True)
        _audit('window.focus', hwnd)
        return jsonify({'ok': True})
    if action == 'minimize':
        user32.ShowWindow(hwnd, 6)         # SW_MINIMIZE
        _audit('window.minimize', hwnd)
        return jsonify({'ok': True})
    if action == 'close':
        user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        _audit('window.close', hwnd)
        return jsonify({'ok': True})
    return jsonify({'error': '未知动作'}), 400


# ---------------- 应用 ----------------
@blueprint.route('/api/control/launch', methods=['POST'])
def launch():
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get('path') or '').strip()
    if path not in _allowedAppPaths():
        return jsonify({'error': '只允许启动开始菜单或 pinnedApps 里的程序'}), 403
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        os.startfile(path)   # noqa: S606
    except OSError as e:
        _audit('app.launch', name, 'error', str(e))
        return jsonify({'error': '启动失败: %s' % e}), 500
    _audit('app.launch', name)
    return jsonify({'ok': True, 'name': name})


# ---------------- 电源 / 音量 / 文本 ----------------

@blueprint.route('/api/control/audit')
def auditList():
    return jsonify({'entries': _readAudit()})