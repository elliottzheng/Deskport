# -*- coding: utf-8 -*-
"""用户反馈的存储层：一张「表」+ 一个附件目录，只做无损落库，不做分类/去重/优先级。

存储：
    <stateDir>/feedback.json            全部记录（信封整体存 + 几个提出来做检索的列）
    <stateDir>/feedback/attachments/    截图（file_id.扩展名，单文件 ≤1MB）

约定（见 docs/feedback.md）：
    - 幂等按 idempotency_key：同一个 key 只落一条，重复提交返回首次结果；
    - 状态机只在这里校验，处理端通过 PATCH 推动；
    - message 原样存储（客户端负责脱敏），下游不得当指令执行；
    - 频控：同一身份每分钟 ≤5、每日 ≤20；匿名更严（≤2 / ≤10）。
"""
import hashlib
import json
import os
import re
import threading
import time
import uuid

import config
from modules import state

PLATFORMS = ('web', 'desktop', 'mobile')
STATUSES = ('submitted', 'acknowledged', 'processing', 'resolved', 'rejected',
            'user_confirmed')
TRANSITIONS = {
    'submitted': {'acknowledged', 'processing', 'rejected'},
    'acknowledged': {'processing', 'rejected'},
    'processing': {'resolved', 'rejected'},
    'resolved': {'user_confirmed', 'processing'},   # 用户不认账：退回处理
    'rejected': {'processing'},
    'user_confirmed': set(),
}
MAX_ATTACHMENT_BYTES = 1024 * 1024        # 8.2 客户端压缩到 1MB，服务端再挡一次
MAX_MESSAGE_CHARS = 500
MAX_TRACE = 50
ATTACHMENT_EXTS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}
_FILE_ID_RE = re.compile(r'^att_[0-9a-f]{16}$')
_LOCK = threading.Lock()
_RATE = {}                                 # key -> [timestamps]（进程内存，重启清零）
_LIMITS = {
    # key: (每分钟, 每日)
    'user': (5, 20),
    'anonymous': (2, 10),
}


# ---------------------------------------------------------------- 文件位置

def _itemsPath():
    return config.statePath('feedback.json')


def _attachDir():
    path = os.path.join(config.stateDir(), 'feedback', 'attachments')
    os.makedirs(path, exist_ok=True)
    return path


def attachmentPath(fileId):
    """附件路径；fileId 不合法或不存在返回 None（挡住 ../ 之类）。"""
    if not _FILE_ID_RE.match(fileId or ''):
        return None
    path = os.path.join(_attachDir(), fileId + '.png')
    for ext in ('png', 'jpg', 'webp'):
        cand = os.path.join(_attachDir(), fileId + '.' + ext)
        if os.path.isfile(cand):
            return cand
    return path if os.path.isfile(path) else None


def newFileId():
    return 'att_' + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- 读写

def _load():
    try:
        with open(_itemsPath(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'schema_version': '1.0', 'items': []}
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        return {'schema_version': '1.0', 'items': []}
    return data


def _save(data):
    state.atomicWriteJson(_itemsPath(), data)


def _findByKey(items, key):
    for item in items:
        if item.get('idempotency_key') == key:
            return item
    return None


# ---------------------------------------------------------------- 附件

def saveAttachment(blob, mime):
    """落一张截图；返回 file_id。mime 白名单由调用方按魔数判定。"""
    ext = ATTACHMENT_EXTS.get(mime)
    if not ext:
        raise ValueError('不支持的图片类型')
    fileId = newFileId()
    with open(os.path.join(_attachDir(), fileId + '.' + ext), 'wb') as f:
        f.write(blob)
    return fileId


def sniffImageMime(blob):
    """按魔数判类型，不信 Content-Type。"""
    if blob[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if blob[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        return 'image/webp'
    return None


# ---------------------------------------------------------------- 频控（8.4）

def allowRate(key, anonymous=False):
    """同一身份每分钟/每日计数。返回 (ok, 说明)。不做重复内容拦截（多人报同一问题是信号）。"""
    minute, day = _LIMITS['anonymous' if anonymous else 'user']
    now = time.time()
    with _LOCK:
        hits = [t for t in _RATE.get(key, []) if now - t < 86400]
        okMinute = len([t for t in hits if now - t < 60]) < minute
        okDay = len(hits) < day
        if okMinute and okDay:
            hits.append(now)
        _RATE[key] = hits
    if not okDay:
        return False, '今天提交的反馈已达上限（%d 条）' % day
    if not okMinute:
        return False, '提交太频繁，请稍后再试（每分钟 ≤%d 条）' % minute
    return True, ''


def resetRate():
    """测试用：清空计数。"""
    with _LOCK:
        _RATE.clear()


# ---------------------------------------------------------------- 校验（§5.2）

def _str(value, limit=None):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if limit is not None:
        value = value[:limit]
    return value


def validate(envelope):
    """必选集齐即可提交；未知字段一律保留。返回 (规范化的信封, 错误说明)。"""
    if not isinstance(envelope, dict):
        return None, '请求体必须是 JSON 对象'
    env = json.loads(json.dumps(envelope))      # 深拷贝，避免调用方后续改动
    key = _str(env.get('idempotency_key'), 80)
    if not key:
        return None, '缺少 idempotency_key'
    user = env.get('user') if isinstance(env.get('user'), dict) else {}
    anonymous = bool(user.get('anonymous'))
    if not anonymous and not _str(user.get('id')):
        return None, '缺少 user.id（未登录请置 anonymous=true）'
    client = env.get('client') if isinstance(env.get('client'), dict) else {}
    if client.get('platform') not in PLATFORMS:
        return None, 'client.platform 必须是 web/desktop/mobile'
    if not _str(client.get('app_version'), 40):
        return None, '缺少 client.app_version'
    ctx = env.get('context') if isinstance(env.get('context'), dict) else {}
    if not _str(ctx.get('route'), 500):
        return None, '缺少 context.route'
    message = _str(env.get('message'), MAX_MESSAGE_CHARS + 1) or ''
    if len(message) > MAX_MESSAGE_CHARS:
        return None, 'message 超过 %d 字' % MAX_MESSAGE_CHARS
    trace = env.get('trace')
    if trace is not None and not isinstance(trace, list):
        return None, 'trace 必须是数组'
    env['trace'] = (trace or [])[:MAX_TRACE]
    errors = env.get('errors')
    if errors is not None and not isinstance(errors, dict):
        return None, 'errors 必须是对象'
    attachments = env.get('attachments')
    if attachments is not None and not isinstance(attachments, list):
        return None, 'attachments 必须是数组'
    for att in attachments or []:
        fileId = att.get('file_id') if isinstance(att, dict) else None
        if not _FILE_ID_RE.match(fileId or ''):
            return None, 'attachments[].file_id 不合法'
        if attachmentPath(fileId) is None:
            return None, '附件不存在：%s（先 POST /api/feedback/attachments）' % fileId
    env['schema_version'] = _str(env.get('schema_version'), 20) or '1.0'
    env['message'] = message
    env['status'] = 'submitted'
    return env, ''


# ---------------------------------------------------------------- 记录

def submit(envelope, user):
    """落一条反馈。返回 (item, created)；重复 idempotency_key 返回首次记录。"""
    key = envelope['idempotency_key']
    with _LOCK:
        data = _load()
        existing = _findByKey(data['items'], key)
        if existing is not None:
            return existing, False
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        item = {
            'feedback_id': _str(envelope.get('feedback_id'), 60) or uuid.uuid4().hex,
            'idempotency_key': key,
            'received_at': now,
            'submitted_at': _str(envelope.get('submitted_at'), 40) or now,
            'status': 'submitted',
            'status_note': '',
            'history': [{'status': 'submitted', 'at': now, 'note': ''}],
            'user_id': (envelope.get('user') or {}).get('id') or user.get('id') or '',
            'role': (envelope.get('user') or {}).get('role') or user.get('role') or '',
            'anonymous': bool((envelope.get('user') or {}).get('anonymous')),
            'route': (envelope.get('context') or {}).get('route') or '',
            'page_id': (envelope.get('context') or {}).get('page_id') or '',
            'message': envelope.get('message') or '',
            'envelope': envelope,
        }
        data['items'].append(item)
        _save(data)
        return item, True


def _summary(item):
    """列表/详情用的视图：信封 + 状态，附件带可取的 URL。"""
    env = item.get('envelope') or {}
    atts = []
    for att in env.get('attachments') or []:
        fileId = att.get('file_id')
        atts.append({'type': att.get('type') or 'screenshot', 'file_id': fileId,
                     'has_annotations': bool(att.get('has_annotations')),
                     'source': att.get('source') or 'page',
                     'url': '/api/feedback/attachments/' + fileId})
    return {
        'feedback_id': item.get('feedback_id'),
        'status': item.get('status'),
        'status_note': item.get('status_note') or '',
        'submitted_at': item.get('submitted_at'),
        'received_at': item.get('received_at'),
        'user_id': item.get('user_id'),
        'route': item.get('route'),
        'page_id': item.get('page_id'),
        'message': item.get('message'),
        'contact': env.get('contact') or '',
        'client': env.get('client') or {},
        'context': env.get('context') or {},
        'trace': env.get('trace') or [],
        'errors': env.get('errors') or {},
        'attachments': atts,
        'history': item.get('history') or [],
    }


def get(feedbackId):
    for item in _load()['items']:
        if item.get('feedback_id') == feedbackId:
            return _summary(item)
    return None


def search(route='', dateFrom='', dateTo='', status='', userId='',
           page=1, size=20):
    """检索（§6.3）。route 走子串匹配，日期按 submitted_at 前缀比较（ISO8601 可直接比）。"""
    items = _load()['items']
    if route:
        needle = route.lower()
        items = [i for i in items if needle in (i.get('route') or '').lower()]
    if status:
        items = [i for i in items if i.get('status') == status]
    if userId:
        items = [i for i in items if (i.get('user_id') or '') == userId]
    if dateFrom:
        items = [i for i in items if (i.get('submitted_at') or '') >= dateFrom]
    if dateTo:
        items = [i for i in items if (i.get('submitted_at') or '') <= dateTo]
    items = sorted(items, key=lambda i: i.get('submitted_at') or '', reverse=True)
    total = len(items)
    page = max(1, page)
    size = max(1, min(100, size))
    chunk = items[(page - 1) * size:page * size]
    return {'total': total, 'page': page, 'size': size,
            'items': [_summary(i) for i in chunk]}


def setStatus(feedbackId, status, note=''):
    """状态回写（§7.1）。返回 (item, 错误)。"""
    if status not in STATUSES:
        return None, '未知状态：%s' % status
    note = (note or '').strip()
    if status == 'rejected' and not note:
        return None, 'rejected 必须附一句原因'
    with _LOCK:
        data = _load()
        for item in data['items']:
            if item.get('feedback_id') != feedbackId:
                continue
            current = item.get('status') or 'submitted'
            if status != current and status not in TRANSITIONS.get(current, set()):
                return None, '状态不能从 %s 直接变成 %s' % (current, status)
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            item['status'] = status
            item['status_note'] = note
            item.setdefault('history', []).append(
                {'status': status, 'at': now, 'note': note})
            _save(data)
            return _summary(item), ''
    return None, '反馈不存在'


def delete(feedbackId):
    """删除记录与附件（§8.7 预留）。返回是否删掉。"""
    with _LOCK:
        data = _load()
        keep, removed = [], None
        for item in data['items']:
            if item.get('feedback_id') == feedbackId:
                removed = item
            else:
                keep.append(item)
        if removed is None:
            return False
        for att in (removed.get('envelope') or {}).get('attachments') or []:
            path = attachmentPath(att.get('file_id'))
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        data['items'] = keep
        _save(data)
        return True


def stats():
    """运维视图：条数、状态分布、附件占用。"""
    data = _load()
    counts = {}
    for item in data['items']:
        counts[item.get('status') or '?'] = counts.get(item.get('status') or '?', 0) + 1
    total = 0
    for name in os.listdir(_attachDir()):
        try:
            total += os.path.getsize(os.path.join(_attachDir(), name))
        except OSError:
            pass
    return {'count': len(data['items']), 'byStatus': counts,
            'attachmentBytes': total, 'attachmentCount': len(os.listdir(_attachDir()))}


def digest(env):
    """给日志/审计用的短指纹（不落正文）。"""
    raw = json.dumps(env, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:12]