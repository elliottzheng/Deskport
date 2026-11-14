# -*- coding: utf-8 -*-
"""主管管理面（批次 Q P1）：设置页「主管」卡片 + 令牌通道自查端点。

两组端点、两套鉴权（沿用现有约定，不自造守卫）：
    /api/settings/leads*   只认网页会话——与 /api/settings/agent-tokens
                           同一档：设备/智能体令牌打这些管理接口一律 401
                           （authGuard 的 /api/ 分支自动做到，见 auth.py）
    GET /api/agent/lead    任何有效令牌可调：问"我绑了哪个主管"（Q4）

绑定存在 config.agentTokens[].lead（Q2）：一个令牌最多绑一个主管，一个
主管可绑多个令牌；吊销令牌自然解绑（条目没了）。审计 lead.created /
lead.updated / lead.deleted / lead.token.bound / lead.token.unbound。
"""
import json

from flask import Blueprint, jsonify, request

import config
from modules import agent_api, events, leads_store, tasks_store

blueprint = Blueprint('leads', __name__)

# PATCH 只许改这三个键（status 的合法值在 leads_store.STATUSES）
PATCH_KEYS = ('name', 'project', 'status')


def _tokenNames(leadId):
    """绑到该主管的令牌名（按 config 现值算，吊销后自然消失）。"""
    return [str(t.get('name') or '') for t in config.getAgentTokens()
            if str(t.get('lead') or '') == leadId]


def _taskCounts(leadId):
    """该主管名下的任务计数（total/running/waiting）。"""
    tasks_store.repo.load()
    rows = [t for t in tasks_store.repo.list()
            if (t.get('lead') or '') == leadId]
    return {'total': len(rows),
            'running': sum(1 for t in rows if t.get('status') == 'running'),
            'waiting': sum(1 for t in rows if t.get('status') == 'waiting')}


def _record(lead):
    """管理列表视图：记录 + 绑定的令牌名 + 任务计数。"""
    out = dict(lead)
    out['tokens'] = _tokenNames(str(lead.get('id') or ''))
    out['tasks'] = _taskCounts(str(lead.get('id') or ''))
    return out


def _findToken(tokenId):
    tokens = config.getAgentTokens()
    entry = next((t for t in tokens if str(t.get('id') or '') == str(tokenId)),
                 None)
    return tokens, entry


@blueprint.route('/api/settings/leads', methods=['GET'])
def listLeads():
    leads = sorted(leads_store.list(), key=lambda l: l.get('createdAt', 0))
    return jsonify({'leads': [_record(lead) for lead in leads]})


@blueprint.route('/api/settings/leads', methods=['POST'])
def createLead():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    lead, err = leads_store.create(data.get('name'), data.get('project'),
                                   data.get('kind') or 'external')
    if err:
        return jsonify({'error': err}), 400
    events.log('lead.created', lead['name'], 'ok',
               'kind=%s project=%s' % (lead['kind'], lead['project'] or '-'),
               request.remote_addr)
    return jsonify({'lead': lead}), 201


@blueprint.route('/api/settings/leads/<lid>', methods=['PATCH'])
def updateLead(lid):
    lead = leads_store.get(lid)
    if lead is None:
        return jsonify({'error': '主管不存在: %s' % lid}), 404
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    unknown = [str(k) for k in data if k not in PATCH_KEYS]
    if unknown:
        return jsonify({'error': 'PATCH 只支持 %s，不认: %s'
                       % (' / '.join(PATCH_KEYS), '、'.join(sorted(unknown)))}), 400
    updated, err = leads_store.update(lid, data)
    if err == leads_store.NOT_FOUND:
        return jsonify({'error': '主管不存在: %s' % lid}), 404
    if err:
        return jsonify({'error': err}), 400
    events.log('lead.updated', updated['name'], 'ok',
               json.dumps({k: data.get(k) for k in PATCH_KEYS if k in data},
                          ensure_ascii=False)[:200], request.remote_addr)
    return jsonify({'lead': updated})


@blueprint.route('/api/settings/leads/<lid>', methods=['DELETE'])
def deleteLead(lid):
    lead = leads_store.get(lid)
    if lead is None:
        return jsonify({'error': '主管不存在: %s' % lid}), 404
    name = lead.get('name') or lid
    bound = _tokenNames(lid)
    if bound:
        # 令牌还绑着：删了记录，这些令牌建任务时会 403 悬空——先解绑
        return jsonify({'error': '主管 %s 还绑着 %d 个令牌（%s）——先解绑，'
                                 '或把 status 改成 retired 停用'
                       % (name, len(bound), '、'.join(bound))}), 409
    counts = _taskCounts(lid)
    if counts['total']:
        # 有任务引用：删了记录，这些任务的归属就说不清了——建议 retired
        return jsonify({'error': '主管 %s 名下有 %d 个任务（%d 个在跑）——'
                                 '先处理掉，或把 status 改成 retired 停用'
                       % (name, counts['total'], counts['running'])}), 409
    leads_store.remove(lid)
    events.log('lead.deleted', name, 'ok', '', request.remote_addr)
    return jsonify({'ok': True})


@blueprint.route('/api/settings/leads/<lid>/tokens', methods=['POST'])
def bindToken(lid):
    lead = leads_store.get(lid)
    if lead is None:
        return jsonify({'error': '主管不存在: %s' % lid}), 404
    data = request.get_json(force=True, silent=True) or {}
    tokenId = str(data.get('tokenId') or '').strip()
    if not tokenId:
        return jsonify({'error': '缺少 tokenId（要绑到该主管的令牌 id）'}), 400
    tokens, entry = _findToken(tokenId)
    if entry is None:
        return jsonify({'error': '令牌不存在: %s' % tokenId}), 404
    current = str(entry.get('lead') or '')
    if current and current != lid:
        other = leads_store.get(current)
        return jsonify({'error': '该令牌已绑到主管 %s——一个令牌最多绑一个'
                                 '主管，先从那边解绑'
                       % ((other or {}).get('name') or current)}), 409
    entry['lead'] = lid
    config.setAgentTokens(tokens)
    events.log('lead.token.bound', lead.get('name') or lid, 'ok',
               'token=%s' % (entry.get('name') or tokenId), request.remote_addr)
    return jsonify({'lead': lead})


@blueprint.route('/api/settings/leads/<lid>/tokens/<tokenId>',
                 methods=['DELETE'])
def unbindToken(lid, tokenId):
    lead = leads_store.get(lid)
    if lead is None:
        return jsonify({'error': '主管不存在: %s' % lid}), 404
    tokens, entry = _findToken(tokenId)
    if entry is None:
        return jsonify({'error': '令牌不存在: %s' % tokenId}), 404
    current = str(entry.get('lead') or '')
    if current and current != lid:
        other = leads_store.get(current)
        return jsonify({'error': '该令牌绑的是主管 %s，不是 %s——要解绑请到'
                                 '那边操作'
                       % ((other or {}).get('name') or current,
                          lead.get('name') or lid)}), 409
    if current == lid:
        entry.pop('lead', None)
        config.setAgentTokens(tokens)
        events.log('lead.token.unbound', lead.get('name') or lid, 'ok',
                   'token=%s' % (entry.get('name') or tokenId),
                   request.remote_addr)
    # 本来就没绑：幂等成功（解绑是收敛操作，不报错）
    return jsonify({'lead': lead})


@blueprint.route('/api/agent/lead')
def agentLead():
    """令牌通道自查（Q4）：任何有效令牌可问"我绑了哪个主管"。

    绑了 → {lead: {id, name, project, kind, parent, status}}；
    没绑 → {lead: null}。绑定悬空（主管记录被手删）同样视作没绑——
    悬空状态由建任务时的 403 说清（规格 Q5 验收 #15）。
    """
    agent = agent_api.agentFromRequest()
    if agent is None:
        return jsonify({'error': 'unauthorized (需要 X-Agent-Token)'}), 401
    lead = leads_store.get(str(agent.get('lead') or ''))
    if lead is None:
        return jsonify({'lead': None})
    return jsonify({'lead': {'id': lead.get('id') or '',
                             'name': lead.get('name') or '',
                             'project': lead.get('project') or '',
                             'kind': lead.get('kind') or '',
                             'parent': lead.get('parent') or '',
                             'status': lead.get('status') or ''}})
