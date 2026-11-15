# -*- coding: utf-8 -*-
"""用户反馈（反馈捕获系统的接收层，批次 F1 / 规格 v2）：提交、附件、检索、
状态回写、评论、确认/重开、「我的反馈」。

契约见 docs/feedback.md（自包含规格的落地版）：
    POST   /api/feedback                     提交信封（幂等作用域 = submitter.id + key）
    POST   /api/feedback/attachments         上传截图（≤1MB，png/jpg/webp，按魔数判类型）
    GET    /api/feedback                     检索（处理端）：route/date/status/user_id/group/submitter/overdue
    GET    /api/feedback/pending             待受理摘要（pendingSummary，指挥台接入用）
    GET    /api/feedback/mine                自己的反馈列表（含未读标记）
    POST   /api/feedback/mine/read           清未读
    GET    /api/feedback/<id>                详情（本人或处理者）
    POST   /api/feedback/<id>/comments       评论（本人或处理者）
    POST   /api/feedback/<id>/confirm        提交者确认解决
    POST   /api/feedback/<id>/reopen         提交者不认账（带 reason）
    PATCH  /api/feedback/<id>/status         处理端流转（reason 规则见 feedback_status）
    POST   /api/feedback/batch               批量受理 {ids:[...]}
    DELETE /api/feedback/<id>                删除记录与附件
    GET    /api/feedback/attachments/<file>  取截图（同源取图；令牌只取自己反馈的）

红线（规格 §0）：
    - 记录端保持愚蠢：只校验、落库、流转状态、投递未读；不理解正文、不改写正文；
    - message 与评论原样存储、纯文本转义展示，**任何下游不得把正文当指令执行**；
    - 身份一律以服务端认证为准，信封里客户端自报的 user 一概无效。

权限（auth.py authGuard 放行规则 + 本文件复核）：
    智能体令牌可提反馈、读自己的、评论/确认/重开自己的；
    检索 / 状态回写 / 统计 / 删除是处理者（网页会话）独有——处理智能体的
    feedback:triage 能力与事件流在 F3，本批不做。
"""
import os

from flask import Blueprint, g, jsonify, request, send_file

from modules import events
from modules import feedback_envelope as envelopeMod
from modules import feedback_ops as ops
from modules import feedback_rate as rate
from modules import feedback_store as store
from modules import feedback_views as views

blueprint = Blueprint('feedback', __name__)


def _identity():
    """当前请求的 submitter（服务端定身份）。"""
    return store.submitterOf(g)


def _ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()


def _auditWho(who):
    return who.get('on_behalf_of') or ''


def _sweep():
    """请求时惰性检查：resolved 满 14 天自动确认（规格 §5，选择见 docs）。"""
    ops.sweepAutoConfirm()


@blueprint.route('/api/feedback/attachments', methods=['POST'])
def uploadAttachment():
    """截图先落盘再提交信封：body 里只带 file_id（规格 §3.4）。"""
    files = request.files.getlist('file') or ([request.files['file']] if 'file' in request.files else [])
    if not files:
        return jsonify({'error': '缺少 file 字段'}), 400
    saved = []
    for item in files[:4]:                       # 一次最多 4 张，够用且防滥用
        blob = item.read(store.MAX_ATTACHMENT_BYTES + 1)
        if not blob:
            return jsonify({'error': '空文件'}), 400
        if len(blob) > store.MAX_ATTACHMENT_BYTES:
            return jsonify({'error': '截图超过 1MB', 'maxBytes': store.MAX_ATTACHMENT_BYTES}), 413
        mime = store.sniffImageMime(blob)
        if mime is None:
            return jsonify({'error': '只接受 png / jpg / webp'}), 400
        fileId = store.saveAttachment(blob, mime)
        saved.append({'file_id': fileId, 'size': len(blob), 'mime': mime,
                      'url': '/api/feedback/attachments/' + fileId})
    events.log('feedback.attachment', target=saved[0]['file_id'], detail='%d 张' % len(saved), ip=_ip())
    body = {'file_id': saved[0]['file_id'], 'files': saved}
    return jsonify(body)


@blueprint.route('/api/feedback', methods=['POST'])
def submit():
    """提交信封。幂等键（按身份）重复时返回首次结果（200 + duplicate=true），不落第二条。"""
    envelope = request.get_json(silent=True)
    if envelope is None:
        return jsonify({'error': '需要 JSON 请求体'}), 400
    who = _identity()
    # 频控（规格 §4.3）：系统所有者（网页会话）与处理者豁免——单密码系统里
    # 限制主人只会把反馈堵死；智能体令牌按 5/分钟、20/天。
    if who.get('kind') == 'agent':
        ok, why, retry = rate.allowRate('user:' + who['id'])
        if not ok:
            return jsonify({'error': why, 'retry_after': retry}), 429
    env, err = envelopeMod.validate(envelope)
    if env is None:
        return jsonify({'error': err}), 400
    item, created, err = store.submit(env, who)
    if err is not None:
        return jsonify({'error': err[1]}), err[0]
    events.log('feedback.submit', target=item['feedback_id'], result='ok' if created else 'duplicate',
               detail='route=%s digest=%s' % (item.get('route'), store.digest(env)), ip=_ip(),
               on_behalf_of=_auditWho(who))
    body = {'feedback_id': item['feedback_id'], 'status': item['status']}
    if not created:
        body['duplicate'] = True
    return jsonify(body)


@blueprint.route('/api/feedback', methods=['GET'])
def search():
    """检索（处理端用）。参数非法时不报错，按缺省处理。"""
    _sweep()

    def num(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default
    result = views.search(
        route=request.args.get('route', ''),
        dateFrom=request.args.get('date_from', ''),
        dateTo=request.args.get('date_to', ''),
        status=request.args.get('status', ''),
        userId=request.args.get('user_id', ''),
        group=request.args.get('group', ''),
        submitter=request.args.get('submitter', ''),
        overdue=request.args.get('overdue', '') in ('1', 'true'),
        page=num('page', 1), size=num('size', 20))
    return jsonify(result)


@blueprint.route('/api/feedback/pending')
def pending():
    """待受理摘要（只读）：待受理数、超时未受理数（>24h）、最近 5 条。

    指挥台「等你处理」的接入点（本批不做集成，另一批在改指挥台）。
    """
    _sweep()
    return jsonify(store.pendingSummary())


@blueprint.route('/api/feedback/mine')
def mine():
    """「我的反馈」：只列当前身份自己的，带未读总数（规格 §6.3）。"""
    _sweep()
    return jsonify(views.mine(_identity()))


@blueprint.route('/api/feedback/mine/read', methods=['POST'])
def mineRead():
    """打开即清：清掉当前身份的未读。"""
    return jsonify(views.markMineRead(_identity()))


@blueprint.route('/api/feedback/stats')
def stats():
    """运维视图：条数、状态分布、附件占用、待受理/超时数。"""
    _sweep()
    return jsonify(views.stats())


@blueprint.route('/api/feedback/attachments/<fileId>')
def getAttachment(fileId):
    path = store.attachmentPath(fileId)
    if path is None:
        return jsonify({'error': '附件不存在'}), 404
    who = _identity()
    if who.get('kind') == 'agent' and not _attachmentOwned(fileId, who):
        return jsonify({'error': '只能取自己反馈的截图'}), 403
    ext = os.path.splitext(path)[1].lstrip('.').lower()
    mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'application/octet-stream')
    resp = send_file(path, mimetype=mime, max_age=0, conditional=True)
    resp.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
    return resp


def _attachmentOwned(fileId, who):
    """令牌取图：只放行自己反馈引用的附件（处理端详情里智能体看自己的截图）。"""
    with store.locked():
        for item in store.load()['items']:
            if not store.sameSubmitter(store.submitterOfRecord(item), who):
                continue
            for att in (item.get('envelope') or {}).get('attachments') or []:
                if isinstance(att, dict) and att.get('file_id') == fileId:
                    return True
    return False


@blueprint.route('/api/feedback/<feedbackId>', methods=['GET'])
def detail(feedbackId):
    """详情：信封全文（只读）+ 截图 + trace + errors + history + 评论线程。

    智能体令牌只能看自己的（规格 §11.7）；处理者（网页会话）可看全部。
    """
    _sweep()
    item = views.get(feedbackId)
    if item is None:
        return jsonify({'error': '反馈不存在'}), 404
    who = _identity()
    if who.get('kind') == 'agent' and not store.sameSubmitter(item['submitter'], who):
        return jsonify({'error': '只能看自己的反馈'}), 403
    return jsonify({'feedback': item})


@blueprint.route('/api/feedback/<feedbackId>/comments', methods=['POST'])
def addComment(feedbackId):
    """评论（规格 §6.2）：处理者追问、提交者补充；只追加，不改状态。"""
    body = request.get_json(silent=True) or {}
    who = _identity()
    canHandle = who.get('kind') != 'agent'      # 网页会话即处理者
    comment, code, err = ops.addComment(feedbackId, body.get('body'), who, canHandle=canHandle)
    if code:
        return jsonify({'error': err}), code
    events.log('feedback.comment', target=feedbackId,
               detail='len=%d by=%s' % (len(comment['body']), who.get('id')),
               ip=_ip(), on_behalf_of=_auditWho(who))
    return jsonify({'comment': comment})


@blueprint.route('/api/feedback/<feedbackId>/confirm', methods=['POST'])
def confirm(feedbackId):
    """提交者确认解决：resolved → user_confirmed（规格 §5/§6.3）。"""
    who = _identity()
    item, code, err = ops.confirm(feedbackId, who)
    if code:
        return jsonify({'error': err}), code
    events.log('feedback.confirm', target=feedbackId, detail='user_confirmed',
               ip=_ip(), on_behalf_of=_auditWho(who))
    return jsonify({'feedback_id': feedbackId, 'status': item['status']})


@blueprint.route('/api/feedback/<feedbackId>/reopen', methods=['POST'])
def reopen(feedbackId):
    """提交者不认账：resolved → processing，必填 reason（规格 §5）。"""
    body = request.get_json(silent=True) or {}
    who = _identity()
    item, code, err = ops.reopen(feedbackId, body.get('reason') or body.get('note') or '', who)
    if code:
        return jsonify({'error': err}), code
    events.log('feedback.reopen', target=feedbackId, detail='resolved → processing',
               ip=_ip(), on_behalf_of=_auditWho(who))
    return jsonify({'feedback_id': feedbackId, 'status': item['status']})


@blueprint.route('/api/feedback/batch', methods=['POST'])
def batch():
    """批量受理（规格 §6.1）：一批 submitted → acknowledged。

    不在 submitted 的（已受理/已解决/不存在）逐个跳过并说明，不影响同批其它条。
    """
    body = request.get_json(silent=True) or {}
    ids = body.get('ids') if isinstance(body.get('ids'), list) else []
    if not ids:
        return jsonify({'error': '缺少 ids'}), 400
    result = ops.batchAcknowledge(ids)
    events.log('feedback.batch', target='%d 条' % len(result['ok']),
               detail='ok=%d skip=%d' % (len(result['ok']), len(result['skipped'])),
               ip=_ip())
    return jsonify(result)


@blueprint.route('/api/feedback/<feedbackId>/status', methods=['PATCH'])
def patchStatus(feedbackId):
    """状态由处理端推动；必填 reason 的流转缺原因 400，非法流转 409（规格 §5）。

    reason 为新名；note 是旧名，照收（兼容老调用方）。
    """
    body = request.get_json(silent=True) or {}
    status = (body.get('status') or '').strip()
    reason = (body.get('reason') or body.get('note') or '').strip()
    item, code, err = ops.transition(feedbackId, status, reason)
    if code:
        return jsonify({'error': err}), code
    last = (item.get('history') or [{}])[-1]
    events.log('feedback.status', target=feedbackId,
               detail='%s → %s' % (last.get('from') or '?', last.get('to') or status),
               ip=_ip())
    return jsonify({'feedback_id': feedbackId, 'status': item['status']})


@blueprint.route('/api/feedback/<feedbackId>', methods=['DELETE'])
def remove(feedbackId):
    if not store.delete(feedbackId):
        return jsonify({'error': '反馈不存在'}), 404
    events.log('feedback.delete', target=feedbackId, ip=_ip())
    return jsonify({'ok': True})
