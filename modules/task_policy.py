# -*- coding: utf-8 -*-
"""任务审批边界（R4-1/R4-2）：policy 校验与工具调用判定。

纯函数、无 Flask 依赖；调用方（tasks_runner 审批漏斗）在挂起人工审批前
先走 evaluate()——allow/deny 自动落卡，ask 才推 permission_request。

判定顺序（反馈 R4-1 规格 + 安全补充）:
    1. 命令段命中 denyCommands → ask（deny 的语义是"强制人工"）；
    1b. 命令段或文件工具的路径参数命中敏感文件（config.json / 令牌文件）
        → ask（R5-2 内建 deny，policy 压不掉，read-only 白名单也压不掉）；
    2. 危险模式（ask，即使段首命中 allowCommands）：
       - 原始命令里（单引号内除外）出现 $( 或反引号——双引号不保护命令替换；
       - 剔除引号内容与豁免项（2>&1、>/dev/null、2>/dev/null、1>/dev/null）
         之后仍存在输出重定向 >；
       - find 的 -exec / -delete；xargs 后面跟非只读命令（含 xargs 悬空）；
    3. 只读工具（kind=read 或名称白名单）→ allow；
    4. execute：剔除引号后按两侧带空白的 && || | ; 与换行拆段，每段都命中
       任一 allowCommands → allow；
    5. 写文件工具（edit/delete）的路径全部落在 allowPaths 下 → allow；
    6. 其余按 default（ask/allow/deny）。

启发式边界（文档已声明）：引号剥离不做转义嵌套；命令重定向写（`echo x > f`）
属于命令维度，需命令段全匹配 allowCommands；路径判定前 normpath 归一
（折叠 `..` 与混合斜杠），命令文本里的 `..` 不做处理（走 allowCommands）。
"""
import os
import re

READONLY_KINDS = {'read'}
WRITE_KINDS = {'edit', 'delete'}
# 真实 Kimi 的 request_permission.toolCall 常不带 kind——按工具名兜底
# （真实事件形状：title='Edit'，args={'new_string','old_string','path'}）
WRITE_NAMES = re.compile(r'^(edit|write|multiedit|notebookedit|apply[_-]?patch|'
                         r'str[_-]?replace|replace)\b', re.I)
READONLY_NAMES = re.compile(r'^(read|grep|glob|ls|search|view|todowrite)\b', re.I)
READONLY_XARGS = {'cat', 'head', 'tail', 'grep', 'ls', 'wc', 'file', 'stat',
                  'find', 'echo', 'xargs'}
PATH_KEYS = ('path', 'file_path', 'file', 'notebook_path', 'target')
DANGER_SUBST = re.compile(r'\$\(|`')
STRIP_DQ = re.compile(r'"[^"]*"')
STRIP_SQ = re.compile(r"'[^']*'")
REDIRECT_EXEMPT = ('2>&1', '>/dev/null', '2>/dev/null', '1>/dev/null')
SEGMENT_SPLIT = re.compile(r'\s*(?:&&|\|\||\||;)\s*|\n')
FIND_UNSAFE = re.compile(r'(?:^|[\s;&|])-(?:exec|delete)\b')
XARGS = re.compile(r'\bxargs\b')
FIRST_WORD = re.compile(r'^\s*([A-Za-z_][\w.-]*)')
CD_RE = re.compile(r'\bcd\s+(?:"([^"]+)"|\'([^\']+)"?|([^\s;&|]+))', re.I)
HEREDOC_RE = re.compile(r'<<-?\s*[\'"]?([A-Za-z_][\w.-]*)[\'"]?')
# R5-2 内建 deny：读中枢配置/令牌文件的调用一律 ask——命中即 ask，独立于
# 调用方 denyCommands（policy 写了 allowCommands 也压不过它）。模式：
# 中枢/成员 config.json、.agent-token / agent_node_token 类令牌文件、
# 本机 node 令牌 ~/.deskport-agent-node/token
SENSITIVE_FILE_RE = tuple(re.compile(p, re.I) for p in (
    r'config\.json',
    r'\.agent[_-]?token',
    r'agent[_-]?node[_-]?token',
    r'deskport-agent-node[/\\]token',
))


def _stripHeredocs(cmd):
    """去掉 heredoc 正文（<<TAG 到独占一行的 TAG）：正文是数据，不参与
    引号剥离/命令替换检测/分段——否则正文里的引号与 $( 会误伤判定。"""
    out = cmd
    while True:
        m = HEREDOC_RE.search(out)
        if not m:
            return out
        tag = m.group(1)
        start = out.find('\n', m.end())
        if start < 0:
            return out[:m.start()]
        tm = re.search(r'^%s\s*$' % re.escape(tag), out[start + 1:], re.M)
        if not tm:
            return out[:start]
        out = out[:start + 1] + out[start + 1 + tm.end():]

DEFAULT_POLICY = {'allowPaths': [], 'allowCommands': [], 'denyCommands': [],
                  'default': 'ask', 'timeoutSec': None}


def _strList(value, cap, length, label):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > cap:
        raise ValueError('%s 必须是数组且不超过 %d 项' % (label, cap))
    out = []
    for item in value:
        text = str(item or '').strip()
        if not text:
            continue
        if len(text) > length:
            raise ValueError('%s 单项过长（≤%d 字符）' % (label, length))
        out.append(text)
    return out


def validatePolicy(policy):
    """校验并归一 policy；非法抛 ValueError（消息指出哪一项）。"""
    if policy in (None, {}):
        return {}
    if not isinstance(policy, dict):
        raise ValueError('policy 必须是对象')
    allowPaths = _strList(policy.get('allowPaths'), 20, 500, 'allowPaths')
    default = str(policy.get('default') or 'ask').lower()
    if default not in ('ask', 'allow', 'deny'):
        raise ValueError("policy.default 只能是 ask / allow / deny")
    timeoutSec = policy.get('timeoutSec')
    if timeoutSec is not None:
        try:
            timeoutSec = int(timeoutSec)
        except (TypeError, ValueError):
            raise ValueError('policy.timeoutSec 必须是非负整数（0=不超时）')
        if timeoutSec < 0:
            raise ValueError('policy.timeoutSec 必须是非负整数（0=不超时）')
    allowCommands = []
    for pattern in _strList(policy.get('allowCommands'), 30, 300, 'allowCommands'):
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError('allowCommands 含非法正则 %r: %s' % (pattern, e))
        allowCommands.append(pattern)
    denyCommands = []
    for pattern in _strList(policy.get('denyCommands'), 30, 300, 'denyCommands'):
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError('denyCommands 含非法正则 %r: %s' % (pattern, e))
        denyCommands.append(pattern)
    return {'allowPaths': allowPaths, 'allowCommands': allowCommands,
            'denyCommands': denyCommands, 'default': default,
            'timeoutSec': timeoutSec}


def _underAny(path, allowPaths):
    """前缀判定：先 normpath 折叠 `..`/混合斜杠（R4-2 验收漏洞——
    `root\\..\\other\\x` 曾绕过前缀直落 allow），再按目录边界 + 大小写
    不敏感比较（web-platform-dev-evil 不会误匹配 web-platform-dev）。"""
    p = os.path.normpath(str(path).replace('/', '\\')).lower()
    for root in allowPaths:
        r = os.path.normpath(str(root).replace('/', '\\')).lower().rstrip('\\')
        if p == r or p.startswith(r + '\\'):
            return True
    return False


def _pathsInArgs(args):
    if isinstance(args, dict):
        for key in PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                yield value.strip()
    elif isinstance(args, str):
        for m in re.finditer(r'[A-Za-z]:[\\/][^\s"\']+|/[^\s"\']+', args):
            yield m.group(0)


def _commandText(args):
    if isinstance(args, dict):
        return str(args.get('command') or '')
    if isinstance(args, str):
        return args
    return ''


def _firstWord(segment):
    m = FIRST_WORD.match(segment)
    return m.group(1).lower() if m else ''


def _cdTarget(cmd):
    """命令里第一个 `cd <目录>` 的目标；POSIX 风格 /e/x 转成 E:\\x。"""
    m = CD_RE.search(cmd)
    if not m:
        return ''
    d = next((g for g in m.groups() if g), '').strip('"\'')
    if not d:
        return ''
    if re.match(r'^[A-Za-z]:', d) or d.startswith('\\\\'):
        return d
    if re.match(r'^/[A-Za-z](/|$)', d):
        return d[1].upper() + ':' + d[2:].replace('/', '\\')
    return d


def _redirectTargets(cmd):
    """扫描重定向目标（含 `>>`、双/单引号目标）；单引号内整体是数据。

    调用前 `$( `/反引号已由调用方拦截，`2>&1` 等豁免项已剔除。
    返回 [(target, append)]；扫描不到目标时返回空——调用方按 ask 处理。
    """
    targets = []
    i, n = 0, len(cmd)
    inTarget = False
    buf = ''
    append = False
    inSQ = inDQ = False
    while i < n:
        ch = cmd[i]
        if inTarget:
            if inDQ:
                if ch == '"':
                    targets.append((buf, append))
                    buf = ''
                    inTarget = False
                    inDQ = False
                else:
                    buf += ch
                i += 1
                continue
            if ch == "'":
                i += 1
                continue
            if ch in ' \t':
                if buf:
                    targets.append((buf, append))
                    buf = ''
                inTarget = False
            elif ch in ';&|<':
                if buf:
                    targets.append((buf, append))
                    buf = ''
                inTarget = False
                continue
            else:
                buf += ch
            i += 1
            continue
        if inSQ:
            if ch == "'":
                inSQ = False
            i += 1
            continue
        if ch == '"':
            inDQ = not inDQ
            i += 1
            continue
        if ch == '>':
            j = i + 1
            append = bool(j < n and cmd[j] == '>')
            i = j + 1 if append else j
            while i < n and cmd[i] in ' \t':
                i += 1
            inTarget = True
            buf = ''
            continue
        i += 1
    if buf:
        targets.append((buf, append))
    return targets


def evaluate(policy, kind='', name='', args=None, cwd=None):
    """按 policy 判定一次工具调用。

    返回 (decision, rule)：decision ∈ allow/ask/deny；rule 供 auto_approved/
    auto_denied 事件与审计追溯。ask 永远是安全侧——拿不到参数、解析失败、
    命中 deny 都导向 ask。
    """
    allowPaths = policy.get('allowPaths') or []
    allowRes = [re.compile(p) for p in policy.get('allowCommands') or []]
    denyRes = [re.compile(p) for p in policy.get('denyCommands') or []]
    default = policy.get('default') or 'ask'
    cmd = _commandText(args)

    segments = None
    if cmd.strip():
        cmdNoHeredoc = _stripHeredocs(cmd)
        outsideSingle = STRIP_SQ.sub('', cmdNoHeredoc)
        if DANGER_SUBST.search(outsideSingle):
            return 'ask', 'danger:command-substitution'
        stripped = STRIP_DQ.sub('', STRIP_SQ.sub('', cmdNoHeredoc))
        for exempt in REDIRECT_EXEMPT:
            stripped = stripped.replace(exempt, '')
        if '>' in stripped:
            # 重定向写改进（R4-2b）：目标全部落在 allowPaths 内（相对路径按
            # 命令里 cd 的目录或任务 cwd 解析，解析不出 → ask）时不再一刀切
            # ask；命令本身仍要走 allowCommands 判定
            targets = _redirectTargets(cmdNoHeredoc)
            base = _cdTarget(cmdNoHeredoc) or str(cwd or '')
            resolved = []
            for target, _append in targets:
                p = target.replace('/', '\\')
                if not re.match(r'^[A-Za-z]:[\\/]', p):
                    if not base or p.startswith('~'):
                        return 'ask', 'danger:redirect'
                    p = os.path.normpath(os.path.join(base, p))
                resolved.append(p)
            if not resolved or not all(_underAny(p, allowPaths) for p in resolved):
                return 'ask', 'danger:redirect'
        if FIND_UNSAFE.search(stripped):
            return 'ask', 'danger:find'
        segments = [s for s in SEGMENT_SPLIT.split(stripped) if s.strip()]
        if XARGS.search(stripped):
            # xargs 的消费命令：同段后继词，或（悬空时）下一个段的首词；
            # 拿不到消费命令、或消费命令非只读 → ask
            after = stripped[XARGS.search(stripped).end():]
            consumer = _firstWord(after)
            if not consumer and segments:
                idx = next((i for i, s in enumerate(segments)
                            if XARGS.search(s)), None)
                if idx is not None and idx + 1 < len(segments):
                    consumer = _firstWord(segments[idx + 1])
            if consumer not in READONLY_XARGS:
                return 'ask', 'danger:xargs'
        if any(r.search(seg) for seg in segments for r in denyRes):
            return 'ask', 'deny:command'
        if any(r.search(seg) for seg in segments for r in SENSITIVE_FILE_RE):
            return 'ask', 'deny:sensitive-file'
    else:
        # 非命令工具（Read/Edit/Grep 等）：路径参数命中敏感文件同样 ask
        argsText = args if isinstance(args, str) \
            else ' '.join(_pathsInArgs(args))
        if argsText and any(r.search(argsText) for r in SENSITIVE_FILE_RE):
            return 'ask', 'deny:sensitive-file'

    if kind in READONLY_KINDS or READONLY_NAMES.match(name or ''):
        return 'allow', 'read-only'

    if cmd.strip() and segments and allowRes:
        if all(any(r.search(seg) for r in allowRes) for seg in segments):
            return 'allow', 'allowCommands'

    if kind in WRITE_KINDS or WRITE_NAMES.match(name or ''):
        paths = list(_pathsInArgs(args))
        if paths and all(_underAny(p, allowPaths) for p in paths):
            return 'allow', 'allowPaths'

    return default, 'default'
