# -*- coding: utf-8 -*-
"""已知敏感值注册表（R5-2）：中枢持有的明文令牌在写事件流前统一脱敏。

来源（只收明文，tokenHash 这类指纹收不到也不收；<12 字符不纳入防误伤）：
- config.json：remotes[].token、agentNodes[].token（远端 node 令牌中枢侧
  持有的那份）、federationToken、password、decisionApiKey
- 本机 agent node 令牌文件 ~/.deskport-agent-node/token（config 与 node
  共享的那份，只读不创建）
- 批次 A：所有 kind=hosted 主管的任务工作区里的 deskport/.agent-token
  （一键建托管主管时写的令牌明文，与本机 node 令牌文件同一种"读文件收集"）
config.json 热改（mtime+size 变化）即自动重建，托管令牌文件的新建/替换同样
纳入 key，无需重启；重建判定带 1 秒粒度的 stat 缓存（STAT_TTL_SEC），窗口内
的调用不碰文件系统——append_event 每事件都过这里，缓存是 p50 的主来源。

exec / sftp/get 等 API 响应不经此处——那是主控显式取数据，脱敏反而破坏语义。
"""
import os
import threading
import time

import config

MIN_LEN = 12   # 短于这个长度不纳入：误伤正常文本的代价大于泄露价值
TOKEN_FILE = os.path.join(os.path.expanduser('~'), '.deskport-agent-node',
                          'token')
REDACT_TYPES = frozenset(('tool', 'tool_result', 'assistant',
                          'permission_request', 'result'))

# 批 PS2 追件：stat 缓存粒度。append_event 每事件都过
# redactText→pairs()→refresh()；无缓存时每次都要 stat 来源文件 + 列 leads
# 存储（读 leads.json、makedirs、任务回填），实测 ~3.7ms/次，是 append_event
# p50 的主要来源。缓存后窗口内的调用只做一次单调钟比较、不碰文件系统
# （~0.05ms）；来源变化最长 1 秒后被感知（TTL 到期即重新 stat，mtime+size
# 变了才重建）。托管令牌刚落盘走 invalidate()，会连 TTL 一起冲掉。
STAT_TTL_SEC = 1.0

_lock = threading.Lock()
_cache = {'key': None, 'pairs': ()}   # pairs: ((value, label), ...) 长值优先
_statLock = threading.Lock()
_statCache = {'at': 0.0}              # 上次 stat 时刻（单调钟）


def _readTokenFile(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _hostedTokenFiles():
    """托管主管的令牌文件清单（批次 A）：[(path, label)]，读失败/无记录 = 空。

    来源 = 所有 kind=hosted 主管的任务工作区里的 deskport/.agent-token（A1-4
    只往这一个文件写明文）。函数内延迟 import：tasks_store 模块级 import 本
    模块（事件脱敏），顶层互相 import 会成加载环。任何一步异常都吞掉返回空
    ——脱敏收集失败不能影响 redactText 的调用方（任务主链路）。
    """
    try:
        from modules import leads_store, tasks_store
        tasks_store.repo.load()
        out = []
        for lead in leads_store.list():
            if str(lead.get('kind') or '') != 'hosted' or not lead.get('taskId'):
                continue
            task = tasks_store.repo.get(str(lead.get('taskId') or ''))
            workspace = str((task or {}).get('workspace') or '')
            if not workspace:
                continue
            out.append((os.path.join(workspace, 'deskport', '.agent-token'),
                        'hostedLead.%s.token'
                        % (lead.get('name') or lead.get('id') or '?')))
        return out
    except Exception:
        return []


def _collectPairs():
    pairs = {}

    def add(value, label):
        value = str(value or '')
        if len(value) >= MIN_LEN and value not in pairs:
            pairs[value] = label

    cfg = config.load()
    for r in cfg.get('remotes') or []:
        if isinstance(r, dict):
            add(r.get('token'), 'remotes.%s.token'
                % (r.get('name') or r.get('id') or '?'))
    for n in cfg.get('agentNodes') or []:
        if isinstance(n, dict):
            add(n.get('token'), 'agentNodes.%s.token'
                % (n.get('name') or '?'))
    add(cfg.get('federationToken'), 'federationToken')
    add(cfg.get('password'), 'password')
    add(cfg.get('decisionApiKey'), 'decisionApiKey')
    add(_readTokenFile(TOKEN_FILE), 'localNode.token')
    for path, label in _hostedTokenFiles():
        add(_readTokenFile(path), label)
    # 长值优先：一个令牌是另一个的子串时，先替换长的才不会留下残段
    return tuple(sorted(pairs.items(), key=lambda kv: -len(kv[0])))


def _statKey():
    key = []
    for path in (config.CONFIG_PATH, TOKEN_FILE):
        try:
            st = os.stat(path)
            key.append((st.st_mtime_ns, st.st_size))
        except OSError:
            key.append((0, 0))
    # 批次 A：托管令牌文件不在 config 里，新建/替换要能触发重建
    for path, _label in _hostedTokenFiles():
        try:
            st = os.stat(path)
            key.append((st.st_mtime_ns, st.st_size))
        except OSError:
            key.append((0, 0))
    return tuple(key)


def refresh():
    """config.json / 令牌文件 mtime+size 变化时重建敏感值集合（热读跟随）。

    批 PS2 追件：1 秒粒度的 stat 缓存——窗口内的调用直接复用上次结论
    （一次单调钟比较，不碰文件系统，也就没有每次调用的 makedirs/读盘）；
    TTL 到期才重新 stat，key 变了才重建。invalidate() 冲掉 _cache['key']
    的同时也冲掉 TTL，托管令牌刚落盘后下一次调用必然重新收集。
    """
    now = time.monotonic()
    with _statLock:
        if now - _statCache['at'] < STAT_TTL_SEC:
            return
        _statCache['at'] = now
    key = _statKey()
    with _lock:
        if key == _cache['key']:
            return
    pairs = _collectPairs()
    with _lock:
        _cache['key'] = key
        _cache['pairs'] = pairs


def invalidate():
    """强制下次 pairs() 重建（批次 A：托管令牌刚落盘，mtime 可能同秒）。"""
    with _statLock:
        _statCache['at'] = 0.0        # 连 TTL 一起冲掉，否则窗口内的
    with _lock:                        # refresh() 会直接返回、看不到新令牌
        _cache['key'] = None


def pairs():
    refresh()
    with _lock:
        return _cache['pairs']


def redactText(text):
    """文本里出现的已知敏感值 → <redacted:来源>。"""
    if not text:
        return text
    text = str(text)
    for value, label in pairs():
        if value in text:
            text = text.replace(value, '<redacted:%s>' % label)
    return text


def redactValue(obj):
    """递归脱敏 dict/list 里的所有字符串（重建结构，不改原对象）。"""
    if isinstance(obj, str):
        return redactText(obj)
    if isinstance(obj, dict):
        return {k: redactValue(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redactValue(v) for v in obj)
    return obj


def redactEvent(etype, payload, full=None):
    """事件落卡前的脱敏入口（R5-2）：执行侧四类事件（tool/tool_result/
    assistant/permission_request，含直通 jsonl 流水的 full 全文）统一脱敏；
    user/status 等主控显式写入的内容原样放行；exec/sftp/get 响应不经此路径。"""
    if etype not in REDACT_TYPES:
        return payload, full
    payload = redactValue(payload)
    if full is not None:
        full = redactText(full)
    return payload, full


def sources():
    """已纳入的敏感值来源名（调试/交付用，只有名字没有值）。"""
    return [label for _value, label in pairs()]


try:   # 启动即加载（import 路径即 web 启动路径）；失败不挡启动
    refresh()
except Exception:
    pass
