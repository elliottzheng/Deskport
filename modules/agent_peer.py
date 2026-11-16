# -*- coding: utf-8 -*-
"""批次 PM（R5-13）：平级消息与登记薄。

两个端点（都走智能体令牌通道，复用 agent_api 鉴权）：
    GET  /api/agent/registry   登记薄：已注册主管 + 其托管任务所在执行点
    POST /api/agent/message    平级消息：按 lead id/名寻址，落收件箱
                                （kind='peer'，进 URGENT_KINDS 立即唤醒）

设计（主控 2026-11-17 定，来源反馈 ac0b386f：ai-xmyz:9222 被 40 天前的
Playwright 实例占着，要起 CDP 的一方无法询问归属）：**不造消息系统**——
投递/未读/唤醒全部复用 leads_inbox，发现复用 leads_store 注册表（外部
智能体自助注册是批次 SR 已有的能力）。本模块只做两件薄事：聚合一个
可查的登记薄；按令牌寻址把消息落进别人的箱。

典型场景：占端口/目录/服务前 GET registry 查有没有主；撞车/协商
POST /api/agent/message 问对方（text 说清要什么、要多久）。

频控：每令牌每分钟 10 条（照 feedback_rate 的模式，计数落状态目录小
JSON 重启不清零；平级协调是低频动作，10 条/分足够正常协商，堵死刷屏）。
"""
import json
import threading
import time

from flask import Blueprint, jsonify, request

from modules import agent_api, leads_inbox, leads_store, tasks_store

blueprint = Blueprint('agent_peer', __name__)

MAX_TEXT = 1000            # 与 lead inbox 的 MAX_TEXT 对齐
MAX_CONTEXT = 500          # context 序列化上限
RATE_PER_MIN = 10          # 每令牌每分钟平级消息条数
_LOCK = threading.Lock()


def _ratePath():
    import config
    return config.statePath('peer-message-rate.json')


def _rateSave(data):
    from modules import state
    state.atomicWriteJson(_ratePath(), data)


def _rateOk(key):
    """频控：返回 (ok, retryAfter 秒)。计数落状态目录，重启不清零。"""
    now = time.time()
    with _LOCK:
        try:
            with open(_ratePath(), encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        hits = [t for t in (data.get(key) or [])
                if isinstance(t, (int, float)) and now - t < 60]
        if len(hits) >= RATE_PER_MIN:
            data[key] = hits
            _rateSave(data)
            return False, int(60 - (now - hits[0])) + 1
        hits.append(now)
        data[key] = hits
        _rateSave(data)
        return True, 0


def _registryRows():
    """登记薄行：每个 lead 一行，node 取其托管任务所在执行点。"""
    tasks_store.repo.load()
    rows = []
    for lead in leads_store.list():
        lead = lead if isinstance(lead, dict) else {}
        taskId = str(lead.get('taskId') or '')
        node = ''
        if taskId:
            task = tasks_store.repo.get(taskId)
            node = str((task or {}).get('node') or '')
        rows.append({'id': str(lead.get('id') or ''),
                     'name': str(lead.get('name') or ''),
                     'kind': str(lead.get('kind') or ''),
                     'status': str(lead.get('status') or ''),
                     'node': node,
                     'taskId': taskId})
    rows.sort(key=lambda r: r['name'])
    return rows


def _resolveLead(to):
    """寻址：先 id 精确，再 name 唯一匹配（cleanName 口径）。"""
    lead = leads_store.get(to)
    if lead is not None:
        return lead
    wanted = leads_store.cleanName(to)
    if not wanted:
        return None
    matches = [l for l in leads_store.list()
               if leads_store.cleanName(str(l.get('name') or '')) == wanted]
    return matches[0] if len(matches) == 1 else None


@blueprint.route('/api/agent/registry')
def agentRegistry():
    """登记薄（批次 PM）：已注册智能体 + 所在执行点。

    任何有效智能体令牌可读（同 /api/tasks/nodes 口径）。聚合现有注册表
    与任务存储，零新存储。node 为空 = 该主管当前没有托管任务（external
    未起任务或已结束）。
    """
    agent = agent_api.agentFromRequest()
    if agent is None:
        return jsonify({'error': 'unauthorized (需要 X-Agent-Token)'}), 401
    return jsonify({'leads': _registryRows()})


@blueprint.route('/api/agent/message', methods=['POST'])
def agentMessage():
    """平级消息（批次 PM）：按 lead id/名给另一个智能体发消息。

    落对方的收件箱（kind='peer'，进 URGENT_KINDS 立即唤醒，不等合并
    窗口）；对方无托管任务时条目落箱，下次读取可见。from 按令牌定
    （服务端身份，信封自报不采信），text 前缀带发送方名（人读），ref 里
    带结构化 from/context（机器读）。回复用同一端点互换 to 即可。
    频控每令牌每分钟 RATE_PER_MIN 条，超了 429 带 retryAfter。
    """
    agent = agent_api.agentFromRequest()
    if agent is None:
        return jsonify({'error': 'unauthorized (需要 X-Agent-Token)'}), 401
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    to = str(body.get('to') or '').strip()
    text = str(body.get('text') or '').strip()
    if not to or not text:
        return jsonify({'error': 'to 与 text 必填'}), 400
    ok, retryAfter = _rateOk(str(agent.get('name') or ''))
    if not ok:
        return jsonify({'error': '平级消息太频繁（每令牌每分钟 %d 条），%d 秒后重试'
                       % (RATE_PER_MIN, retryAfter),
                        'retryAfter': retryAfter}), 429
    lead = _resolveLead(to)
    if lead is None:
        return jsonify({'error': '找不到收方：%s' % to,
                        'hint': 'to 用 lead id 或 name（GET /api/agent/registry 查）',
                        'availableLeads': [r['name'] for r in _registryRows()]}), 400
    if str(lead.get('id') or '') == str(agent.get('lead') or ''):
        return jsonify({'error': '不能给自己发平级消息'}), 400
    context = body.get('context')
    if context is not None and not isinstance(context, dict):
        return jsonify({'error': 'context 必须是对象'}), 400
    contextText = json.dumps(context, ensure_ascii=False)[:MAX_CONTEXT] \
        if context else ''
    ref = ''
    if context or body.get('ref'):
        ref = {'from': {'token': str(agent.get('name') or ''),
                        'lead': str(agent.get('lead') or '')},
               'ref': str(body.get('ref') or '')[:120]}
        if context:
            ref['context'] = json.loads(contextText) if contextText else {}
    fromName = str(agent.get('name') or 'unknown')
    seq = leads_inbox.append(str(lead.get('id') or ''), 'up', 'peer',
                             '【%s】%s' % (fromName, text[:MAX_TEXT - 32]),
                             ref)
    if not seq:
        return jsonify({'error': '消息落箱失败（lead id 为空或方向非法）'}), 400
    return jsonify({'ok': True, 'to': str(lead.get('name') or ''),
                    'leadId': str(lead.get('id') or ''), 'seq': seq}), 202
