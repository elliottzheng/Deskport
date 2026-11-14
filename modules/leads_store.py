# -*- coding: utf-8 -*-
"""主管记录存储（批次 Q P1）：{id: record} 一张小表，原子写。

主管 = 指挥链里的纵向节点（设计见 docs/plan/agent-chain-of-command.md）：
令牌绑到主管上，用该令牌建的任务自动记 task.lead——审计分得清"谁派了
谁"。本批只做记录 + 绑定查询 + 建任务闸门；收件箱、上限、子树权限、
托管主管都是后续批次。

存储: config.statePath('leads.json')，state.atomicWriteJson 原子写。
绑定本身不在这里——它存在 config.agentTokens[].lead 上（吊销令牌自然
解绑），本模块只管主管记录。

不 import Flask: authGuard 每次令牌鉴权都要调 touch()（最近活跃），
保持纯状态层；名字校验复制 agent_tokens 的字面量（那边 import flask，
反向依赖会把 Flask 拖进每条请求的导入路径）。

记录形状（taskId/workline/ceiling 本批只占位，恒空）：
    {"id": "ld_<8位hex>", "name": "...", "project": "", "kind": "external",
     "parent": "human", "status": "active", "taskId": "", "workline": "",
     "ceiling": {}, "createdAt": <秒>, "updatedAt": <秒>, "lastSeenAt": 0}
"""
import json
import secrets
import threading
import time

import config
from modules import state

MAX_NAME = 24                      # 与 agent_tokens.MAX_NAME 同一套（令牌名限制）
BAD_NAME_CHARS = '<>"\'\r\n\t'     # 同上，字面量复制（理由见模块 docstring）
MAX_PROJECT = 40
KINDS = ('external', 'hosted')     # 本批只存不用，缺省 external
PARENTS = ('human',)               # 本批只接受 human
STATUSES = ('active', 'paused', 'retired')
SEEN_INTERVAL = 60                 # 同一主管 lastSeenAt 最多每 60s 写盘一次（Q7）
NOT_FOUND = '主管不存在'

_LOCK = threading.RLock()
# 节流状态在进程内存（重启清零 = 重启后首个请求必写一次，可接受）
_seenAt = {}                       # leadId -> 上次写盘时间（time.time）


def storePath():
    return config.statePath('leads.json')


def _now():
    return time.time()


def _load():
    try:
        with open(storePath(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    state.atomicWriteJson(storePath(), data)


def _newId(data):
    while True:
        leadId = 'ld_' + secrets.token_hex(4)
        if leadId not in data:
            return leadId


def _nameTaken(data, name, exceptId=''):
    """非 retired 的主管里不重名（retired 的名字可回收）。"""
    for leadId, lead in data.items():
        if leadId == exceptId or not isinstance(lead, dict):
            continue
        if (lead.get('status') or '') == 'retired':
            continue
        if str(lead.get('name') or '').strip() == name:
            return True
    return False


def cleanName(raw):
    """返回 (name, error)：与令牌名同一套校验（1–24、无 < > " ' 与换行）。"""
    if not isinstance(raw, str):
        return '', 'name 需为 1-%d 个字符的字符串' % MAX_NAME
    name = raw.strip()
    if not name or len(name) > MAX_NAME \
            or any(c in name for c in BAD_NAME_CHARS):
        return '', ('name 需为 1-%d 个字符, 不能含 < > " \' 或换行' % MAX_NAME)
    return name, ''


def cleanProject(raw):
    """返回 (project, error)：≤40 字符，可空（None 归一成 ''）。"""
    if raw is None:
        return '', ''
    if not isinstance(raw, str):
        return '', 'project 需为字符串（可空，上限 %d 字符）' % MAX_PROJECT
    project = raw.strip()
    if len(project) > MAX_PROJECT:
        return '', 'project 过长（上限 %d 字符）' % MAX_PROJECT
    return project, ''


def get(leadId):
    """按 id 取记录（每次从盘上读，拿到的是私有副本）；不存在返回 None。"""
    leadId = str(leadId or '')
    if not leadId:
        return None
    with _LOCK:
        lead = _load().get(leadId)
    return lead if isinstance(lead, dict) else None


def list():
    """全部记录（含 retired——停用历史要在列表里可见）。"""
    with _LOCK:
        return [dict(lead) for lead in _load().values() if isinstance(lead, dict)]


def create(name, project='', kind='external'):
    """建主管记录。返回 (record, error)——校验失败 record 为 None。"""
    name, err = cleanName(name)
    if err:
        return None, err
    project, err = cleanProject(project)
    if err:
        return None, err
    kind = str(kind or '').strip() or 'external'
    if kind not in KINDS:
        return None, 'kind 只支持 %s（本批只存不用）' % ' / '.join(KINDS)
    with _LOCK:
        data = _load()
        if _nameTaken(data, name):
            return None, ('已存在同名主管: %s（换个名字，审计才分得清是谁）'
                          % name)
        lead = {'id': _newId(data), 'name': name, 'project': project,
                'kind': kind, 'parent': 'human', 'status': 'active',
                'taskId': '', 'workline': '', 'ceiling': {},
                'createdAt': _now(), 'updatedAt': _now(), 'lastSeenAt': 0}
        data[lead['id']] = lead
        _save(data)
        return lead, ''


def update(leadId, patch):
    """改 name/project/status（patch 只许这三个键，由调用方校验键集）。

    返回 (record, error)：NOT_FOUND 表示记录没了（调用方映射 404），
    其余是校验失败（400）。
    """
    leadId = str(leadId or '')
    with _LOCK:
        data = _load()
        lead = data.get(leadId)
        if not isinstance(lead, dict):
            return None, NOT_FOUND
        if 'name' in patch:
            name, err = cleanName(patch.get('name'))
            if err:
                return None, err
            if _nameTaken(data, name, leadId):
                return None, ('已存在同名主管: %s（换个名字，审计才分得清'
                              '是谁）' % name)
            lead['name'] = name
        if 'project' in patch:
            project, err = cleanProject(patch.get('project'))
            if err:
                return None, err
            lead['project'] = project
        if 'status' in patch:
            status = str(patch.get('status') or '').strip()
            if status not in STATUSES:
                return None, 'status 只支持 %s' % ' / '.join(STATUSES)
            lead['status'] = status
        lead['updatedAt'] = _now()
        _save(data)
        return lead, ''


def remove(leadId):
    """删记录；不存在返回 False。调用方负责先检查任务引用与令牌绑定。"""
    with _LOCK:
        data = _load()
        if leadId not in data:
            return False
        del data[leadId]
        _save(data)
        return True


def touch(leadId):
    """令牌通道鉴权时刷新主管 lastSeenAt（Q7）；60 秒内最多写盘一次。

    记录不存在（悬空绑定/已删）时什么都不做——不写盘、不新建记录，
    免得一次悬空请求把空表写上去盖掉真数据。
    """
    leadId = str(leadId or '')
    if not leadId:
        return
    now = _now()
    with _LOCK:
        if now - _seenAt.get(leadId, 0.0) < SEEN_INTERVAL:
            return
        _seenAt[leadId] = now
        data = _load()
        lead = data.get(leadId)
        if not isinstance(lead, dict):
            return
        lead['lastSeenAt'] = now
        _save(data)
