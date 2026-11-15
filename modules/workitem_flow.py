# -*- coding: utf-8 -*-
"""结果流转（批次 G2）：实施结果 → 同步 → 验收；验收结论 → 集成/返工/判断。

这里是规格 §2 的两张表：processImplResult（有 questions → 判断；否则
同步 main，冲突发回、无冲突建/复用验收任务）与 processVerdict（pass →
集成；rework → mustFix 发回同一实施任务、attempt+1；needs_judgment 或
到顶 → 判断）。状态只经 workitem_state.transition 改，每次迁移带原因与
关联任务/提交进 history。
"""
import logging

from modules import workitem_git, workitem_integrate, workitem_prompts
from modules import workitem_state, workitem_store
from modules import workitem_core as core
from modules import workitem_actions, workitem_setup

log = logging.getLogger('workitem-flow')


def processImplResult(wi, task):
    project = core._project(wi)
    res = task.get('result') or {}
    core._markSeen(wi, task)
    wi['lastImplResult'] = dict(res)
    wi['earlyStreak'] = 0            # 真交付了一次：提前收工连续计数清零
    questions = res.get('questions') or []
    if questions:
        core._toJudgment(wi, '实施方有 %d 个问题要主管拍板：%s'
                         % (len(questions), '；'.join(questions)[:160]),
                         frm=workitem_state.IMPLEMENTING)
        return
    worktree = wi.get('worktree') or ''
    target = (res.get('commit') or '').strip() or workitem_git.head(worktree)
    if not target:
        core._toJudgment(wi, '实施结果没有 commit，且工作树读不到 HEAD')
        return
    # 足迹校正（§2b）：用实际 diff 更新声明
    base = workitem_git.mergeBase(project.get('repoPath') or '',
                                  project.get('mainBranch') or 'main', target)
    if base:
        actual = workitem_git.diffFiles(worktree, base)
        if actual:
            wi['footprintActual'] = actual
    # 验收前先同步 main（§2b）：有冲突发回实施任务解决（算一次 attempt）
    ok, out = workitem_git.mergeMain(worktree,
                                     project.get('mainBranch') or 'main')
    if not ok:
        if not core._beginAttempt(wi):
            core._toJudgment(wi, '同步 main 冲突且返工次数已达上限（%d/%d）'
                             % (wi.get('attempt'), wi.get('maxAttempts')))
            return
        err = workitem_state.transition(wi, workitem_state.IMPLEMENTING,
                                        '同步 main 冲突，发回实施方',
                                        task=task['id'])
        core._save(wi)
        if not err:
            workitem_actions.sendToImpl(
                wi, workitem_prompts.syncConflictMessage(
                    wi.get('footprintActual') or wi.get('footprint') or [],
                    out), '同步冲突')
        return
    # §2b：验收对象是"已含最新 main 的提交"——同步成功后的 worktree 新 HEAD
    # （合并提交），不是交付时的旧 commit；基线同步重算（= main 头）
    synced = workitem_git.head(worktree)
    if synced:
        target = synced
        freshBase = workitem_git.mergeBase(
            project.get('repoPath') or '',
            project.get('mainBranch') or 'main', target)
        if freshBase:
            base = freshBase
    workitem_setup._ensureVerify(wi, project, target, base)


def processVerdict(wi, task):
    project = core._project(wi)
    res = task.get('result') or {}
    core._markSeen(wi, task)
    wi['lastVerdict'] = dict(res)
    wi['earlyStreak'] = 0            # 验收真交了结论：连续计数清零
    verdict = res.get('verdict') or ''
    if verdict == 'pass':
        wi['integration']['log'] = workitem_store.logPath(wi['id'])
        err = workitem_state.transition(wi, workitem_state.INTEGRATING,
                                        '验收通过，开始集成',
                                        task=task['id'],
                                        commit=res.get('target') or '')
        core._save(wi)
        if err:
            core._toJudgment(wi, '进入集成态失败：%s' % err)
            return
        workitem_integrate.start(wi['id'], project, wi.get('branch') or '',
                                 _verdictDone)
        return
    if verdict == 'needs_judgment':
        core._toJudgment(wi, '验收方结论：需要人判断——%s'
                         % (res.get('summary') or '（无摘要）')[:160],
                         frm=workitem_state.VERIFYING)
        return
    # rework：mustFix 发回同一个实施任务（§2）
    mustFix = res.get('mustFix') or []
    if not core._beginAttempt(wi):
        core._toJudgment(wi, '返工次数已达上限（%d/%d），未修完：%s'
                         % (wi.get('attempt'), wi.get('maxAttempts'),
                            '；'.join(mustFix)[:160]))
        return
    err = workitem_state.transition(wi, workitem_state.IMPLEMENTING,
                                    '验收退回，返工回同一实施会话',
                                    task=wi.get('implTask') or '',
                                    attempt=wi.get('attempt'))
    core._save(wi)
    if err:
        core._toJudgment(wi, '返回实施态失败：%s' % err)
        return
    workitem_actions.sendToImpl(
        wi, workitem_prompts.reworkMessage(mustFix), '返工')


def _verdictDone(wid, outcome, detail):
    from modules import workitem_pipeline     # 延迟导入：单一写者在 pipeline
    workitem_pipeline.integrationDone(wid, outcome, detail)
