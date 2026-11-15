# -*- coding: utf-8 -*-
"""反馈信封校验（规格 §2/§4.1）：必选性与长度，v1 照收、v2 认领。

纯函数：不碰磁盘、不认身份（身份一律服务端定，见 feedback_store.submitterOf）。
未知字段原样保留（整信封存进 envelope，便于前向演进）。

与规格的落地差别（写进 docs/feedback.md §八）：
    - feedback_id / submitted_at 缺省时服务端补（智能体技能包里的 curl 例子
      不带这两个字段，照收不 400）；
    - trace / errors 超长按"取最近"截断，不 400（规格只要求 ≤50 / ≤20 步）。
"""
import json
import re

PLATFORMS = ('web', 'desktop', 'mobile', 'agent')
MAX_MESSAGE_CHARS = 500
MAX_CONTACT_CHARS = 100
MAX_TRACE = 50
MAX_ERRORS = 20
MAX_ATTACHMENTS = 4
MAX_KEY_CHARS = 80
MAX_APP_VERSION_CHARS = 40
MAX_ROUTE_CHARS = 500
_FILE_ID_RE = re.compile(r'^att_[0-9a-f]{16}$')


def _str(value, limit=None):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if limit is not None:
        value = value[:limit]
    return value


def validate(envelope):
    """校验信封。返回 (规范化信封, 错误说明)；错误时空串说明字段名（规格 §4.1）。"""
    if not isinstance(envelope, dict):
        return None, '请求体必须是 JSON 对象'
    env = json.loads(json.dumps(envelope))      # 深拷贝，避免调用方后续改动
    key = _str(env.get('idempotency_key'), MAX_KEY_CHARS)
    if not key:
        return None, '缺少 idempotency_key'
    client = env.get('client') if isinstance(env.get('client'), dict) else {}
    if client.get('platform') not in PLATFORMS:
        return None, 'client.platform 必须是 web/desktop/mobile/agent'
    if not _str(client.get('app_version'), MAX_APP_VERSION_CHARS):
        return None, '缺少 client.app_version'
    ctx = env.get('context') if isinstance(env.get('context'), dict) else {}
    if not _str(ctx.get('route'), MAX_ROUTE_CHARS):
        return None, '缺少 context.route'
    message = _str(env.get('message'), MAX_MESSAGE_CHARS + 1) or ''
    if not message:
        return None, 'message 不能为空'
    if len(message) > MAX_MESSAGE_CHARS:
        return None, 'message 超过 %d 字' % MAX_MESSAGE_CHARS
    contact = env.get('contact')
    if contact is not None:
        if not isinstance(contact, str):
            return None, 'contact 必须是字符串'
        if len(contact.strip()) > MAX_CONTACT_CHARS:
            return None, 'contact 超过 %d 字' % MAX_CONTACT_CHARS
    trace = env.get('trace')
    if trace is not None and not isinstance(trace, list):
        return None, 'trace 必须是数组'
    env['trace'] = (trace or [])[-MAX_TRACE:]
    errors = env.get('errors')
    if errors is not None and not isinstance(errors, dict):
        return None, 'errors 必须是对象'
    if isinstance(errors, dict):
        for name in ('console', 'failed_requests'):
            if isinstance(errors.get(name), list):
                errors[name] = errors[name][-MAX_ERRORS:]
    attachments = env.get('attachments')
    if attachments is not None and not isinstance(attachments, list):
        return None, 'attachments 必须是数组'
    if len(attachments or []) > MAX_ATTACHMENTS:
        return None, 'attachments 一次最多 %d 个' % MAX_ATTACHMENTS
    for att in attachments or []:
        fileId = att.get('file_id') if isinstance(att, dict) else None
        if not _FILE_ID_RE.match(fileId or ''):
            return None, 'attachments[].file_id 不合法'
    if env.get('capture') is not None and not isinstance(env.get('capture'), dict):
        return None, 'capture 必须是对象'
    if env.get('refs') is not None and not isinstance(env.get('refs'), dict):
        return None, 'refs 必须是对象'
    env['schema_version'] = _str(env.get('schema_version'), 20) or '1.0'
    env['message'] = message
    env['contact'] = contact.strip() if isinstance(contact, str) else ''
    # 客户端自报的 user 一律无效：服务端按会话/令牌写 submitter（规格 §1/§2）
    env.pop('user', None)
    env.pop('status', None)
    env.pop('history', None)
    return env, ''


def fileIdOk(fileId):
    return bool(_FILE_ID_RE.match(fileId or ''))
