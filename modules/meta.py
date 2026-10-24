# -*- coding: utf-8 -*-
"""版本、运行状态与诊断 — /api/version、/api/diagnostics。

BUILD 同时用于页面静态资源缓存戳 (HTML 里的 __BUILD__ 占位)。
"""
import datetime
import io
import os
import platform
import subprocess
import sys
import time
import zipfile

import psutil
from flask import jsonify, send_file

import config
import modules.internal_api as internal_api

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STARTED_AT = time.time()


def _build():
    # prod 部署时写入 .deploy-version：git 在 prod 不更新，靠它保证每次发布
    # 静态资源 URL 的 ?v= 都变化，避免 immutable 缓存把新 JS 挡在门外
    try:
        with open(os.path.join(BASE_DIR, '.deploy-version'), encoding='utf-8') as f:
            stamp = f.read().strip()
        if stamp:
            return stamp
    except OSError:
        pass
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                             cwd=BASE_DIR, capture_output=True, text=True,
                             timeout=3, creationflags=0x08000000)
        value = (out.stdout or '').strip()
        if value:
            dirty = subprocess.run(['git', 'status', '--porcelain'],
                                   cwd=BASE_DIR, capture_output=True, text=True,
                                   timeout=3, creationflags=0x08000000)
            if (dirty.stdout or '').strip():
                # dev 每次启动换一个缓存戳，避免浏览器/Service Worker 拿着旧 JS 测新代码
                value += '-dirty-' + str(int(STARTED_AT))
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return str(int(os.path.getmtime(os.path.join(BASE_DIR, 'app.py'))))
    except OSError:
        return 'dev'


BUILD = _build()


def _daemons():
    return internal_api.services(timeout=1.5)


def _tail_errors(path, limit=5, maxBytes=256 * 1024):
    if not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            if size > maxBytes:
                f.seek(size - maxBytes)
            lines = f.readlines()[-400:]
    except OSError:
        return []
    hits = [ln.rstrip() for ln in lines if ('Traceback' in ln or ' ERROR ' in ln
                                            or ln.startswith('ERROR'))]
    return hits[-limit:]


def _ports():
    out = []
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                out.append(conn.laddr.port)
    except (psutil.AccessDenied, OSError):
        return []
    return sorted(set(out))


def register(app):
    @app.route('/api/version')
    def version():
        return jsonify({
            'build': BUILD,
            'startedAt': datetime.datetime.fromtimestamp(STARTED_AT)
                                    .strftime('%Y-%m-%d %H:%M:%S'),
            'uptimeSeconds': int(time.time() - STARTED_AT),
            'python': platform.python_version(),
            'proto': internal_api.PROTO,
            'daemons': _daemons(),
        })

    @app.route('/api/backup')
    def backup():
        """config.json + 状态目录 (跳过缩略图/锁文件/超大文件) 打包下载。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            cfg = os.path.join(BASE_DIR, 'config.json')
            if os.path.isfile(cfg):
                z.write(cfg, 'config.json')
            stateDir = config.stateDir()
            for dirpath, dirs, files in os.walk(stateDir):
                dirs[:] = [d for d in dirs if d not in ('thumbs', '__pycache__')]
                for name in files:
                    if name.endswith('.lock'):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        if os.path.getsize(full) > 20 * 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    rel = os.path.relpath(full, stateDir)
                    z.write(full, os.path.join('state', rel))
        buf.seek(0)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M')
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name='web-platform-backup-%s.zip' % stamp)

    @app.route('/api/diagnostics')
    def diagnostics():
        mem = psutil.virtual_memory()
        logs = {}
        for name in ('run.log', 'locald.log', 'sshd.log', 'watchdog.log'):
            path = os.path.join(BASE_DIR, name)
            if os.path.isfile(path):
                logs[name] = {'size': os.path.getsize(path),
                              'errors': _tail_errors(path)}
        sessions = {}
        for kind in ('local', 'ssh'):
            health = internal_api.health(kind, timeout=1.5)
            sessions[kind] = (health or {}).get('sessions')
        return jsonify({
            'build': BUILD,
            'python': sys.version,
            'platform': platform.platform(),
            'pid': os.getpid(),
            'uptimeSeconds': int(time.time() - STARTED_AT),
            'memory': {'total': mem.total, 'available': mem.available,
                       'percent': mem.percent},
            'disks': [{'path': p.device, 'free': psutil.disk_usage(p.mountpoint).free}
                      for p in psutil.disk_partitions(all=False)
                      if p.fstype and 'cdrom' not in p.opts],
            'ports': _ports(),
            'daemons': _daemons(),
            'sessions': sessions,
            'logs': logs,
            'stateDir': config.stateDir(),
        })