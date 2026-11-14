# -*- coding: utf-8 -*-
"""托管主管边界内的只读命令自动放行（批次 A 二次返工，主控拍板）。

背景（例 11 / 复验问题 2）：托管主管自己的会话跑
`cd "<工作区>" && cat deskport/.deskport.json` 这类只读命令，按
allowCommands 逐段判定不命中 → ask → 主管自己的任务不写收件箱、不唤醒
→ 无人干预链静默停摆。主控决定把托管主管的边界放宽成「只读命令在自己
的范围内自动放行」：

- 命令白名单：cat/type/head/tail/ls/dir/find（不带 -exec/-delete/-fprint
  等写或执行参数）/grep/rg/wc/file/stat（rg 不带 --pre 之类执行参数）；
  命令名不带路径前缀——`<工作区>/cat` 是执行自写程序，不算只读命令；
- 前提：**所有路径参数**（含 cd 段的目标）都落在主管工作区、或建主管时
  给的项目目录 cwd 之内。规范化后判断：`..` 折叠、符号链接（realpath
  解析后越界）、绝对路径越界都算越界；解析不出的一律询问（ask）；
- 重定向：只放行 2>/dev/null / 2>&1（管道上下游都认，复验建议改的那
  条），其它重定向（>、>>、<、2>文件…）仍询问；
- 读主管自己的 deskport/.agent-token 仍然询问：task_policy 的敏感文件
  内建 deny 先于本模块生效（evaluate 前段按剥引号的 segments 查），且
  调用方在入口处按未剥引号原文再查一遍——引号里的敏感路径剥引号判定
  看不见，不能漏进白名单。

只对 policy.readonlyRoots 非空的任务生效（一键托管主管自己的任务，
leads_hosted 建卡时写入）；普通任务的 allowCommands 行为逐字不变。

本模块还承载政策家族共用的两块路径语义（2026-11-15 从 task_policy 移来
腾 400 行预算，公开名）：underAny 前缀判定（重定向/写文件两处闸门在用）
与 SENSITIVE_FILE_RE 敏感文件模式（R5-2 内建 deny）。
"""
import os
import re

from modules import task_policy_pipe

READONLY_CMDS = ('cat', 'type', 'head', 'tail', 'ls', 'dir', 'find',
                 'grep', 'rg', 'wc', 'file', 'stat')
# find 的写/执行动作：-exec/-execdir/-delete/-ok/-okdir 执行命令，
# -fprint/-fprintf 写文件（task_policy 的 FIND_UNSAFE 已拦 exec/delete，
# 这里补全其余——消费段与只读段都要过这一遍）
FIND_WRITE_RE = re.compile(
    r'^-+(?:exec|execdir|delete|ok|okdir|fprint|fprintf)\b', re.I)
RG_EXEC_RE = re.compile(r'^--pre(?:-glob)?$')     # rg 的预处理执行参数
ABS_RE = re.compile(r'^[A-Za-z]:[\\/]|^\\\\|^/')
STRIP_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
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


def underAny(path, allowPaths):
    """前缀判定：先 normpath 折叠 `..`/混合斜杠（R4-2 验收漏洞——
    `root\\..\\other\\x` 曾绕过前缀直落 allow），再按目录边界 + 大小写
    不敏感比较（web-platform-dev-evil 不会误匹配 web-platform-dev）。"""
    p = os.path.normpath(str(path).replace('/', '\\')).lower()
    for root in allowPaths:
        r = os.path.normpath(str(root).replace('/', '\\')).lower().rstrip('\\')
        if p == r or p.startswith(r + '\\'):
            return True
    return False


def _real(path):
    """规范化 + 解析符号链接；失败返回 ''（调用方按不可判定 → 询问）。"""
    try:
        return os.path.realpath(os.path.normpath(str(path)))
    except OSError:
        return ''


def _resolve(path, base):
    """把命令里的路径参数解析成规范化绝对路径：绝对路径原样处理；相对
    路径按 base（cd 目标或任务工作区）拼。base 为空（相对路径无处可
    解析）或以 ~ 开头（家目录不可判定）→ ''。"""
    p = str(path or '').strip()
    if not p or p.startswith('~') or '$' in p:
        return ''             # 家目录 / 变量替换：展开结果不可判定
    if not ABS_RE.match(p):
        if not base:
            return ''
        p = os.path.join(str(base), p)
    return _real(p)


def _tokens(seg):
    """按空白拆 token：单/双引号内的空白不分词、引号本身剥掉；引号不
    配平 → None（不可判定，调用方按询问处理）。"""
    out, buf, quote = [], '', ''
    for ch in seg:
        if quote:
            if ch == quote:
                quote = ''
            else:
                buf += ch
        elif ch in '"\'':
            quote = ch
        elif ch.isspace():
            if buf:
                out.append(buf)
                buf = ''
        else:
            buf += ch
    if quote:
        return None          # 引号不配平：不可判定
    if buf:
        out.append(buf)
    if not out:
        return None
    return out


def _hasRedirect(seg):
    """段内是否存在非豁免重定向（>、>>、<、2>文件…；2>/dev/null 与 2>&1
    豁免，管道上下游都认——复验建议改的那条）。引号内是数据，先剥掉。"""
    text = STRIP_QUOTED.sub('', seg)
    for exempt in task_policy_pipe.STDERR_EXEMPT:
        text = text.replace(exempt, '')
    return '>' in text or '<' in text


def _segmentOk(seg, cur, roots):
    """单段判定，返回 (是否放行, 新的当前目录)。cd 段校验目标在根内并
    更新当前目录（后续段的相对路径按它解析）；其余段须是白名单只读命令
    且每个非旗标参数解析后都落在根内。"""
    tokens = _tokens(seg)
    if not tokens or _hasRedirect(seg):
        return False, cur
    if '/' in tokens[0] or '\\' in tokens[0]:
        return False, cur      # 带路径前缀的命令名：执行的是那个程序
    name = tokens[0].lower()
    if name == 'cd':
        if len(tokens) != 2 or tokens[1].startswith('-'):
            return False, cur
        target = _resolve(tokens[1], cur)
        if not target or not underAny(target, roots):
            return False, cur
        return True, target
    if name not in READONLY_CMDS:
        return False, cur
    if name == 'find' and any(FIND_WRITE_RE.match(a) for a in tokens[1:]):
        return False, cur
    if name == 'rg' and any(RG_EXEC_RE.match(a) for a in tokens[1:]):
        return False, cur
    for arg in tokens[1:]:
        if arg.startswith('-') or arg in task_policy_pipe.STDERR_EXEMPT:
            continue           # 旗标（含取值）与 2>/dev/null、2>&1 不是路径参数
        p = _resolve(arg, cur)
        if not p or not underAny(p, roots):
            return False, cur
    return True, cur


def segmentsReadonly(cmd, base, roots):
    """cmd（只剥 heredoc、未剥引号的原文）的每一段都是边界内的只读命令
    或 cd 段 → True（调用方据此 allow，rule=readonly-roots）。

    任一段不可判定（引号不配平 / 家目录 / 相对路径无 base / realpath
    失败）、越界（`..`、符号链接、绝对路径出根）、非白名单命令、带非
    豁免重定向 → False（调用方继续走 default=ask）。base = 任务工作区
    （会话 cwd）；roots = policy.readonlyRoots（工作区 + 建卡时的项目
    目录）。
    """
    resolved = []
    for root in roots:
        text = str(root or '').strip()
        if not text:
            continue
        r = _resolve(text, base)
        if not r:
            return False
        resolved.append(r)
    if not resolved:
        return False
    cur = _real(base) if str(base or '').strip() else ''
    for seg, _pipe in task_policy_pipe.splitSegments(cmd):
        ok, cur = _segmentOk(seg, cur, resolved)
        if not ok:
            return False
    return True
