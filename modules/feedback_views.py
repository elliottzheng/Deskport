# -*- coding: utf-8 -*-
"""反馈的读取视图（批次 F1）：列表/详情/「我的反馈」/统计。

- summary：一条记录的展示视图（信封摊平 + submitter + 评论 + 未读 + 超时标记）；
- search：处理端检索，按状态/路由/提交者/归属/日期/超时筛选，待受理优先；
- mine：只列当前身份自己的（规格 §6.3，v1 实现列出全部是缺陷），带未读总数；
- stats：运维计数（保留 v1 形状，加待受理/超时两个数）。

写操作（流转/评论/批量/自动确认）在 feedback_ops。
"""
import os

from modules import feedback_store as store


def summary(item):
    """列表/详情用的视图：信封摊平 + 状态 + 评论线程 + 未读 + 超时标记。"""
    env = item.get('envelope') or {}
    submitter = store.submitterOfRecord(item)
    atts = []
    for att in env.get('attachments') or []:
        fileId = att.get('file_id')
        atts.append({'type': att.get('type') or 'screenshot', 'file_id': fileId,
                     'has_annotations': bool(att.get('has_annotations')),
                     'source': att.get('source') or 'page',
                     'url': '/api/feedback/attachments/' + fileId})
    return {
        'feedback_id': item.get('feedback_id'),
        'status': item.get('status') or 'submitted',
        'status_note': item.get('status_note') or '',
        'submitted_at': item.get('submitted_at'),
        'received_at': item.get('received_at'),
        'submitter': submitter,
        'user_id': store.submitterIdOf(item),
        'route': item.get('route'),
        'page_id': item.get('page_id'),
        'message': item.get('message'),
        'contact': env.get('contact') or '',
        'capture': env.get('capture') or {},
        'refs': env.get('refs') or {},
        'client': env.get('client') or {},
        'context': env.get('context') or {},
        'trace': env.get('trace') or [],
        'errors': env.get('errors') or {},
        'attachments': atts,
        'history': item.get('history') or [],
        'comments': item.get('comments') or [],
        'unread': bool(item.get('unread')),
        'overdue': store.isOverdue(item),
    }


def detail(item):
    """详情 = summary + 原始信封全文（只读，处理端要看「所见即所传」的那一封）。"""
    view = summary(item)
    view['envelope'] = item.get('envelope') or {}
    return view


def get(feedbackId):
    with store.locked():
        for item in store.load()['items']:
            if item.get('feedback_id') == feedbackId:
                return detail(item)
    return None


def search(route='', dateFrom='', dateTo='', status='', userId='', group='',
           submitter='', overdue=False, page=1, size=20):
    """处理端检索（规格 §6.1）。route 子串；日期按 submitted_at 前缀（ISO 可直接比）；
    submitter 按 id/显示名子串；group 精确；overdue 只要超 24h 未受理的。
    排序：待受理（submitted）优先，再按 submitted_at 倒序。"""
    with store.locked():
        items = list(store.load()['items'])
    if route:
        needle = route.lower()
        items = [i for i in items if needle in (i.get('route') or '').lower()]
    if status:
        items = [i for i in items if (i.get('status') or 'submitted') == status]
    if userId:
        items = [i for i in items if store.submitterIdOf(i) == userId]
    if group:
        items = [i for i in items
                 if (store.submitterOfRecord(i).get('group') or '') == group]
    if submitter:
        needle = submitter.lower()
        items = [i for i in items
                 if needle in store.submitterIdOf(i).lower()
                 or needle in (store.submitterOfRecord(i).get('display_name') or '').lower()]
    if overdue:
        items = [i for i in items if store.isOverdue(i)]
    if dateFrom:
        items = [i for i in items if (i.get('submitted_at') or '') >= dateFrom]
    if dateTo:
        items = [i for i in items if (i.get('submitted_at') or '') <= dateTo]
    # 稳定排序两次：先时间倒序，再让 submitted 浮到最前（组内保持时间倒序）
    items.sort(key=lambda i: i.get('submitted_at') or '', reverse=True)
    items.sort(key=lambda i: (i.get('status') or 'submitted') != 'submitted')
    total = len(items)
    page = max(1, page)
    size = max(1, min(100, size))
    chunk = items[(page - 1) * size:page * size]
    return {'total': total, 'page': page, 'size': size,
            'items': [summary(i) for i in chunk]}


def mine(identity):
    """「我的反馈」：只列当前身份自己的（规格 §6.3）。"""
    with store.locked():
        items = [i for i in store.load()['items']
                 if store.sameSubmitter(store.submitterOfRecord(i), identity)]
    items.sort(key=lambda i: i.get('submitted_at') or '', reverse=True)
    unread = sum(1 for i in items if i.get('unread'))
    return {'total': len(items), 'unread': unread,
            'items': [summary(i) for i in items]}


def markMineRead(identity):
    """打开即清（规格 §6.3）：清掉当前身份所有未读。返回清了几条。"""
    cleared = 0
    with store.locked():
        data = store.load()
        for item in data['items']:
            if item.get('unread') \
                    and store.sameSubmitter(store.submitterOfRecord(item), identity):
                store.clearUnread(item)
                cleared += 1
        if cleared:
            store.save(data)
    return {'cleared': cleared, 'unread': 0}


def stats():
    """运维视图：条数、状态分布、附件占用（v1 形状）+ 待受理/超时数。"""
    with store.locked():
        items = store.load()['items']
    counts = {}
    for item in items:
        key = item.get('status') or '?'
        counts[key] = counts.get(key, 0) + 1
    total = 0
    attachDir = store.attachDir()
    try:
        names = os.listdir(attachDir)
    except OSError:
        names = []
    for name in names:
        try:
            total += os.path.getsize(os.path.join(attachDir, name))
        except OSError:
            pass
    return {'count': len(items), 'byStatus': counts,
            'pending': counts.get('submitted', 0),
            'overdue': sum(1 for i in items if store.isOverdue(i)),
            'attachmentBytes': total, 'attachmentCount': len(names)}
