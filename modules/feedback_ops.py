# -*- coding: utf-8 -*-
"""反馈的写操作（批次 F1）：状态流转、评论线程、批量受理、自动确认。

规则判定全在 feedback_status（纯函数），本模块只做「找记录 → 校验 → 落库」：
    - transition：处理端 PATCH（actor=handler，网页会话）；
    - confirm / reopen：提交者本人（actor=submitter，resolved 之后的两种结局）；
    - addComment：处理者可追问、提交者可补充（只追加线程，不改状态）；
    - batchAcknowledge：批量受理（一批 submitted → acknowledged）；
    - sweepAutoConfirm：resolved 满 14 天系统自动确认（actor=system）。

未读规则（规格 §6.3）：别人（处理者/系统）动了这条 → 提交者未读 +1；
提交者自己的动作不加（他自己知道）。
"""
import uuid

from modules import feedback_store as store
from modules import feedback_status

MAX_COMMENT_CHARS = 1000
MAX_BATCH = 100


def _find(data, feedbackId):
    for item in data['items']:
        if item.get('feedback_id') == feedbackId:
            return item
    return None


def _isSelf(item, actorId):
    """这条是不是 actorId 自己提的（自己的评论不点亮自己的红点）。"""
    return store.submitterIdOf(item) == (actorId or '')


def _apply(item, target, reason, actor):
    """落一次流转：状态 + 最后一句说明 + history + 未读。调用方持锁。

    未读规则（规格 §6.3 / 批次 F1 第 6 项）：**状态变化一律**点亮提交者红点——
    含处理者对待自己提的那条（设置页与反馈面板是两个界面，流转完该在「我的反馈」
    里看到结果）。提交者自己的评论不点亮（ops.addComment 里判）。
    """
    current = item.get('status') or 'submitted'
    now = store.nowStamp()
    item['status'] = target
    item['status_note'] = reason
    store.appendHistory(item, current, target, actor, reason, now)
    store.markUnread(item)


def _owned(item, identity):
    return store.sameSubmitter(store.submitterOfRecord(item), identity)


# ---------------------------------------------------------------- 流转

def transition(feedbackId, target, reason=''):
    """处理端流转（PATCH /api/feedback/<id>/status）。返回 (item, httpCode, err)。"""
    with store.locked():
        data = store.load()
        item = _find(data, feedbackId)
        if item is None:
            return None, 404, '反馈不存在'
        current = item.get('status') or 'submitted'
        ok, code, err = feedback_status.checkTransition(
            current, target, 'handler', reason)
        if not ok:
            return None, code, err
        _apply(item, target, (reason or '').strip(), 'handler')
        store.save(data)
        return item, 0, ''


def confirm(feedbackId, identity):
    """提交者确认解决：resolved → user_confirmed。返回 (item, httpCode, err)。"""
    with store.locked():
        data = store.load()
        item = _find(data, feedbackId)
        if item is None:
            return None, 404, '反馈不存在'
        if not _owned(item, identity):
            return None, 403, '只能由提交者本人确认'
        current = item.get('status') or 'submitted'
        ok, code, err = feedback_status.checkTransition(
            current, 'user_confirmed', 'submitter', '')
        if not ok:
            return None, code, err
        _apply(item, 'user_confirmed', '', 'submitter')
        store.save(data)
        return item, 0, ''


def reopen(feedbackId, reason, identity):
    """提交者不认账：resolved → processing，必填 reason。"""
    with store.locked():
        data = store.load()
        item = _find(data, feedbackId)
        if item is None:
            return None, 404, '反馈不存在'
        if not _owned(item, identity):
            return None, 403, '只能由提交者本人重开'
        current = item.get('status') or 'submitted'
        ok, code, err = feedback_status.checkTransition(
            current, 'processing', 'submitter', reason)
        if not ok:
            return None, code, err
        _apply(item, 'processing', (reason or '').strip(), 'submitter')
        store.save(data)
        return item, 0, ''


# ---------------------------------------------------------------- 评论

def addComment(feedbackId, body, author, canHandle=False):
    """评论（规格 §6.2）：只追加线程，不改变状态。返回 (comment, httpCode, err)。

    author = submitter 形状（feedback_store.submitterOf 的产物）；
    canHandle=True 是处理者（主人会话），可评论任意一条——用来追问提交者。
    智能体令牌只能评论自己的（提交者补充）。
    """
    text = (body or '').strip() if isinstance(body, str) else ''
    if not text:
        return None, 400, '评论不能为空'
    if len(text) > MAX_COMMENT_CHARS:
        return None, 400, '评论超过 %d 字' % MAX_COMMENT_CHARS
    with store.locked():
        data = store.load()
        item = _find(data, feedbackId)
        if item is None:
            return None, 404, '反馈不存在'
        if not canHandle and not _owned(item, author):
            return None, 403, '只能评论自己的反馈'
        comment = {
            'comment_id': 'cmt_' + uuid.uuid4().hex[:16],
            'at': store.nowStamp(),
            'author': {'kind': author.get('kind'), 'id': author.get('id'),
                       'display_name': author.get('display_name') or author.get('id')},
            'body': text,
        }
        item.setdefault('comments', []).append(comment)
        # 处理者追问 → 提交者未读 +1；提交者自己补充不加
        if canHandle and not _isSelf(item, author.get('id')):
            store.markUnread(item)
        store.save(data)
        return comment, 0, ''


# ---------------------------------------------------------------- 批量受理

def batchAcknowledge(ids):
    """批量受理：一批 submitted → acknowledged。返回 {ok, skipped}。

    不在 submitted 的（已受理/已解决/不存在）逐个跳过并说明原因，不影响同批其它条。
    """
    ok, skipped = [], []
    with store.locked():
        data = store.load()
        changed = False
        for fid in (ids or [])[:MAX_BATCH]:
            item = _find(data, str(fid or ''))
            if item is None:
                skipped.append({'feedback_id': fid, 'reason': '不存在'})
                continue
            current = item.get('status') or 'submitted'
            good, _code, err = feedback_status.checkTransition(
                current, 'acknowledged', 'handler', '')
            if not good:
                skipped.append({'feedback_id': fid, 'reason': err})
                continue
            _apply(item, 'acknowledged', '', 'handler')
            ok.append(fid)
            changed = True
        if changed:
            store.save(data)
    return {'ok': ok, 'skipped': skipped}


# ---------------------------------------------------------------- 自动确认

def sweepAutoConfirm(now=None):
    """resolved 满 14 天提交者未操作 → 系统推进为 user_confirmed（规格 §5）。

    时钟可注入（测试）；触发方式选「请求时惰性检查」——反馈 API 的读路径
    （mine/详情/检索/统计/待受理摘要）进来先扫这一遍，不另起线程。
    返回本次自动确认的 feedback_id 列表。
    """
    now = store.nowSeconds() if now is None else now
    done = []
    with store.locked():
        data = store.load()
        for item in data['items']:
            if feedback_status.dueAutoConfirm(item, now):
                _apply(item, 'user_confirmed', '14 天未操作，系统自动确认', 'system')
                done.append(item.get('feedback_id'))
        if done:
            store.save(data)
    return done
