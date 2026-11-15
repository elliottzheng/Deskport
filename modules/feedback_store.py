# -*- coding: utf-8 -*-
"""用户反馈的存储层（批次 F1，规格 v2）：一张「表」+ 一个附件目录，只做无损落库。

存储：
    <stateDir>/feedback.json            全部记录（信封整体存 + 提出来做检索的列）
    <stateDir>/feedback/attachments/    截图（file_id.扩展名，单文件 ≤1MB）

约定（见 docs/feedback.md）：
    - submitter 由服务端按会话/令牌写（规格 §1/§2），信封里自报身份一概无效；
    - 幂等作用域 = (submitter.id, idempotency_key)，feedback_id 撞号 409；
    - 状态机规则在 feedback_status（纯函数），落库在 feedback_ops；
    - message 与评论原样存储（客户端负责脱敏），下游不得当指令执行；
    - v1 老记录照常读出：submitter 按 user_id/role 合成（归属字段补不回来）。

读取视图（summary/mine/search）在 feedback_views；写操作（流转/评论/批量/自动确认）
在 feedback_ops。本模块只做「表」原语与提交。
"""
import hashlib
import json
import os
import threading
import time
import uuid

import config
from modules import feedback_status
from modules import state

OWNER_ID = 'owner'                     # 单密码系统：登录者就是这台机器的主人
OWNER_DISPLAY = '主人'
MAX_ATTACHMENT_BYTES = 1024 * 1024     # 客户端压缩到 1MB，服务端再挡一次
ATTACHMENT_EXTS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}
_LOCK = threading.Lock()
_clock = time.time                     # 可注入时钟（测试自动确认/超时用）


def setClock(fn):
    """注入时钟（epoch 秒）。测试用；生产环境不调用。"""
    global _clock
    _clock = fn


def nowStamp():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_clock()))


def nowSeconds():
    return _clock()


# ---------------------------------------------------------------- 文件位置

def _itemsPath():
    return config.statePath('feedback.json')


def attachDir():
    """附件目录（不存在则建）。公开：views/stats 要列举它。"""
    path = os.path.join(config.stateDir(), 'feedback', 'attachments')
    os.makedirs(path, exist_ok=True)
    return path


def _attachDir():
    return attachDir()


def attachmentPath(fileId):
    """附件路径；fileId 不合法或不存在返回 None（挡住 ../ 之类）。"""
    if not isinstance(fileId, str) or not fileId.startswith('att_') \
            or len(fileId) != 20 or any(c not in '0123456789abcdef' for c in fileId[4:]):
        return None
    for ext in ('png', 'jpg', 'webp'):
        cand = os.path.join(_attachDir(), fileId + '.' + ext)
        if os.path.isfile(cand):
            return cand
    return None


def newFileId():
    return 'att_' + uuid.uuid4().hex[:16]


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


# ---------------------------------------------------------------- 读写（公开原语）

def locked():
    """跨模块写锁：ops/views 的「读-改-写」横亘多次文件操作，必须同锁完成。"""
    return _LOCK


def load():
    """读全表。调用方持 locked() 时用；结构异常按空表处理（不静默丢数据）。"""
    try:
        with open(_itemsPath(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'schema_version': '1.0', 'items': []}
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        return {'schema_version': '1.0', 'items': []}
    return data


def save(data):
    state.atomicWriteJson(_itemsPath(), data)


# ---------------------------------------------------------------- 身份（服务端定）

def submitterOf(g):
    """当前请求的 submitter（规格 §2）：令牌 = 智能体实名；会话 = 机器主人。

    网页会话：kind=human, id=owner。智能体令牌：kind=agent, id=令牌名，
    group=令牌绑定的主管 id，on_behalf_of 照记（调研 S5：归属信息不再丢失）。
    """
    name = getattr(g, 'agentTokenName', '') or ''
    if name:
        return {'kind': 'agent', 'id': name, 'display_name': name,
                'on_behalf_of': str(getattr(g, 'agentOnBehalfOf', '') or ''),
                'group': str(getattr(g, 'agentLead', '') or '')}
    return {'kind': 'human', 'id': OWNER_ID, 'display_name': OWNER_DISPLAY,
            'on_behalf_of': '', 'group': ''}


def submitterOfRecord(item):
    """记录里的 submitter；v1 老记录按 user_id/role 合成（9 条线上数据照常读）。"""
    existing = item.get('submitter')
    if isinstance(existing, dict) and existing.get('id'):
        return {'kind': existing.get('kind') or 'human',
                'id': existing.get('id') or '',
                'display_name': existing.get('display_name') or existing.get('id') or '',
                'on_behalf_of': existing.get('on_behalf_of') or '',
                'group': existing.get('group') or ''}
    uid = str(item.get('user_id') or '')
    if str(item.get('role') or '') == 'agent':
        # v1 智能体记录：id 是令牌名；group/on_behalf_of 线上已丢失，补不回来
        return {'kind': 'agent', 'id': uid, 'display_name': uid,
                'on_behalf_of': '', 'group': ''}
    return {'kind': 'human', 'id': uid or OWNER_ID, 'display_name': OWNER_DISPLAY,
            'on_behalf_of': '', 'group': ''}


def submitterIdOf(item):
    return submitterOfRecord(item).get('id') or ''


def sameSubmitter(recordSubmitter, identity):
    return (recordSubmitter.get('kind') == identity.get('kind')
            and recordSubmitter.get('id') == identity.get('id'))


# ---------------------------------------------------------------- 提交

def submit(envelope, submitter):
    """落一条反馈。返回 (item, created, err)。

    err 为 (httpCode, 说明)：feedback_id 撞号 409；附件不存在/被占用 400。
    幂等：同 (submitter.id, idempotency_key) 重复提交返回首次记录（created=False）；
    不同身份用同一个键各落一条（规格 §4.2，v1 的全局键会静默吞反馈）。
    """
    sid = submitter.get('id') or ''
    key = envelope['idempotency_key']
    fid = envelope.get('feedback_id') or ''
    with _LOCK:
        data = load()
        for item in data['items']:
            if item.get('idempotency_key') == key \
                    and submitterIdOf(item) == sid:
                return item, False, None
        if fid:
            for item in data['items']:
                if item.get('feedback_id') == fid:
                    return None, False, (409, 'feedback_id 已存在：%s' % fid)
        used = _usedFileIds(data['items'])
        for att in envelope.get('attachments') or []:
            fileId = att.get('file_id')
            if attachmentPath(fileId) is None:
                return None, False, (400, '附件不存在：%s（先 POST /api/feedback/attachments）' % fileId)
            if fileId in used:
                return None, False, (400, '附件已被其他反馈引用：%s' % fileId)
        now = nowStamp()
        envelope['received_at'] = now
        envelope['submitter'] = {k: submitter.get(k, '') for k in
                                 ('kind', 'id', 'display_name', 'on_behalf_of', 'group')}
        ctx = envelope.get('context') or {}
        item = {
            'feedback_id': fid or uuid.uuid4().hex,
            'idempotency_key': key,
            'received_at': now,
            'submitted_at': (envelope.get('submitted_at') or '').strip() or now,
            'status': 'submitted',
            'status_note': '',
            'submitter': envelope['submitter'],
            'user_id': sid,                      # 索引列（v1 检索与迁移读取）
            'role': 'agent' if submitter.get('kind') == 'agent' else 'owner',
            'anonymous': False,
            'route': ctx.get('route') or '',
            'page_id': ctx.get('page_id') or '',
            'message': envelope.get('message') or '',
            'comments': [],
            'unread': False,
            'history': [{'from': '', 'to': 'submitted', 'actor': 'submitter',
                         'reason': '', 'at': now}],
            'envelope': envelope,
        }
        data['items'].append(item)
        save(data)
        return item, True, None


def _usedFileIds(items):
    used = set()
    for item in items:
        for att in (item.get('envelope') or {}).get('attachments') or []:
            if isinstance(att, dict) and att.get('file_id'):
                used.add(att['file_id'])
    return used


# ---------------------------------------------------------------- history / 未读

def appendHistory(item, frm, to, actor, reason, at):
    """history 条目形状按规格 §5：{from, to, actor, reason, at}。

    v1 老条目是 {status, at, note}，读取方两种都认（feedback_status.resolvedAt）。
    """
    item.setdefault('history', []).append(
        {'from': frm, 'to': to, 'actor': actor, 'reason': reason, 'at': at})


def markUnread(item):
    """提交者未读 +1（规格 §6.3）：状态变化或处理者评论时由 ops 调。"""
    item['unread'] = True


def isOverdue(item, now=None):
    """待受理超时（>24h 无人 acknowledged）。规则在 feedback_status，这里转公开
    接口给 views/summary 用——读取方只跟 store 说话，不横向 import 规则模块。"""
    return feedback_status.isOverdue(item, now)


def clearUnread(item):
    item['unread'] = False


# ---------------------------------------------------------------- 删除 / 摘要

def delete(feedbackId):
    """删除记录与附件。返回是否删掉。"""
    with _LOCK:
        data = load()
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
        save(data)
        return True


def pendingSummary(now=None):
    """只读摘要，供指挥台「等你处理」接入（批次 F1 第 9 项）。

    待受理 = status submitted；超时未受理 = submitted 且超过 24h 无人 acknowledged。
    recent 是最近 5 条的展示用摘要（message 截 50 字，只给处理者看）。
    """
    now = nowSeconds() if now is None else now
    items = load()['items']
    pending = [i for i in items if (i.get('status') or 'submitted') == 'submitted']
    overdue = [i for i in pending if feedback_status.isOverdue(i, now)]
    recent = sorted(items, key=lambda i: i.get('received_at') or '', reverse=True)[:5]
    return {'pending': len(pending), 'overdue': len(overdue),
            'recent': [_brief(i) for i in recent]}


def _brief(item):
    return {
        'feedback_id': item.get('feedback_id'),
        'status': item.get('status') or 'submitted',
        'route': item.get('route') or '',
        'submitter': submitterOfRecord(item),
        'received_at': item.get('received_at') or '',
        'submitted_at': item.get('submitted_at') or '',
        'message': (item.get('message') or '')[:50],
        'unread': bool(item.get('unread')),
    }


def digest(env):
    """给日志/审计用的短指纹（不落正文）。"""
    raw = json.dumps(env, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:12]
