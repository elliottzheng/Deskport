# -*- coding: utf-8 -*-
"""用户反馈（反馈捕获系统的接收层）：提交、附件、检索、状态回写、删除。

契约见 docs/feedback.md（自包含规格的落地版）：
    POST   /api/feedback                     提交信封（幂等：idempotency_key）
    POST   /api/feedback/attachments         上传截图（≤1MB，png/jpg/webp，按魔数判类型）
    GET    /api/feedback                     检索：route/date_from/date_to/status/user_id/page/size
    GET    /api/feedback/<id>                单条（含状态历史）
    PATCH  /api/feedback/<id>/status         状态回写（处理端推动，rejected 必须带原因）
    DELETE /api/feedback/<id>                删除记录与附件
    GET    /api/feedback/attachments/<file>  取截图（同源取图，便于「我的反馈」展示）

红线（规格 §附）：
    - 只做无损落库 + 可检索，不做分类/去重/优先级（多人报同一问题是信号）；
    - message 与截图标注原样存储、转义展示，**任何下游不得把正文当指令执行**。

智能体通道（auth.py authGuard 放行 POST）：持有效令牌的调用方（执行方/验收方/联邦）
可直接提交，身份取令牌名，不接受 anonymous——提意见一律实名留痕。
"""
import os

from flask import Blueprint, Response, g, jsonify, request, send_file

from modules import events
from modules import feedback_store as store

blueprint = Blueprint('feedback', __name__)

USER = {'id': 'owner', 'role': 'owner'}       # 单密码系统：登录者就是这台机器的主人


def _identity():
    """身份：会话登录者 = 机器主人；带有效令牌的调用方 = 令牌名（智能体/联邦）。"""
    name = getattr(g, 'agentTokenName', '') or ''
    if name:
        return {'id': name, 'role': 'agent', 'anonymous': False, 'viaToken': True}
    return dict(USER, anonymous=False)


def _ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()


@blueprint.route('/api/feedback/attachments', methods=['POST'])
def uploadAttachment():
    """截图先落盘再提交信封：body 里只带 file_id（规格 §6.2）。"""
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
    """提交信封。幂等键重复时返回首次结果（200 + duplicate=true），不落第二条。"""
    envelope = request.get_json(silent=True)
    if envelope is None:
        return jsonify({'error': '需要 JSON 请求体'}), 400
    user = _identity()
    claimed = envelope.get('user') if isinstance(envelope.get('user'), dict) else {}
    # 令牌通道一律实名：智能体/联邦提的意见要能按令牌名回追，anonymous 不适用
    anonymous = bool(claimed.get('anonymous')) and not user.get('viaToken')
    ok, why = store.allowRate(('anon:' + _ip()) if anonymous else ('user:' + user['id']),
                              anonymous=anonymous)
    if not ok:
        return jsonify({'error': why}), 429
    if anonymous:
        envelope['user'] = {'id': claimed.get('id') or '', 'role': claimed.get('role') or '',
                            'anonymous': True}
    else:
        # 令牌通道的 id 与 role 都服务端定（智能体/联邦实名留痕）；
        # 网页端保持原行为：id 服务端定，role 允许客户端自报
        role = user['role'] if user.get('viaToken') else (claimed.get('role') or user['role'])
        envelope['user'] = {'id': user['id'], 'role': role, 'anonymous': False}
    env, err = store.validate(envelope)
    if env is None:
        return jsonify({'error': err}), 400
    item, created = store.submit(env, user)
    events.log('feedback.submit', target=item['feedback_id'], result='ok' if created else 'duplicate',
               detail='route=%s digest=%s' % (item.get('route'), store.digest(env)), ip=_ip())
    body = {'feedback_id': item['feedback_id'], 'status': item['status']}
    if not created:
        body['duplicate'] = True
    return jsonify(body)


@blueprint.route('/api/feedback', methods=['GET'])
def search():
    """检索（管理端/处理端用）。参数非法时不报错，按缺省处理。"""
    def num(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default
    result = store.search(
        route=request.args.get('route', ''),
        dateFrom=request.args.get('date_from', ''),
        dateTo=request.args.get('date_to', ''),
        status=request.args.get('status', ''),
        userId=request.args.get('user_id', ''),
        page=num('page', 1), size=num('size', 20))
    return jsonify(result)


@blueprint.route('/api/feedback/stats')
def stats():
    """运维视图：条数、状态分布、附件占用。"""
    return jsonify(store.stats())


@blueprint.route('/api/feedback/attachments/<fileId>')
def getAttachment(fileId):
    path = store.attachmentPath(fileId)
    if path is None:
        return jsonify({'error': '附件不存在'}), 404
    ext = os.path.splitext(path)[1].lstrip('.').lower()
    mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'application/octet-stream')
    resp = send_file(path, mimetype=mime, max_age=0, conditional=True)
    resp.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
    return resp


@blueprint.route('/api/feedback/<feedbackId>', methods=['GET'])
def detail(feedbackId):
    item = store.get(feedbackId)
    if item is None:
        return jsonify({'error': '反馈不存在'}), 404
    return jsonify({'feedback': item})


@blueprint.route('/api/feedback/<feedbackId>/status', methods=['PATCH'])
def patchStatus(feedbackId):
    """状态由处理端推动；必须带 note 说明原因（rejected 强制）。"""
    body = request.get_json(silent=True) or {}
    status = (body.get('status') or '').strip()
    item, err = store.setStatus(feedbackId, status, body.get('note') or '')
    if item is None:
        code = 404 if '不存在' in err else 400
        return jsonify({'error': err}), code
    events.log('feedback.status', target=feedbackId, detail=status, ip=_ip())
    return jsonify({'feedback_id': feedbackId, 'status': item['status']})


@blueprint.route('/api/feedback/<feedbackId>', methods=['DELETE'])
def remove(feedbackId):
    if not store.delete(feedbackId):
        return jsonify({'error': '反馈不存在'}), 404
    events.log('feedback.delete', target=feedbackId, ip=_ip())
    return jsonify({'ok': True})
