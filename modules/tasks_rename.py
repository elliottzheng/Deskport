# -*- coding: utf-8 -*-
"""任务改名（批次 T）：PATCH /api/tasks/<id> 的校验、归属判断与审计。

路由壳自带蓝图（照 tasks_federation 的先例：/api/tasks/* 的路由不必都挂在
tasks_api 的蓝图上），业务只有一件——把标题改成调用方给的名字：
    标题归一走 task_title.cleanTitle（1–40 字，超长截断，不含换行）；
    网页会话可改任何任务；令牌只能改它自己建的任务（owner == 令牌名）或
    它绑定主管名下的任务（task.lead == 令牌的 lead，沿用批次 Q 的归属判断）；
    落盘 + 任务事件 + 审计 agent.task.rename。
"""
from flask import Blueprint, g, jsonify, request

from modules import events, task_title
from modules import tasks_store as store

blueprint = Blueprint('tasks_rename', __name__)


def _onBehalfOf():
    """批次 H：令牌代表谁（onBehalfOf）；没有就不传，老日志解析不受影响。"""
    who = str(getattr(g, 'agentOnBehalfOf', '') or '').strip()
    return {'on_behalf_of': who} if who else {}


def renameTask(task, data):
    """改名核心（路由只做 404 与 jsonify）。返回 (payload, status)。"""
    if not isinstance(data, dict) or 'title' not in data:
        return {'error': 'PATCH 只支持 title（新名字放在 title 里）'}, 400
    title = task_title.cleanTitle(data.get('title'))
    if not title:
        return {'error': 'title 不能为空（1–40 字，不含换行）'}, 400
    tokenName = str(getattr(g, 'agentTokenName', '') or '')
    if tokenName:
        leadId = str(getattr(g, 'agentLead', '') or '')
        own = (task.get('owner') or '') == tokenName
        bound = bool(leadId) and (task.get('lead') or '') == leadId
        if not (own or bound):
            return {'error': '只能改名你自己建的任务，或你绑定主管名下的任务'}, 403
    with store.repo.lock:
        task['title'] = title
        task['titleSource'] = 'given'
        store.repo.save()
    store.repo.append_event(task, 'status', text='任务已改名: ' + title)
    events.log('agent.task.rename', tokenName or '面板', 'ok',
               '%s → %s' % (task.get('id') or '', title),
               request.remote_addr, **_onBehalfOf())
    return {'ok': True, 'title': title, 'titleSource': 'given',
            'task': store.repo.summary(task)}, 200


@blueprint.route('/api/tasks/<taskId>', methods=['PATCH'])
def renameTaskRoute(taskId):
    store.repo.load()
    task = store.repo.get(taskId)
    if task is None:
        return jsonify({'error': '任务不存在'}), 404
    payload, code = renameTask(task, request.get_json(force=True, silent=True)
                               or {})
    return jsonify(payload), code
