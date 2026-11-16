# -*- coding: utf-8 -*-
"""工作项 API（批次 G2）：/api/workitems 与 /api/settings/projects。

鉴权同任务（规格 §3）：网页会话全看；绑了主管的令牌只看自己的、只能决策/
取消自己的。项目配置只认网页会话（/api/settings 前缀本就不放行令牌）。
创建经 workitem_setup.create（平台建 worktree/分支/实施任务）；决策与取消
经 workitem_actions（单一写者在流水线侧）。
"""
import logging

from flask import Blueprint, g, jsonify, request

from modules import events, workitem_actions, workitem_projects, workitem_setup
from modules import workitem_view

blueprint = Blueprint('workitems', __name__)
log = logging.getLogger('workitem-api')

DECISIONS = ('continue', 'rework', 'integrate', 'cancel')


def _isWeb():
    """网页会话（没有智能体令牌名）；设备/联邦/智能体令牌都不是。"""
    return not str(getattr(g, 'agentTokenName', '') or '')


def _boundLead():
    return str(getattr(g, 'agentLead', '') or '')


def _forbidden(wi):
    """主管令牌只能看/动自己的；网页会话均可。"""
    lead = _boundLead()
    if lead and (wi.get('lead') or '') != lead:
        return True
    return False


@blueprint.route('/api/workitems', methods=['POST'])
def createWorkitem():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    # 主管令牌派活：lead 缺省取令牌绑定的主管（网页会话可显式传）
    if not str(data.get('lead') or '').strip():
        data['lead'] = _boundLead()
    payload, code = workitem_setup.create(data)
    if code < 400:
        events.log('workitem.create', (payload.get('workItem') or {}).get('id')
                   or '', 'ok', 'project=%s' % (payload.get('project') or ''),
                   request.remote_addr,
                   **({'on_behalf_of': str(getattr(g, 'agentOnBehalfOf', '')
                                           or '')}
                      if getattr(g, 'agentOnBehalfOf', '') else {}))
    return jsonify(payload), code


@blueprint.route('/api/workitems', methods=['GET'])
def listWorkitems():
    # 主管只看自己的；网页会话全看（规格 §3）
    items = workitem_view.listBriefs(_boundLead())
    states = {}
    for item in items:
        states[item['state']] = states.get(item['state'], 0) + 1
    return jsonify({'workItems': items, 'count': len(items),
                    'byState': states})


@blueprint.route('/api/workitems/<wid>', methods=['GET'])
def getWorkitem(wid):
    detail = workitem_view.detail(wid)
    if detail is None:
        return jsonify({'error': '工作项不存在'}), 404
    if _boundLead() and detail.get('lead') != _boundLead():
        return jsonify({'error': '只能看自己名下的工作项'}), 403
    return jsonify({'workItem': detail})


@blueprint.route('/api/workitems/<wid>/decision', methods=['POST'])
def decideWorkitem(wid):
    data = request.get_json(force=True, silent=True) or {}
    action = str(data.get('action') or '').strip().lower()
    if action not in DECISIONS:
        return jsonify({'error': 'action 只能是 %s' % ' / '.join(DECISIONS)}), 400
    wi = workitem_view.briefOf(wid)
    if wi is None:
        return jsonify({'error': '工作项不存在'}), 404
    if _forbidden(wi):
        return jsonify({'error': '只能决策自己名下的工作项'}), 403
    payload, code = workitem_actions.decide(wid, action,
                                            str(data.get('text') or ''))
    if code < 400:
        events.log('workitem.decision', wid, 'ok', action,
                   request.remote_addr)
    return jsonify(payload), code


@blueprint.route('/api/workitems/<wid>/cancel', methods=['POST'])
def cancelWorkitem(wid):
    wi = workitem_view.briefOf(wid)
    if wi is None:
        return jsonify({'error': '工作项不存在'}), 404
    if _forbidden(wi):
        return jsonify({'error': '只能取消自己名下的工作项'}), 403
    payload, code = workitem_actions.cancel(wid, by='面板' if _isWeb()
                                            else str(getattr(g, 'agentTokenName',
                                                             '') or ''))
    if code < 400:
        events.log('workitem.cancel', wid, 'ok', '', request.remote_addr)
    return jsonify(payload), code


# ---------------------------------------------------------------- 项目配置

@blueprint.route('/api/settings/projects', methods=['GET'])
def listProjects():
    return jsonify({'projects': workitem_projects.repo.list()})


@blueprint.route('/api/settings/projects', methods=['POST'])
def saveProject():
    """增/改（带 id=改，缺 id=新增）；内置 deskport 可改参数但 id 不变。"""
    data = request.get_json(force=True, silent=True) or {}
    existing = workitem_projects.repo.get(str(data.get('id') or ''))
    clean, errors = workitem_projects.validate(data, isNew=existing is None)
    if errors:
        return jsonify({'error': '项目参数不符合要求', 'fields': errors}), 400
    if existing is not None:
        clean['id'] = existing['id']
        clean['builtin'] = bool(existing.get('builtin'))
    project = workitem_projects.repo.put(clean)
    events.log('settings.workitemProjects', project['id'], 'ok',
               'name=%s' % project['name'], request.remote_addr)
    return jsonify({'ok': True, 'project': project})


@blueprint.route('/api/settings/projects/<pid>', methods=['DELETE'])
def deleteProject(pid):
    if pid == workitem_projects.DEFAULT_PROJECT_ID:
        return jsonify({'error': '内置项目 deskport 不可删除（可改参数）'}), 400
    try:
        workitem_projects.repo.remove(pid)
    except KeyError:
        return jsonify({'error': '项目不存在'}), 404
    events.log('settings.workitemProjects', pid, 'ok', 'deleted',
               request.remote_addr)
    return jsonify({'ok': True})


def register(app):
    app.register_blueprint(blueprint)
