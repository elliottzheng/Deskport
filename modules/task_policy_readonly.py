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

三验返工（主控拍板：黑名单改白名单，不再补洞）三处收紧：
- 旗标白名单 FLAGS：每个只读命令只放行表内旗标（含取值形态），表外
  一律 ask——等号形态（rg --pre=cat、wc --files0-from=）、读外部文件
  （grep -f/--file）、执行程序（rg --pre/-z）都从这拦；
- 路径解析后复查敏感文件：realpath 展开 8.3 短名（AGENT-~1）与符号
  链接后的结果再匹配一次 SENSITIVE_FILE_RE，命令文本正则认不出的
  令牌文件形态由此补上；
- 不可判定字符 FORBIDDEN：参数含 %（Windows 环境变量）、$、~、反引号、
  !（历史展开）→ ask。
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
# 不可判定字符（三验返工）：%（Windows 环境变量）、$（变量/命令替换）、
# 反引号（命令替换）、!（历史展开）——出现在任何参数里都视为不可判定 →
# ask（fail-closed）。~ 只在家目录展开位置（词首）算，词中的 ~（8.3 短名
# AGENT-~1）是字面量，交给 realpath 解析后的敏感文件复查
FORBIDDEN = '%$`!'
# 只读命令旗标白名单（三验返工，主控拍板：只认下表已知安全旗标，表外
# 一律 ask——黑名单已被等号形态/旗标取值/短文件名绕过三次）：
#   bool   不带取值的旗标
#   valued 取值（下一个 token，或紧贴 -n5 / 等号 --include=GLOB 形态），
#          值本身不是路径（glob/模式/数字）
#   path   取值是路径，须再过根内判定（find -newer）
#   exact  只认这一个完整 token（--color=never；auto/always 会改输出色）
#   numeric 旧式数字旗标放行（head/tail 的 -40 = -n 40）
# stat/file/type 无条目 = 不带任何旗标才放行。
FLAGS = {
    'cat': {'bool': '-n -b -A -E -T -v -s'},
    'head': {'bool': '-q -v', 'valued': '-n -c', 'numeric': True},
    'tail': {'bool': '-q -v', 'valued': '-n -c', 'numeric': True},
    'ls': {'bool': '-l -a -A -h -R -t -r -S -1 -d -F'},
    'dir': {'bool': '-l -a -A -h -R -t -r -S -1 -d -F'},
    'grep': {'bool': '-r -R -n -i -l -L -c -w -x -v -E -F -H -h -o -q -s',
             'valued': '-e -m -A -B -C --include --exclude',
             'exact': '--color=never'},
    'rg': {'bool': '-r -R -n -i -l -L -c -w -x -v -E -F -H -h -o -q -s '
                   '--hidden --no-ignore',
           'valued': '-e -m -A -B -C --include --exclude -g -t',
           'exact': '--color=never'},
    'wc': {'bool': '-l -w -c -m -L'},
    'find': {'bool': '-print',
             'valued': '-name -iname -type -maxdepth -mindepth -path '
                       '-ipath -size -mtime',
             'path': '-newer'},
    'stat': {}, 'file': {}, 'type': {},
}
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


def _sensitive(resolved):
    """解析后的路径（realpath 已展开 8.3 短名与符号链接）复查敏感文件
    规则——命令文本正则认不出 AGENT-~1，链接目标也只在解析后才现形，
    这里补上（三验返工）。"""
    return any(r.search(str(resolved)) for r in SENSITIVE_FILE_RE)


def _flagValue(spec, arg, tokens, i):
    """旗标 token 白名单判定，返回 (是否放行, 下一个 token 下标, 路径取值)。

    形态依次认：完整匹配（bool/exact/valued/path）；长旗标等号形态
    （--include=GLOB——值是 glob 不是路径；--hidden=x 这类不放行）；
    短旗标紧贴取值（-n5、-A2）与旧式数字（-40）；布尔短旗标簇（-rn、
    -la）。表外旗标（含任何 --xxx=yyy 等号形态）一律不放行。取值缺失
    （-n 后面没有东西）按不可判定 → 不放行。
    """
    bools = set((spec.get('bool') or '').split())
    valued = set((spec.get('valued') or '').split())
    paths = set((spec.get('path') or '').split())
    nxt = i + 1
    if arg in bools or arg in (spec.get('exact') or '').split():
        return True, nxt, None
    if arg in valued:
        return (True, i + 2, None) if i + 1 < len(tokens) else (False, nxt, None)
    if arg in paths:
        if i + 1 < len(tokens):
            return True, i + 2, tokens[i + 1]
        return False, nxt, None
    if arg.startswith('--'):
        head, eq, value = arg.partition('=')
        if eq and head in valued and value:
            return True, nxt, None      # --include=GLOB：值是 glob 不是路径
        return False, nxt, None
    body = arg[1:]
    if spec.get('numeric') and body.isdigit():
        return True, nxt, None          # head/tail 的 -40（= -n 40）
    j = 0
    while j < len(body):
        flag = '-' + body[j]
        if flag in bools:
            j += 1
            continue
        if flag in paths:
            value = body[j + 1:]
            if value:
                return True, nxt, value
            if i + 1 < len(tokens):
                return True, i + 2, tokens[i + 1]
            return False, nxt, None
        if flag in valued:
            if body[j + 1:]:
                return True, nxt, None
            if i + 1 < len(tokens):
                return True, i + 2, None
            return False, nxt, None
        return False, nxt, None
    return True, nxt, None


def _segmentOk(seg, cur, roots):
    """单段判定，返回 (是否放行, 新的当前目录, 是否命中敏感文件)。

    cd 段校验目标在根内并更新当前目录（后续段的相对路径按它解析）；
    其余段须是白名单只读命令，旗标全在 FLAGS 表内（含取值形态），
    -newer 的路径取值与每个位置参数都解析后落在根内，且解析结果不
    命中敏感文件规则。任一步不满足 → 不放行（调用方按 ask）；解析出
    的路径命中敏感文件 → 第三项 True（deny:sensitive-file）。
    """
    tokens = _tokens(seg)
    if not tokens or _hasRedirect(seg):
        return False, cur, False
    for tok in tokens:
        # ~ 只在词首是家目录展开（词中的 ~ 如 AGENT-~1 是字面量，往下走
        # realpath 解析）；% $ 反引号 ! 在词中任何位置都不可判定
        if tok.startswith('~') or any(ch in tok for ch in FORBIDDEN):
            return False, cur, False
    if '/' in tokens[0] or '\\' in tokens[0]:
        return False, cur, False      # 带路径前缀的命令名：执行的是那个程序
    name = tokens[0].lower()
    if name == 'cd':
        if len(tokens) != 2 or tokens[1].startswith('-'):
            return False, cur, False
        target = _resolve(tokens[1], cur)
        if not target or not underAny(target, roots):
            return False, cur, False
        return True, target, _sensitive(target)
    if name not in READONLY_CMDS:
        return False, cur, False
    if name == 'find' and any(FIND_WRITE_RE.match(a) for a in tokens[1:]):
        return False, cur, False
    if name == 'rg' and any(RG_EXEC_RE.match(a) for a in tokens[1:]):
        return False, cur, False
    spec = FLAGS.get(name) or {}
    positional = []
    i = 1
    while i < len(tokens):
        arg = tokens[i]
        if arg in task_policy_pipe.STDERR_EXEMPT:
            i += 1
            continue
        if arg.startswith('-') and arg not in ('-', '--'):
            ok, nxt, value = _flagValue(spec, arg, tokens, i)
            if not ok:
                return False, cur, False
            if value is not None:      # -newer 的取值是路径：过根内判定
                p = _resolve(value, cur)
                if not p or not underAny(p, roots):
                    return False, cur, False
                if _sensitive(p):
                    return False, cur, True
            i = nxt
            continue
        positional.append(arg)
        i += 1
    for arg in positional:
        p = _resolve(arg, cur)
        if not p or not underAny(p, roots):
            return False, cur, False
        if _sensitive(p):
            return False, cur, True
    return True, cur, False


def segmentsReadonly(cmd, base, roots):
    """cmd（只剥 heredoc、未剥引号的原文）的每一段都是边界内的只读命令
    或 cd 段 → (True, False)（调用方据此 allow，rule=readonly-roots）。

    任一段不可判定（引号不配平 / 家目录 / 相对路径无 base / realpath
    失败 / 参数含 % $ ~ ` !）、越界（`..`、符号链接、绝对路径出根）、
    非白名单命令、旗标表外、带非豁免重定向 → (False, False)（调用方
    继续走 default=ask）。解析后的路径（realpath 展开 8.3 短名/符号
    链接后）命中敏感文件规则 → (False, True)（deny:sensitive-file）。
    base = 任务工作区（会话 cwd）；roots = policy.readonlyRoots（工作区
    + 建卡时的项目目录）。
    """
    resolved = []
    for root in roots:
        text = str(root or '').strip()
        if not text:
            continue
        r = _resolve(text, base)
        if not r:
            return False, False
        resolved.append(r)
    if not resolved:
        return False, False
    cur = _real(base) if str(base or '').strip() else ''
    for seg, _pipe in task_policy_pipe.splitSegments(cmd):
        ok, cur, sensitive = _segmentOk(seg, cur, resolved)
        if sensitive:
            return False, True
        if not ok:
            return False, False
    return True, False
