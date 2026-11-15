# -*- coding: utf-8 -*-
"""流水线巡检与集成回调（批次 G2）：tick 分派、集成作业回收入口、
集成后通知。异常检测（起不来/卡死/提前收工/换手）在 workitem_watch，
它调本模块的 tick 与 actions 的公开面。

集成回调 integrationDone 是作业线程与巡检共享的单一写者：整段在
core._lock 里读改写，杜绝"作业线程刚提交合并、巡检同时判返工"的竞态。
"""
import logging

from modules import tasks_api, workitem_git, workitem_integrate
from modules import workitem_prompts, workitem_state, workitem_store
from modules import workitem_core as core
from modules import workitem_actions, workitem_flow, workitem_setup

log = logging.getLogger('workitem-pipeline')


def tick():
    """扫一遍所有未终态工作项（watch 每 2s 调；测试可直接调）。"""
    for wi in workitem_store.repo.list():
        try:
            with core._lock:
                _tickOne(wi)
        except Exception as e:      # 一个工作项炸了不能带走整条流水线
            log.warning('workitem tick failed %s: %s', wi.get('id'), e)


def _tickOne(wi):
    state = wi.get('state') or ''
    if state in (workitem_state.INTEGRATED, workitem_state.FAILED,
                 workitem_state.CANCELLED):
        return
    if state == workitem_state.QUEUED:
        workitem_setup._schedule(wi)
    elif state == workitem_state.IMPLEMENTING:
        _implTick(wi)
    elif state == workitem_state.VERIFYING:
        _verifyTick(wi)
    # integrating：集成作业线程驱动；needs_judgment：等主管


def _implTick(wi):
    task = core._implTask(wi)
    if task is None:
        core._toJudgment(wi, '实施任务 %s 不见了（可能被手工删除），请主管判断'
                         % (wi.get('implTask') or '')[:8])
        return
    if task.get('result') and core._resultCount(task) > core._seen(wi, task):
        workitem_flow.processImplResult(wi, task)
        return
    if core._ended(task):
        return                      # 起不来/提前收工/接回归 watch 处理
    if task.get('status') == 'waiting' and task.get('resultNudged') \
            and not task.get('result'):
        core._toJudgment(wi, '实施方被追发一次后仍未提交结构化结果（任务 %s）'
                         % task['id'][:8])


def _verifyTick(wi):
    task = core._verifyTask(wi)
    if task is None:
        core._toJudgment(wi, '验收任务不见了（可能被手工删除），请主管判断')
        return
    if task.get('result') and core._resultCount(task) > core._seen(wi, task):
        workitem_flow.processVerdict(wi, task)
        return
    if core._ended(task):
        return                      # 接回归 watch 处理
    if task.get('status') == 'waiting' and task.get('resultNudged') \
            and not task.get('result'):
        core._toJudgment(wi, '验收方被追发一次后仍未提交结论（任务 %s）'
                         % task['id'][:8])


# ---------------------------------------------------------------- 集成回调

def integrationDone(wid, outcome, detail):
    """集成作业线程回call（单一写者：整段在 _lock 里读改写）。"""
    with core._lock:
        wi = workitem_store.repo.get(wid)
        if wi is None or (wi.get('state') or '') != workitem_state.INTEGRATING:
            return                  # 已被取消/主管改判
        project = core._project(wi)
        wi['integration']['log'] = workitem_store.logPath(wid)
        if outcome == 'ok':
            wi['integration']['commit'] = detail or ''
            wi['integration']['gates'] = 'pass'
            wi['integration']['tests'] = 'pass'
            # 先收尾再进终态：integrated 就意味着任务已关、worktree 已拆
            _closeOutSafe(wi)
            err = workitem_state.transition(wi, workitem_state.INTEGRATED,
                                            '集成完成：合并提交',
                                            commit=detail)
            core._save(wi)
            if not err:
                _notifyIntegrated(wi, detail or '')
            return
        if outcome == 'sync':
            # §2b：集成时 main 又动了 → 回"同步"，不是直接返工
            try:
                ok, out = workitem_git.mergeMain(
                    wi.get('worktree') or '',
                    project.get('mainBranch') or 'main')
            except workitem_git.GitError as e:
                ok, out = False, str(e)
            if ok:
                workitem_integrate.start(wid, project, wi.get('branch') or '',
                                         integrationDone)
                return
            wi['syncRetries'] = int(wi.get('syncRetries') or 0) + 1
            if wi['syncRetries'] > core.SYNC_RETRY_CAP:
                core._toJudgment(wi, '同步 main 连续 %d 次冲突，请主管判断'
                                 % wi['syncRetries'])
                return
            if not core._beginAttempt(wi):
                core._toJudgment(wi, '同步冲突且返工次数已达上限')
                return
            err = workitem_state.transition(wi, workitem_state.IMPLEMENTING,
                                            '集成冲突→同步仍冲突，返工',
                                            task=wi.get('implTask') or '')
            core._save(wi)
            if not err:
                workitem_actions.sendToImpl(
                    wi, workitem_prompts.syncConflictMessage(
                        wi.get('footprintActual') or wi.get('footprint') or [],
                        out), '同步冲突')
            return
        step = (detail or {}).get('step') or 'gates'
        out = (detail or {}).get('out') or ''
        if not core._beginAttempt(wi):
            core._toJudgment(wi, '集成失败（%s）且返工次数已达上限' % step)
            return
        err = workitem_state.transition(wi, workitem_state.IMPLEMENTING,
                                        '集成失败（%s 不绿），返工' % step,
                                        task=wi.get('implTask') or '')
        core._save(wi)
        if not err:
            workitem_actions.sendToImpl(
                wi, workitem_prompts.integrateFailMessage(step, out),
                '集成失败')


def _closeOutSafe(wi):
    """收尾兜异常：关任务/拆 worktree 失败不该拦住终态与通知。"""
    try:
        workitem_actions.closeOut(wi)
    except Exception as e:
        log.warning('closeOut %s failed: %s', wi.get('id'), e)


def _notifyIntegrated(wi, commit):
    """§2b 集成后通知：足迹有交集、正在实施的工作项收到"提交前先 merge main"。"""
    mine = wi.get('footprintActual') or wi.get('footprint') or []
    for other in workitem_store.repo.list():
        if other.get('id') == wi.get('id') \
                or other.get('state') != workitem_state.IMPLEMENTING \
                or other.get('projectId') != wi.get('projectId'):
            continue
        if not any(workitem_state.footprintsIntersect(p, q)
                   for p in mine for q in (other.get('footprint') or [])):
            continue
        task = core._implTask(other)
        if task is None or core._ended(task):
            continue
        try:
            tasks_api.deliverMessage(
                task, 'main 已含《%s》（%s），你与本工作项共享足迹：'
                      '提交前请先 git merge %s'
                      % (wi.get('title') or wi.get('id'), (commit or '')[:12],
                         core._project(other).get('mainBranch') or 'main'),
                'queue', by=core.ENGINE_BY)
        except Exception as e:
            log.info('notify integrated -> %s failed: %s', other.get('id'), e)
