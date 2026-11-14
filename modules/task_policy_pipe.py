# -*- coding: utf-8 -*-
"""命令拆段与管道下游只读消费段判定（task_policy 的拆段助手）。

从 task_policy 拆出（2026-11-15 批次 A 返工）：task_policy 顶着 400 行
预算，而管道消费段这套判定（含"看未剥引号原文"的理由）自成一块语义。

背景：托管主管的自然命令形状是
`cd "<工作区>" && python deskport/deskport.py --help 2>&1 | head -40`
（例 11 复跑时主管的首条命令）。task_policy 按 `&& || | ;` 拆段后要求
**每一段**都命中 allowCommands，`head -40` 段不命中 → 整条 ask → 主管自己
的任务不写收件箱、不唤醒任何人 → 无人干预链静默停摆。修法：管道下游
（`|` 右侧）的只读消费命令豁免逐段命中，**上游段仍须整段命中**——
`cat deskport/.agent-token | head`、`rm -rf x | head`、`… | sh`、
`curl … | head`、`python other.py | head` 照旧 ask。

裸 `&` 分隔符（三验返工，主控拍板）：bash 的单个 `&` 是后台执行分隔符，
两侧命令都会跑（touch 双标记实证），必须和 `;` 一样拆段——否则
`cat "<ws>/x" & rm -rf y` 整条被当作 cat 的参数，越界/执行被自动放行
（只读门与 allowCommands 两条路径同此盲区）。`&&`、`2>&1`、`>&2` 里的
`&` 不拆（前后置断言挡住）。
"""
import re

# && || | ; 与换行照旧；单个 & 也拆，但 && （自带）与 2>&1 / >&2
# （& 前一个是 >）不拆。& 前后的空白随分隔符一起吃掉了
SEGMENT_SPLIT = re.compile(
    r'\s*(?:&&|\|\||\||;)\s*|\s*(?<![&>])&(?![&>])\s*|\n')
# 只读消费命令 + 普通参数字符集。字符集刻意不含引号/$/反引号/重定向/元
# 字符：消费段的参数判定必须看**未剥引号**的原文——task_policy 的引号剥离
# 会把 `head "config.json"` 的参数整段吃掉，只看剥过的文本会漏掉引号里的
# 敏感路径，所以带引号参数的消费段一律不豁免（退回 allowCommands 逐段判
# 定，敏感路径另有 task_policy 的内建 deny 兜底）。另排两类写向量：sort
# 的 -o/--output（输出写文件）、uniq 的第二个位置参数（输出文件）；
# head/tail/grep/wc 无写向量。消费段允许带 2>/dev/null / 2>&1（复验建议
# 改的那条：主管写 `… | head -40 2>/dev/null` 不该挂），其它重定向仍不豁免
PIPE_READONLY_RE = re.compile(r'^(head|tail|grep|wc|sort|uniq)\b')
PIPE_PLAIN_ARG_RE = re.compile(r'^[\w./:=,+*?%@\-]+$')   # \w 含 CJK（grep 中文Pattern）
STDERR_EXEMPT = ('2>/dev/null', '2>&1')


def splitSegments(text):
    """按 && || | ; 裸 & 与换行拆段：返回 [(段文本, 是否管道下游段)]。

    裸 `&`（bash 后台执行分隔符，两侧都会执行——三验返工）与 `;` 同等
    拆段；`&&`、`2>&1`、`>&2` 里的 `&` 不拆（见 SEGMENT_SPLIT 的前后置
    断言）。管道下游段（`|` 右侧）可能只是只读消费命令（`| head -40`
    一类），见 isPipeConsumer——这类段在 allowCommands 整体判定里豁免，
    上游段仍须逐段命中（`rm -rf x | head` 的 rm 段照旧 ask）。
    """
    out = []
    last, pipe = 0, False
    for m in SEGMENT_SPLIT.finditer(text):
        seg = text[last:m.start()]
        if seg.strip():
            out.append((seg, pipe))
        pipe = m.group(0).strip() == '|'
        last = m.end()
    tail = text[last:]
    if tail.strip():
        out.append((tail, pipe))
    return out


def isPipeConsumer(rawSeg):
    """管道下游只读消费段判定（原料 = 未剥引号的段文本）。

    条件：命令 ∈ head/tail/grep/wc/sort/uniq，且每个参数都是普通 token
    （见 PIPE_PLAIN_ARG_RE 字符集——不含引号，也就不含引号里的任何东西）。
    段尾允许带 2>/dev/null / 2>&1（复验建议：`… | head -40 2>/dev/null`
    这种自然形状不该挂；其它重定向仍不豁免）。
    """
    seg = (rawSeg or '').strip()
    for exempt in STDERR_EXEMPT:
        seg = seg.replace(exempt, '')
    m = PIPE_READONLY_RE.match(seg)
    if not m:
        return False
    cmd = m.group(1)
    args = seg[m.end():].split()
    if cmd == 'sort' and any(a.startswith('-o') or a.startswith('--output')
                             for a in args):
        return False          # sort -o <文件>：输出写文件
    positional = [a for a in args if not a.startswith('-')]
    if cmd == 'uniq' and len(positional) > 1:
        return False          # uniq <入> <出>：第二个参数是输出文件
    return all(PIPE_PLAIN_ARG_RE.match(a) for a in args)


def segmentsAllowed(stripped, cmdNoHeredoc, allowRes):
    """拆段后逐段判定：每段命中 allowCommands，或管道下游只读消费段豁免。

    stripped = 剥过引号的命令文本（task_policy 的各道危险判定都用它）；
    cmdNoHeredoc = 只剥 heredoc、未剥引号的原文——消费段的参数判定看它。
    两种拆分段数不一致（引号里有分隔符）时不给任何消费段豁免（安全侧）。
    """
    segPairs = splitSegments(stripped)
    rawSegments = [s for s, _ in splitSegments(cmdNoHeredoc)]
    sameCount = len(rawSegments) == len(segPairs)
    for i, (seg, pipe) in enumerate(segPairs):
        if pipe and sameCount and isPipeConsumer(rawSegments[i]):
            continue
        if not any(r.search(seg) for r in allowRes):
            return False
    return True
