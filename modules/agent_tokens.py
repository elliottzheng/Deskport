# -*- coding: utf-8 -*-
"""智能体令牌管理面 — 设置页「智能体令牌」卡片（网页会话态，非令牌接口）。

签发/列表/吊销 config.json 的 agentTokens，替代手改 JSON（先生成随机串、
再算 SHA-256、再粘配置）：手滑一次就全 401，且网页上看不到有哪些令牌、
没法吊销。约束：

- 明文只在签发（POST）响应里出现一次，不落盘、不再回显；服务端只存
  SHA-256（tokenHash），GET 列表绝不含 hash 或明文；
- 三档权限：缺省 scopes = 全权；显式 ["upload","share"] / ["share"]，
  其它组合 400 并列出可选（与 agent_api.agentHasScope 的白名单语义一致）；
- 名称 1-24 字符、不含 < > " ' 与换行、不可重名；总数上限 10 条；
- 不许删最后一条全权令牌——否则可能把自己锁在门外，400 并说明原因；
- 写完即热生效：config.setAgentTokens 原子写 + 失效 mtime 缓存，
  agentFromRequest 下一读就是新值，无需重启；
- 审计 agent.token.created / agent.token.revoked（记令牌名与权限档）。

鉴权走 authGuard 的网页会话分支：/api/settings/* 不在智能体令牌通道
（/api/agent/*、/api/tasks*、/api/worklines）内，设备令牌/智能体令牌
打这些管理接口一律 401，与设置页其他卡片一致。
"""
import datetime
import secrets

from flask import Blueprint, jsonify, request

import config
from modules import events
from modules.agent_api import SCOPE_FULL, SCOPE_SHARE, SCOPE_UPLOAD, tokenHash

blueprint = Blueprint('agent_tokens', __name__)

MAX_TOKENS = 10
MAX_NAME = 24
BAD_NAME_CHARS = '<>"\'\r\n\t'

# 可显式声明的两档（缺省 scopes = 全权，见 agent_api.agentHasScope）；
# 顺序无关，重复或混入 full/upload_remote 一律拒（只认这两个集合）。
ALLOWED_SCOPES = ([SCOPE_UPLOAD, SCOPE_SHARE], [SCOPE_SHARE])
SCOPES_HELP = 'scopes 只支持三种：省略（全权）/ ["upload","share"] / ["share"]'


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _isFull(entry):
    """缺省/空 scopes = 全权（向后兼容）；显式含 'full' 同样全权。"""
    scopes = entry.get('scopes')
    if not isinstance(scopes, list) or not scopes:
        return True
    return SCOPE_FULL in [str(s).strip() for s in scopes]


def _cleanScopes(raw):
    """返回 (scopes, error)：None = 全权（键缺席）；只认两档显式白名单。"""
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, SCOPES_HELP
    items = []
    for item in raw:
        if not isinstance(item, str) or item not in (SCOPE_UPLOAD, SCOPE_SHARE) \
                or item in items:
            return None, SCOPES_HELP
        items.append(item)
    for allowed in ALLOWED_SCOPES:
        if sorted(items) == sorted(allowed):
            return items, None
    return None, SCOPES_HELP


def _public(entry):
    """列表视图：绝不含 tokenHash，明文签发后永不回显。

    lead（批次 Q）：绑定的主管 id；没绑为 ''。键一直都在——没绑主管的
    令牌行为零变化，只是多一个空串字段。
    """
    scopes = entry.get('scopes')
    return {'id': entry.get('id'), 'name': entry.get('name'),
            'scopes': [str(s) for s in scopes] if isinstance(scopes, list) else None,
            'createdAt': entry.get('createdAt') or '',
            'lead': entry.get('lead') or ''}


@blueprint.route('/api/settings/agent-tokens', methods=['GET'])
def listAgentTokens():
    return jsonify({'tokens': [_public(t) for t in config.getAgentTokens()]})


@blueprint.route('/api/settings/agent-tokens', methods=['POST'])
def createAgentToken():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get('name') or '').strip()
    if not name or len(name) > MAX_NAME or any(c in name for c in BAD_NAME_CHARS):
        return jsonify({'error': '名称需为 1-%d 个字符, 不能含 < > " \' '
                                 '或换行' % MAX_NAME}), 400
    scopes, scopeErr = _cleanScopes(data.get('scopes'))
    if scopeErr:
        return jsonify({'error': scopeErr}), 400
    tokens = config.getAgentTokens()
    if any(str(t.get('name') or '').strip() == name for t in tokens):
        return jsonify({'error': '已存在同名令牌: %s（换个名字，审计才分得清'
                                 '是谁）' % name}), 400
    if len(tokens) >= MAX_TOKENS:
        return jsonify({'error': '智能体令牌最多 %d 个, 请先吊销不用的'
                                 % MAX_TOKENS}), 400
    token = secrets.token_urlsafe(32)   # 明文只进这次响应, 不写任何文件
    entry = {'id': secrets.token_hex(8), 'name': name,
             'tokenHash': tokenHash(token), 'createdAt': _now()}
    if scopes is not None:
        entry['scopes'] = scopes
    tokens.append(entry)
    config.setAgentTokens(tokens)
    events.log('agent.token.created', name, 'ok',
               'scopes=%s' % (','.join(scopes) if scopes else 'full'),
               request.remote_addr)
    return jsonify({'id': entry['id'], 'token': token, 'name': name,
                    'scopes': scopes})


@blueprint.route('/api/settings/agent-tokens/<tid>', methods=['DELETE'])
def revokeAgentTokens(tid):
    tokens = config.getAgentTokens()
    hit = next((t for t in tokens if str(t.get('id') or '') == str(tid)), None)
    if hit is None:
        return jsonify({'error': '令牌不存在'}), 404
    if _isFull(hit) and sum(1 for t in tokens if _isFull(t)) <= 1:
        return jsonify({'error': '不能吊销最后一条全权令牌（否则可能把自己锁在'
                                 '门外）；请先新建一条全权令牌再删这条'}), 400
    config.setAgentTokens([t for t in tokens if t is not hit])
    events.log('agent.token.revoked', str(hit.get('name') or ''), 'ok', '',
               request.remote_addr)
    return jsonify({'ok': True})
