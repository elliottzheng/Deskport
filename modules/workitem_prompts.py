# -*- coding: utf-8 -*-
"""平台提示模板（批次 G2）：实施/验收/换手/返工/追发的固定文案。

口径来源：COLLABORATION.md §2 派活结构、§5 验收独立做、验方任务模板、
规格 §2 各异常分支。模板只组装文本，不含流程——流程在 workitem_pipeline。
所有文本都要过 createTaskResponse 的 4000 字上限，超长规格走文件引用。
"""
import os

SPEC_IN_TREE = '.wi-spec.md'      # 平台放入 worktree 的规格副本（勿提交）
NUDGE_TEXT = '继续；不要用工具自带后台任务，前台跑'

# 平台强制规则（每个实施/验收提示都带；逐条来自 COLLABORATION §2.4）
PLATFORM_RULES = """【平台强制规则】
- 不要使用工具自带的后台任务（run_in_background）——服务端会直接拒绝。
- 服务器类进程一律用 nohup … & 后台启动，不许在前台跑。
- 任何直接 import modules 的脚本，先把 APPDATA 指到自己的临时目录。
- 只按进程号结束自己启动的进程；禁止按名字批量结束进程。
- 写含反斜杠的文件用编辑器工具，不用 heredoc / sed。
- 需要主管拍板时，把问题写在回复里并结束这一轮；不要用提问工具。
- 交付必须调用 submit_result 工具提交结构化结果；自由文本只作补充。"""


def specRef(worktree, specPath, specText, budget=4000):
    """规格在提示里的形态：能内联就内联（≤预算），否则指路 worktree 副本。

    副本由平台在开工前写进 worktree（两种来源都落 .wi-spec.md），执行方
    相对 cwd 就能读，不依赖外部路径与审批边界。
    """
    path = os.path.join(worktree, SPEC_IN_TREE).replace('\\', '/')
    if specText:
        head = '【规格全文】\n%s' % specText.strip()
        if len(head) <= budget:
            return head, path
    if specPath:
        return ('【规格】%s\n（平台已把全文副本放到 %s，开工前先读它）'
                % (specPath, path)), path
    return ('【规格】全文见 %s，开工前先读它' % path), path


def implPrompt(wi, project, specBlock):
    """实施方提示：目标/规格/边界/交付，照 COLLABORATION §2 的固定结构。"""
    lines = [
        '【目标】%s' % (wi.get('title') or '完成本工作项'),
        '',
        specBlock,
        '',
        '【仓库与分支】',
        '- 工作树（cwd，你的改动只在这里）：%s' % (wi.get('worktree') or ''),
        '- 分支：%s（从 %s 切出；提交后不要推送、不要合并）'
        % (wi.get('branch') or '', project.get('mainBranch') or 'main'),
        '- 预计改动足迹：%s' % '、'.join(wi.get('footprint') or []),
        '',
        '【项目边界】%s' % (project.get('setupNotes') or '（未说明）'),
        '',
        PLATFORM_RULES,
        '',
        '【交付】完成后调用 submit_result（expects=impl）：summary / commit / '
        'tests 必填，gates / risks / questions 按实际填；有问题要主管拍板就写进 '
        'questions（平台会转给主管）。',
    ]
    return '\n'.join(lines)[:4000]


def verifyPrompt(wi, project, specPathInTree, target, base, attempt,
                 gatesCmd, testCmd):
    """验方提示：不采信自述、改坏即红、门禁+全量、只读不提交。

    gatesCmd/testCmd 由调用方按项目命令解析好（{repo}/{tmp} 已替换）——
    验方照贴执行，不自己猜命令。
    """
    lines = [
        '【角色】你是验方。实施方已交付分支 %s 的 commit %s（第 %d 次验收）。'
        % (wi.get('branch') or '', (target or '')[:12], attempt),
        '不要读实施方的自述、不要相信它的总结——从 git diff 和代码本身出发，'
        '默认假设"这里有bug"。你不改代码、不提交、不推送；发现问题只报告。',
        '',
        '【规格】%s（平台副本，先读）' % specPathInTree,
        '',
        '【验收对象】commit %s； diff 基线 %s（git diff %s..%s）'
        % ((target or '')[:12], (base or '')[:12], (base or '')[:12],
           (target or '')[:12]),
        '【必做四件事】',
        '1. 机械门禁（原文贴上，不要只写"通过"）：',
        '   %s' % gatesCmd,
        '   %s' % testCmd,
        '2. 逐文件读 diff，回答：为什么必要、有无副作用、是否越界。',
        '3. 对照规格逐条打勾（✅/⚠️/❌，⚠️ 必须如实出现）。',
        '4. 主动找五类问题：语义漂移 / 失败路径 / 重复实现 / 越界改动 / '
        '测试质量（挑 2–3 条新测试把实现改坏验证会红）。',
        '',
        '【交付】调用 submit_result（expects=verify）：verdict=pass/rework/'
        'needs_judgment、target=<被验收 commit>；rework 时把必须修的点写进 '
        'mustFix（每条一句话 + 证据）。结论只能是：可合并 / 需退回 / 需要人判断。',
        '',
        PLATFORM_RULES,
    ]
    return '\n'.join(lines)[:4000]


def reworkMessage(mustFix, extra=''):
    """返工消息（发回同一个实施任务）：mustFix 逐条 + 平台要求。"""
    lines = ['【验收退回】必须修的问题（逐条改完再交付）：']
    for i, item in enumerate(mustFix or [], 1):
        lines.append('%d. %s' % (i, item))
    if extra:
        lines.append(extra)
    lines.append('改完在同一分支提交，再调用 submit_result 交付（commit 要换新的）。')
    return '\n'.join(lines)


def syncConflictMessage(files, output):
    """同步 main 冲突（§2b）：发回实施任务解决，算一次 attempt。"""
    return ('【同步冲突】你的分支与最新 main 有冲突，先解决再交付：\n'
            '冲突文件：%s\n'
            '--- git 输出（截断）---\n%s\n'
            '在本 worktree 里 git merge <main> 解决冲突、提交，'
            '再调用 submit_result 交付。'
            % ('、'.join(files[:20]) or '（见输出）', (output or '')[-1500:]))


def integrateFailMessage(step, output):
    """集成失败（门禁/测试红）：失败输出截尾 4KB 作为返工发回。"""
    return ('【集成失败·%s】主仓合并后 %s 不绿，已 git merge --abort 回到合并前。'
            '按下面的输出修（算一次返工）：\n%s\n'
            '修完在同一分支提交并调用 submit_result 交付。'
            % (step, step, (output or '')[-4000:]))


def integrateConflictMessage(output):
    """集成时 main 又动了（§2b）：回到"同步"而不是直接返工。"""
    return ('【集成冲突】主仓 main 在你交付后又前进了，合并有冲突。'
            '已 abort。请先同步再交付：\n%s\n'
            '在本 worktree 里 git merge <main>、解决冲突、提交，'
            '再调用 submit_result。' % (output or '')[-1500:])


def handoffPrompt(wi, project, specPathInTree, head, lastResult, mustFix,
                  tmpDir, why):
    """上下文换手（§2）：新会话接棒的平台生成提示。"""
    lines = [
        '【接棒说明】%s' % why,
        '这不是新任务：前一位执行者已经做了一部分，你接着做。',
        '',
        '【目标】%s' % (wi.get('title') or ''),
        '【规格】%s（先读）' % specPathInTree,
        '【工作树】%s（分支 %s，HEAD %s）'
        % (wi.get('worktree') or '', wi.get('branch') or '', (head or '')[:12]),
    ]
    if lastResult:
        lines.append('【前一位最近一次交付】%s' % lastResult)
    else:
        lines.append('【前一位交付】尚未提交结构化结果（可看工作树里的改动与'
                     '任务事件流）。')
    if mustFix:
        lines.append('【未完成的返工清单】')
        for i, item in enumerate(mustFix, 1):
            lines.append('%d. %s' % (i, item))
    lines.append('【临时目录】%s（前一位的中间产物在这）' % (tmpDir or '（无）'))
    lines += ['', '【项目边界】%s' % (project.get('setupNotes') or '（未说明）'),
              '', PLATFORM_RULES]
    return '\n'.join(lines)[:4000]


def reverifyMessage(target, base):
    """返工后再验收（复用同一验收会话）：worktree 已切到新 commit。"""
    return ('【重新验收】实施方已按返工清单修改并交付新 commit %s'
            '（验收 worktree 平台已切到它；diff 基线仍为 %s）。'
            '请按原流程重新验收（门禁 + 全量 + 逐文件读 diff），'
            '结论用 submit_result 重交（verdict/target 用新 commit）。'
            % ((target or '')[:12], (base or '')[:12]))


def judgmentNote(reason):
    """进 needs_judgment 时给主管收件箱的条目文本（≤300）。"""
    return ('工作项需主管判断：%s。可用动作：continue（带说明继续）/ '
            'rework（带返工要点）/ integrate（直接集成）/ cancel'
            % (reason or '')[:220])
