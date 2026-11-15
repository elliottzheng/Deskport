# -*- coding: utf-8 -*-
"""接手候选列表：GET /api/tasks/kimi-sessions（批次 G1 从 tasks_api 拆出腾预算）。

已存在的 Kimi CLI 会话（第二轮反馈 §2.3）：智能体挑接手候选。
?node=<节点>：代理远端 node 的 /api/kimi-sessions（R4-7），
未指定或 local 时列本机 kimi 会话。
"""
import logging

from flask import Blueprint, g, jsonify, request

from modules import agent_nodes, events
from modules import kimi_acp

blueprint = Blueprint('task_kimi_sessions', __name__)
log = logging.getLogger('tasks')


@blueprint.route('/api/tasks/kimi-sessions')
def listKimiSessions():
    cwd = (request.args.get('cwd') or '').strip()
    try:
        limit = int(request.args.get('limit') or 30)
    except ValueError:
        limit = 30
    node = (request.args.get('node') or '').strip()
    if node and node != 'local':
        target = agent_nodes.findNode(node)
        if target is None:
            return jsonify({'error': '节点不存在', 'nodes': agent_nodes.listNodeNames()}), 404
        from urllib.parse import quote
        try:
            status, data = agent_nodes.nodeRequest(
                target, 'GET',
                '/api/kimi-sessions?cwd=%s&limit=%d' % (quote(cwd), limit),
                timeout=120)
        except Exception as e:
            # 隧道瞬断等传输错误：502 JSON 让调用方可重试，不再 500
            return jsonify({'error': '节点请求失败: %s' % str(e)[:200],
                            'node': node}), 502
        if status != 200:
            return jsonify({'error': (data or {}).get('error') or 'HTTP %s' % status,
                            'node': node}), 502
        items = data.get('sessions') or []
    else:
        try:
            items = kimi_acp.listSessions(limit=limit, cwd=cwd or None)
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 502
    tokenName = getattr(g, 'agentTokenName', '')
    if tokenName:
        events.log('agent.task.kimilist', tokenName, 'ok',
                   'node=%s cwd=%s' % (node or '-', cwd or '-'), request.remote_addr)
    return jsonify({'sessions': items, 'node': node,
                    'hint': 'POST /api/tasks {"kimiSessionId": "<id>", '
                            '"node": "<执行点>", "cwd": "<目录>"} '
                            '接手（默认 fork 副本，不干扰原会话）'})
