# -*- coding: utf-8 -*-
"""已知敏感值注册表（R5-2）：中枢持有的明文令牌在写事件流前统一脱敏。

来源（只收明文，tokenHash 这类指纹收不到也不收；<12 字符不纳入防误伤）：
- config.json：remotes[].token、agentNodes[].token（远端 node 令牌中枢侧
  持有的那份）、federationToken、password、decisionApiKey
- 本机 agent node 令牌文件 ~/.deskport-agent-node/token（config 与 node
  共享的那份，只读不创建）
config.json 热改（mtime 变化）即自动重建，无需重启。

exec / sftp/get 等 API 响应不经此处——那是主控显式取数据，脱敏反而破坏语义。
"""
import os
import threading

import config

MIN_LEN = 12   # 短于这个长度不纳入：误伤正常文本的代价大于泄露价值
TOKEN_FILE = os.path.join(os.path.expanduser('~'), '.deskport-agent-node',
                          'token')
REDACT_TYPES = frozenset(('tool', 'tool_result', 'assistant',
                          'permission_request'))

_lock = threading.Lock()
_cache = {'key': None, 'pairs': ()}   # pairs: ((value, label), ...) 长值优先


def _readTokenFile(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


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
    # 长值优先：一个令牌是另一个的子串时，先替换长的才不会留下残段
    return tuple(sorted(pairs.items(), key=lambda kv: -len(kv[0])))


def _statKey():
    key = []
    for path in (config.CONFIG_PATH, TOKEN_FILE):
        try:
            key.append(os.stat(path).st_mtime_ns)
        except OSError:
            key.append(0)
    return tuple(key)


def refresh():
    """config.json / 令牌文件 mtime 变化时重建敏感值集合（热读跟随）。"""
    key = _statKey()
    with _lock:
        if key == _cache['key']:
            return
    pairs = _collectPairs()
    with _lock:
        _cache['key'] = key
        _cache['pairs'] = pairs


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
