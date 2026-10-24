# -*- coding: utf-8 -*-
"""统一事件流 — 登录、危险操作、发布、守护进程启停都写同一个 JSONL。

文件: %APPDATA%\\<stateDir>\\control_audit.jsonl (沿用旧文件名, 控制台直接读)。
"""
import datetime
import json
import logging
import os
from collections import deque

import config
from modules import state

logger = logging.getLogger('events')


def eventPath():
    return config.statePath('control_audit.jsonl')


def log(action, target='', result='ok', detail='', ip=''):
    entry = {
        'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,
        'target': str(target)[:300],
        'result': result,
        'detail': str(detail)[:300],
        'ip': ip,
    }
    try:
        state.appendJsonl(eventPath(), entry)
    except OSError as e:
        logger.warning('append audit failed action=%s: %s', action, e)
    return entry


def recent(limit=60):
    path = eventPath()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = deque(f, maxlen=limit)
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError):
        return []