# -*- coding: utf-8 -*-
"""结构化结果的 MCP 面（批次 G1）：POST /api/mcp 只提供一个工具
submit_result——执行方的交付就是一次工具调用，平台据此流转。

协议：MCP over HTTP（JSON-RPC 2.0，单端点）。kimi ACP 握手实测
（agentCapabilities.mcpCapabilities = {http: true, sse: true}）只支持
http/sse，故注入 http 形式，stdio 桥不需要。

会话令牌：每个任务一个随机令牌（secrets.token_urlsafe），只授权对本任务调
submit_result（X-Deskport-Session-Token 头，HTTP 提交面同认这个头）；任务
关闭或会话线程退出即吊销。只在内存，不落盘、不进事件流与日志明文。

tools/list 按任务的 expects 动态给出入参 schema 与中文说明（_FIELDS 同源
生成，规则在 modules/task_result.py）；tools/call 走 task_result.submit——
校验失败以 isError 结果返回逐字段 errors，执行方改完可重交。
"""
import json
import logging
import secrets
import threading
import time

import config
from flask import Blueprint, jsonify, request

from modules import agent_handbook, task_node, task_result
from modules import tasks_store as store

blueprint = Blueprint('task_result_mcp', __name__)
log = logging.getLogger('task-result')

TOOL_NAME = 'submit_result'
MCP_SERVER_NAME = 'deskport'          # 工具在 kimi 里呈现为 mcp__deskport__submit_result
TOKEN_HEADER = 'X-Deskport-Session-Token'
SERVER_INFO = {'name': 'deskport-result', 'version': '1.0'}
PROTOCOL_VERSION = '2024-11-05'

_TOOL_DESC = {
    'impl': '提交本任务的结构化交付结果（实施方）。交付 = 一次工具调用：'
            'summary/commit/tests 必填，gates/risks/questions 按实际填。'
            '自由文本报告只作补充，不算交付。',
    'verify': '提交本任务的结构化验收结果（验收方）。verdict/target 必填；'
              'rework 时把必须修的点写进 mustFix。自由文本报告只作补充，不算交付。',
    '': '提交本任务的结构化结果：summary 必填，其余自由 data。',
}


# ---------------------------------------------------------------- 工具 schema

def _schemaStr(limit, desc):
    node = {'type': 'string', 'description': desc}
    if limit:
        node['maxLength'] = limit
    return node


def _inputSchema(expects):
    if expects == 'impl':
        return {
            'type': 'object',
            'properties': {
                'summary': _schemaStr(task_result.SUMMARY_MAX, '本次交付摘要（≤300 字）'),
                'commit': _schemaStr(0, '本地 commit（7–40 位十六进制）'),
                'branch': _schemaStr(task_result.BRANCH_MAX, '分支名（可选）'),
                'tests': {'type': 'object',
                          'properties': {'passed': {'type': 'integer', 'minimum': 0},
                                         'failed': {'type': 'integer', 'minimum': 0},
                                         'skipped': {'type': 'integer', 'minimum': 0}},
                          'required': ['passed', 'failed', 'skipped'],
                          'description': '测试统计（三项都是整数）'},
                'gates': {'type': 'object',
                          'properties': {'structure': {'type': 'string',
                                                        'enum': list(task_result.GATE_VALUES)},
                                         'frontend': {'type': 'string',
                                                      'enum': list(task_result.GATE_VALUES)}},
                          'description': '门禁结果（可选，pass/fail）'},
                'risks': {'type': 'array',
                          'items': _schemaStr(task_result.RISK_MAX, '一条风险（≤200 字）'),
                          'description': '风险与未决事项（可选）'},
                'questions': {'type': 'array',
                              'items': _schemaStr(task_result.ITEM_MAX, '一个需要主管判断的问题'),
                              'description': '需要主管拍板的问题（有内容即表示需要判断）'},
            },
            'required': ['summary', 'commit', 'tests'],
        }
    if expects == 'verify':
        return {
            'type': 'object',
            'properties': {
                'summary': _schemaStr(task_result.SUMMARY_MAX, '验收摘要（可选，≤300 字）'),
                'verdict': {'type': 'string',
                            'enum': list(task_result.VERDICTS),
                            'description': '验收结论：pass / rework / needs_judgment'},
                'target': _schemaStr(0, '被验收的 commit（7–40 位十六进制）'),
                'mustFix': {'type': 'array',
                            'items': _schemaStr(task_result.ITEM_MAX, '一个必须修的问题（≤300 字）')},
                'suggest': {'type': 'array',
                            'items': _schemaStr(task_result.ITEM_MAX, '一条建议（≤300 字）')},
                'evidence': {'type': 'array',
                             'items': _schemaStr(task_result.ITEM_MAX, '一条证据（≤300 字）')},
                'unverified': {'type': 'array',
                               'items': _schemaStr(task_result.ITEM_MAX, '一条没验到的点（≤300 字）')},
            },
            'required': ['verdict', 'target'],
        }
    return {
        'type': 'object',
        'properties': {
            'summary': _schemaStr(task_result.SUMMARY_MAX, '结果摘要（≤300 字）'),
            'data': {'type': 'object', 'description': '自由补充数据（≤8KB，可选）'},
        },
        'required': ['summary'],
    }


def toolSpec(expects):
    """tools/list 返回的工具描述（入参 schema 按任务的 expects 动态给出）。"""
    return {'name': TOOL_NAME,
            'description': _TOOL_DESC.get(expects, _TOOL_DESC['']),
            'inputSchema': _inputSchema(expects)}


# ---------------------------------------------------------------- 会话令牌

_tokens = {}           # token -> {'taskId', 'expects', 'at'}
_byTask = {}           # taskId -> token（每任务一个，重发即换）
_tokensLock = threading.Lock()


def issueToken(taskId, expects):
    """发（换）发本任务的会话令牌：旧令牌立即作废，返回新令牌。"""
    token = secrets.token_urlsafe(32)
    with _tokensLock:
        old = _byTask.pop(taskId, None)
        if old:
            _tokens.pop(old, None)
        _tokens[token] = {'taskId': taskId, 'expects': expects,
                          'at': time.time()}
        _byTask[taskId] = token
    return token


def tokenTask(token):
    """令牌 → taskId；无效/已吊销返回 ''。"""
    with _tokensLock:
        entry = _tokens.get(str(token or ''))
        return entry['taskId'] if entry else ''


def revokeTask(taskId):
    """吊销本任务的会话令牌（任务关闭 / 会话线程退出）。"""
    with _tokensLock:
        old = _byTask.pop(taskId, None)
        if old:
            _tokens.pop(old, None)


def revokeAll():
    """进程退出：全部作废（测试隔离用）。"""
    with _tokensLock:
        _tokens.clear()
        _byTask.clear()


# ---------------------------------------------------------------- 注入

def hubMcpUrl(node):
    """中枢 MCP 端点（kimi 只支持 http/sse，注入 http 形式）。

    地址推导（不自动探测网卡——猜的地址不可靠，印错比不印更糟）：
    config.agentMcpUrl 显式覆盖 > lanAddress 推导 > 本机执行点回环。
    远端执行点且两者都没配 → ''（不注入；执行方改走技能包 result 子命令）。
    """
    raw = str(config.load().get('agentMcpUrl') or '').strip()
    if raw.startswith(('http://', 'https://')):
        return raw.rstrip('/') + '/api/mcp'
    lan = agent_handbook.lanDirect()
    if lan:
        return lan + '/api/mcp'
    if task_node.isLocal(node):
        return 'http://127.0.0.1:%d/api/mcp' % config.getPort()
    return ''


def mcpServersFor(task):
    """会话建立时注入的 mcpServers（ACP http 形式，令牌放 header）。

    无 expects / 推不出中枢地址 → 空列表（调用方据此落状态事件）。
    """
    if not task.get('expects'):
        return []
    url = hubMcpUrl(task_node.of(task))
    if not url:
        return []
    token = issueToken(task.get('id') or '', task.get('expects') or '')
    return [{'type': 'http', 'name': MCP_SERVER_NAME, 'url': url,
             'headers': [{'name': TOKEN_HEADER, 'value': token}]}]


# ---------------------------------------------------------------- 端点

def _error(mid, code, message):
    return jsonify({'jsonrpc': '2.0', 'id': mid,
                    'error': {'code': code, 'message': message}})


def _result(mid, result):
    return jsonify({'jsonrpc': '2.0', 'id': mid, 'result': result})


def _toolError(mid, text):
    return _result(mid, {'content': [{'type': 'text', 'text': text}],
                         'isError': True})


@blueprint.route('/api/mcp', methods=['POST'])
def mcpEndpoint():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _error(None, -32700, 'parse error：请求体必须是 JSON-RPC 对象')
    mid = body.get('id')
    method = body.get('method') or ''
    token = request.headers.get(TOKEN_HEADER) or ''
    taskId = tokenTask(token)
    if not taskId:
        return _error(mid, -32001, '会话令牌无效或已失效（任务关闭即失效）'), 401
    store.repo.load()
    with store.repo.lock:
        task = store.repo.get(taskId)
    if task is None:
        return _error(mid, -32001, '任务不存在'), 401
    expects = task.get('expects') or ''
    if method.startswith('notifications/'):
        return ('', 202)
    if method == 'initialize':
        return _result(mid, {'protocolVersion': PROTOCOL_VERSION,
                             'capabilities': {'tools': {}},
                             'serverInfo': SERVER_INFO})
    if method == 'ping':
        return _result(mid, {})
    if method == 'tools/list':
        return _result(mid, {'tools': [toolSpec(expects)]})
    if method == 'tools/call':
        params = body.get('params') or {}
        name = params.get('name') or ''
        if name != TOOL_NAME:
            return _toolError(mid, '未知工具 %s（本服务只提供 %s）'
                              % (name, TOOL_NAME))
        arguments = params.get('arguments')
        if not isinstance(arguments, dict):
            return _toolError(mid, 'arguments 必须是 JSON 对象')
        payload, code = task_result.submit(task, arguments, via='mcp')
        if code != 200:
            return _toolError(mid, '结果不符合要求，请按字段改后重交：'
                              + json.dumps(payload.get('fields') or {},
                                           ensure_ascii=False))
        text = '结果已提交（第 %d 次）。%s' % (
            payload.get('resultCount') or 1,
            (payload.get('result') or {}).get('summary', ''))
        return _result(mid, {'content': [{'type': 'text', 'text': text[:500]}],
                             'structuredContent': payload})
    return _error(mid, -32601, 'method not found: %s' % method)
