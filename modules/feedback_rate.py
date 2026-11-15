# -*- coding: utf-8 -*-
"""反馈频控（规格 §4.3）：计数落状态目录小 JSON，重启不清零。

表（按服务端时间）：
    登录用户 / 智能体   每分钟 5、每日 20
    匿名（可信反代地址） 每分钟 2、每日 5
系统所有者（网页会话）与处理者不受限——单密码系统里这套限制实际会堵死主人自己
（调研 S8），调用方对所有者直接跳过本模块。

原子写（临时文件 + 替换）与进程内锁；跨进程还有 24h 自然过期兜底。
超限时 allowRate 返回 (False, 说明, retryAfter 秒)。
"""
import json
import os
import threading
import time

import config
from modules import state

_LIMITS = {
    # key: (每分钟, 每日)
    'user': (5, 20),
    'anonymous': (2, 5),
}
_WINDOW_DAY = 86400
_LOCK = threading.Lock()


def _path():
    return config.statePath('feedback-rate.json')


def _load():
    try:
        with open(_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(counts):
    state.atomicWriteJson(_path(), counts)


def _prune(hits, now):
    return [t for t in hits if isinstance(t, (int, float)) and now - t < _WINDOW_DAY]


def allowRate(key, anonymous=False):
    """记一次提交。返回 (ok, 说明, retryAfter)。

    ok=False 时 retryAfter 是建议等待秒数（窗口内最早一条 hit 过期还需多久）。
    """
    kind = 'anonymous' if anonymous else 'user'
    minute, day = _LIMITS[kind]
    now = time.time()
    with _LOCK:
        counts = _load()
        hits = _prune(counts.get(key) or [], now)
        inMinute = [t for t in hits if now - t < 60]
        if len(hits) >= day:
            counts[key] = hits
            _save(counts)
            return False, '今天提交的反馈已达上限（%d 条）' % day, \
                _retryAfter(hits, now, _WINDOW_DAY)
        if len(inMinute) >= minute:
            counts[key] = hits
            _save(counts)
            return False, '提交太频繁，请稍后再试（每分钟 ≤%d 条）' % minute, \
                _retryAfter(inMinute, now, 60)
        hits.append(now)
        counts[key] = hits
        _save(counts)
    return True, '', 0


def _retryAfter(hits, now, window):
    """窗口内最早的一条过期还要多久（秒，至少 1）。"""
    if not hits:
        return 1
    return max(1, int(hits[0] + window - now) + 1)


def resetRate():
    """测试用：清空计数（含落盘文件）。"""
    with _LOCK:
        try:
            os.unlink(_path())
        except OSError:
            pass
