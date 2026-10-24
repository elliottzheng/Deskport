# -*- coding: utf-8 -*-
"""看门狗 — 定期检查 web 与两个会话守护进程, 连续失败则用对应 vbs 拉起。

独立进程, 与 web/sessiond 互不依赖。检查间隔 30 秒, 连续 2 次失败才重启,
避免启动瞬间误判。日志写 watchdog.log (启动时按 10MB 轮转一次)。
"""
import datetime
import msvcrt
import os
import subprocess
import sys
import time

import requests

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, 'watchdog.log')
INTERVAL = 30
FAIL_THRESHOLD = 2

_lockHandle = None

CHECKS = [
    ('web', 'http://127.0.0.1:%d/api/auth/status', 'start-hidden.vbs'),
    ('local', 'http://127.0.0.1:%d/health', 'start-locald.vbs'),
    ('ssh', 'http://127.0.0.1:%d/health', 'start-sshd.vbs'),
]


def _checks():
    """端口从 config.json 读取，与 web/sessiond 保持单一来源。"""
    ports = {
        'web': config.getPort(),
        'local': config.getSessiondPort('local'),
        'ssh': config.getSessiondPort('ssh'),
    }
    return [(name, url % ports[name], vbs) for name, url, vbs in CHECKS]


def _rotate():
    try:
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 10 * 1024 * 1024:
            backup = LOG_PATH + '.1'
            if os.path.isfile(backup):
                os.remove(backup)
            os.replace(LOG_PATH, backup)
    except OSError:
        pass


def log(message):
    line = '%s %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message)
    print(line, flush=True)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def healthy(url):
    try:
        resp = requests.get(url, timeout=3)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def launch(vbs):
    path = os.path.join(BASE_DIR, vbs)
    if not os.path.isfile(path):
        log('[MISS] %s not found' % vbs)
        return
    try:
        subprocess.Popen(['wscript.exe', path], cwd=BASE_DIR,
                         creationflags=0x08000000)
        log('[START] %s' % vbs)
    except OSError as e:
        log('[FAIL] launch %s: %s' % (vbs, e))


def _singleInstance():
    """同一状态目录只允许一个 watchdog 实例 (文件锁)。句柄保存在模块全局, 进程存活期间不释放。"""
    global _lockHandle
    handle = open(config.statePath('watchdog.lock'), 'a+')
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        sys.exit(0)
    _lockHandle = handle


def main():
    _singleInstance()
    _rotate()
    log('[WATCHDOG] start pid=%d interval=%ds' % (os.getpid(), INTERVAL))
    failures = {name: 0 for name, _, _ in _checks()}
    while True:
        for name, url, vbs in _checks():
            if healthy(url):
                if failures[name]:
                    log('[OK] %s recovered' % name)
                failures[name] = 0
                continue
            failures[name] += 1
            log('[DOWN] %s (%d/%d)' % (name, failures[name], FAIL_THRESHOLD))
            if failures[name] >= FAIL_THRESHOLD:
                launch(vbs)
                failures[name] = 0
                time.sleep(8)     # 给被拉起进程一点启动时间
        time.sleep(INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)