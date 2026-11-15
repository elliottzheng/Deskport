# -*- coding: utf-8 -*-
"""指挥者的直接活动（批次 U2 追加）：主管与未登记的派活令牌不派任务也会自己动手——
线上实测有外部智能体某天 0 个任务，却在一小时内几十次 agent.exec / agent.sftp.put。
这些动作走令牌通道，审计日志 control_audit.jsonl 里留痕；本模块只读那份日志，按
令牌名汇总，挂到指挥树的指挥者节点上。

为什么单独一个模块：command_view.py 已贴 400 行预算（docs/code-structure.md：
Python 模块 ≤400 行，超出先拆）。这里只做「读审计 → 按令牌名汇总 → 挂到树上」，
纯数据、不 import Flask，command_view 只在返回前包一层 decorate()。

匹配规则（审计条目两种形状，都以令牌名为准）：
    agent.exec / agent.sftp.* / agent.upload   detail 前缀 '[令牌名] '
        （modules/agent_api.py auditAgentAction）
    agent.task.*                               target 就是令牌名
        （modules/tasks_api.py 建任务/上传/改名处）
只认这四类「直接操作」：agent.exec（含 agent.exec.session）/ agent.sftp.* /
agent.task.* / agent.session.*；其余（agent.token.*、agent.scope、files.*、
联邦来源）与本批无关。已知缺口：agent.session.open 的审计条目不带令牌名（只有
on_behalf_of），归不到具体指挥者，只能跳过——要补记得在写入侧加令牌名。

读法：从文件尾倒序扫最近 TAIL_LINES 行（块状往回读，不全量载入内存）；结果按
CACHE_SEC 缓存（指挥台 5 秒一刷，不能每轮重扫日志）。窗口与时间由调用方给
（command_view 的 now 可被测试冻结），本模块自己只用 monotonic 管缓存时钟。
"""
import json
import os
import time

import config
from modules import events

TAIL_LINES = 5000            # 只从文件尾倒序扫这么多行
TAIL_CHUNK = 65536           # 倒序读的块大小
CACHE_SEC = 10               # 汇总结果缓存（指挥台 5 秒一刷）
RECENT_KEEP = 10             # 每个指挥者留最近几条明细
WINDOW_SEC = 3600            # 「最近 60 分钟」计数窗口
PRESENCE_SEC = 24 * 3600     # 24 小时内有直接操作，没任务也上线成一级节点
SUMMARY_MAX = 60             # 单条摘要截断
# 面板自己派的活（tasks_api 建任务处同一字面量；command_view 也有一份——那边
# import 本模块，不能反向依赖，只能各写一份）
PANEL_OWNER = '面板'
# 动作类型 → 审计 action 前缀（agent.exec.session 归 exec：它就是在跑命令）
KINDS = (('exec', 'agent.exec'), ('sftp', 'agent.sftp'),
         ('task', 'agent.task'), ('session', 'agent.session'))

_CACHE = {'mono': 0.0, 'index': {}}


def tailLines(path, limit=TAIL_LINES):
    """从文件尾倒序读最近 limit 行（JSONL 追加写，新的在末尾）。

    块状往回读：每块读回来先按换行切开，第一段可能被截断就留给下一块拼，够
    limit 行或读到文件头就停——日志长到几百 MB 也不会全量载入内存。
    """
    lines = []
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            pos, buf = f.tell(), b''
            while pos > 0 and len(lines) < limit:
                step = min(TAIL_CHUNK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                parts = buf.split(b'\n')
                buf = parts.pop(0)          # 第一段可能不完整，留给下一块拼
                for raw in reversed(parts):
                    if not raw.strip():
                        continue           # 行尾换行留下的空片段
                    lines.append(raw)
                    if len(lines) >= limit:
                        break
            if pos == 0 and len(lines) < limit and buf.strip():
                lines.append(buf)           # 文件最前面那一行（这时已完整）
    except OSError:
        return []
    lines.reverse()
    return [line.decode('utf-8', 'replace') for line in lines if line.strip()]


def _blank():
    return {'exec': 0, 'sftp': 0, 'writes': 0, 'task': 0, 'session': 0,
            'lastAt': 0, 'lastExec': '', 'recent': []}


def _kindOf(action):
    for kind, prefix in KINDS:
        if action.startswith(prefix):
            return kind
    return ''


def _nameOf(entry):
    """审计条目 → 令牌名；归不上因的返回 ''（面板、联邦、非智能体动作）。"""
    action = str(entry.get('action') or '')
    if not action.startswith('agent.'):
        return ''
    detail = str(entry.get('detail') or '')
    if detail.startswith('['):
        end = detail.find('] ')
        if end > 1:
            return detail[1:end]
    if action.startswith('agent.task'):
        name = str(entry.get('target') or '').strip()
        return '' if name == PANEL_OWNER else name    # 面板自己不是指挥者
    return ''


def _stripName(text, name):
    prefix = '[%s] ' % name
    return text[len(prefix):] if text.startswith(prefix) else text


def _summaryOf(entry, name):
    """单条摘要：exec 取命令、task 取 detail，其余取 target（路径/主机）。"""
    action = str(entry.get('action') or '')
    if action.startswith('agent.exec') or action.startswith('agent.task'):
        return _stripName(str(entry.get('detail') or ''), name)[:SUMMARY_MAX]
    return str(entry.get('target') or '')[:SUMMARY_MAX]


def _atOf(entry):
    """审计时间 'YYYY-MM-DD HH:MM:SS'（本地时间写的）→ epoch 秒；坏数据 0。"""
    try:
        return int(time.mktime(time.strptime(str(entry.get('at') or '')[:19],
                                             '%Y-%m-%d %H:%M:%S')))
    except (ValueError, OverflowError):
        return 0


def _scan(now):
    index = {}
    for line in tailLines(events.eventPath()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue                   # 半行/坏行：跳过，不让一条脏数据废掉整片
        if not isinstance(entry, dict):
            continue
        name = _nameOf(entry)
        kind = _kindOf(str(entry.get('action') or ''))
        if not name or not kind:
            continue
        at = _atOf(entry)
        slot = index.setdefault(name, _blank())
        slot['recent'].append({'at': at, 'action': str(entry.get('action') or ''),
                               'summary': _summaryOf(entry, name)})
        del slot['recent'][:-RECENT_KEEP]
        if at >= slot['lastAt']:       # 日志追加写，后面的就是更新的
            slot['lastAt'] = at
            if kind == 'exec':
                slot['lastExec'] = _summaryOf(entry, name)
        if 0 <= now - at <= WINDOW_SEC:
            slot[kind] += 1
            if str(entry.get('action') or '') == 'agent.sftp.put':
                slot['writes'] += 1
    return index


def activityIndex(now):
    """令牌名 → 直接活动汇总（10 秒缓存；窗口用调用方给的 now）。"""
    if time.monotonic() - _CACHE['mono'] < CACHE_SEC:
        return _CACHE['index']
    index = _scan(now)
    _CACHE.update(mono=time.monotonic(), index=index)
    return index


def resetCache():
    """清缓存（测试用：同一进程里连换多份日志时不读到上一份的残影）。"""
    _CACHE.update(mono=0.0, index={})


def _tokenNames(leads):
    """主管 id → 它名下的令牌名（绑定存在 config.agentTokens[].lead 上）。"""
    out = {}
    for token in config.getAgentTokens() or []:
        leadId = str((token or {}).get('lead') or '')
        name = str((token or {}).get('name') or '').strip()
        if leadId and name:
            out.setdefault(leadId, []).append(name)
    return out


def nodeIndex(leads, now):
    """指挥树节点 key → activity 槽位（key 形如 'lead:<id>' / 'owner:<令牌名>'）。

    令牌按绑定归到主管节点（主管停用时树上同 key 是 orphan 组，照样挂得上）；
    没绑主管的令牌（未登记的指挥者）归 owner 节点。槽位里带 fresh（最近
    PRESENCE_SEC 内有过直接操作）——据此决定「没有任务也要上线成一级节点」。
    """
    index = activityIndex(now)
    byLead = _tokenNames(leads)
    bound = {name for names in byLead.values() for name in names}
    out = {}
    for leadId, names in byLead.items():
        for name in names:
            if name in index:
                # 绑到主管名下就归那个节点——主管停用时树上同 key 是 orphan 组
                out['lead:' + leadId] = index[name]
    for name, slot in index.items():
        if name not in bound:
            out['owner:' + name] = slot
    for slot in out.values():
        slot['fresh'] = slot['lastAt'] > 0 and now - slot['lastAt'] <= PRESENCE_SEC
    return out


def decorate(tree, index):
    """给指挥树挂 activity：各层 lead/owner 节点补槽位；只有直接操作、没有任务
    的令牌补成一级 owner 节点（插在「你直接派的」前面，保持规格的一级顺序）。"""
    def walk(node):
        slot = index.get(node.get('key') or '')
        if slot is not None:
            node['activity'] = slot
        for kid in node.get('children') or []:
            walk(kid)

    for node in tree:
        walk(node)
    extra = []
    for key, slot in index.items():
        if not key.startswith('owner:') or not slot.get('fresh'):
            continue
        if any(n.get('key') == key for n in tree):
            continue                   # 已经有任务的同名分组，不重复上线
        name = key[len('owner:'):]
        extra.append({'type': 'owner', 'key': key, 'id': name, 'name': name,
                      'counts': {'running': 0, 'waiting': 0, 'waiting_you': 0,
                                 'failed': 0},
                      'closedHidden': 0, 'children': [], 'activity': slot})
    at = len(tree)
    for i, node in enumerate(tree):
        if node.get('type') == 'direct':
            at = i
            break
    tree[at:at] = extra
    return tree
