# -*- coding: utf-8 -*-
"""结构化结果的 HTTP 提交面（批次 G1）：POST /api/tasks/<id>/result。

三种身份（规格 §2）：该任务的会话令牌（X-Deskport-Session-Token，与 MCP
同一个令牌）、任务属主/主管的智能体令牌、网页会话。执行方校验失败可重交
（400 带逐字段 errors）；已结束的任务不再收（409）——closedByUser 或任何
终态都算结束（关闭竞态返工：用户关任务到 runner 落定终态之间有窗口，
窗口里 status 可能还是 failed，但任务已算结束）。

与 MCP 面（task_result_mcp）共用 task_result.submit——存储、事件、主管
收件箱的规则只有一份。
"""
import logging

from flask import Blueprint, g, jsonify, request

from modules import events, task_result, task_result_mcp
from modules import tasks_store as store

blueprint = Blueprint('task_result_api', __name__)
log = logging.getLogger('task-result')


def _authorized(task):
    """会话令牌（守卫层已绑本任务）/ 属主或主管的智能体令牌 / 网页会话。"""
    token = str(getattr(g, 'taskResultToken', '') or '')
    if token:
        return task_result_mcp.tokenTask(token) == (task.get('id') or '')
    name = str(getattr(g, 'agentTokenName', '') or '')
    if not name:
        return True          # 网页会话（面板用户）
    if name == (task.get('owner') or ''):
        return True
    lead = str(getattr(g, 'agentLead', '') or '')
    return bool(lead) and lead == (task.get('lead') or '')


@blueprint.route('/api/tasks/<taskId>/result', methods=['POST'])
def postResult(taskId):
    store.repo.load()
    with store.repo.lock:
        task = store.repo.get(taskId)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if not _authorized(task):
        return jsonify({'error': '无权提交该任务的结果（属主/主管/会话令牌）'}), 403
    if task_result.ended(task):
        # 关闭竞态（G1 返工）：用户已关闭（closedByUser，runner 可能还没落定
        # 终态）或已入任何终态（failed/interrupted 也算）都不再收
        return jsonify({'error': '任务已结束，不能再提交结果'}), 409
    data = request.get_json(force=True, silent=True) or {}
    payload, code = task_result.submit(task, data, via='http')
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        # on_behalf_of 与 tasks_api 同规则：代表谁才带这个键（空 = 不带）
        onBehalf = str(getattr(g, 'agentOnBehalfOf', '') or '').strip()
        kw = {'on_behalf_of': onBehalf} if onBehalf else {}
        events.log('agent.task.result', tokenName,
                   'ok' if code == 200 else 'reject',
                   'task=%s expects=%s' % (taskId[:8],
                                           task.get('expects') or '-'),
                   request.remote_addr, **kw)
    return jsonify(payload), code
