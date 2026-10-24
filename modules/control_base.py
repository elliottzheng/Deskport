# -*- coding: utf-8 -*-
"""控制台共享：blueprint、审计写入与读取。"""
import logging

from flask import Blueprint, has_request_context, request

import modules.events as events

blueprint = Blueprint('control', __name__)
log = logging.getLogger('control')
AUDIT_LIMIT = 60


def _audit(action, target='', result='ok', detail=''):
    """写审计。审计是旁路：任何失败都只记 warning，绝不阻断已执行的动作。"""
    try:
        ip = request.remote_addr if has_request_context() else ''
        return events.log(action, target, result, detail, ip)
    except Exception:
        log.warning('audit failed action=%s target=%s', action, target, exc_info=True)
        return None


def _readAudit():
    return events.recent(AUDIT_LIMIT)


# ---------------- 系统摘要 ----------------