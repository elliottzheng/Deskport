# -*- coding: utf-8 -*-
"""流水线动作面（批次 G2）：给任务发消息、接回原会话、上下文换手、
备选模型换手、主管决策与收尾。全部经 tasks_api 公开面投递——不私闯
runner 内部。

换手判定（规格 §2 上下文过长）：上下文用量 ≥50%（出过卡死/提前收工则
≥30%）时不再在原会话追发，而是关掉它、新开会话接棒；接回优先用
kimiSessionId（会话在 kimi 侧持久），失败才新开会话附上下文摘要。
"""
import logging

from modules import tasks_api, workitem_git, workitem_integrate
from modules import workitem_prompts, workitem_state, workitem_store
from modules import workitem_view
from modules import tasks_store as store
from modules import workitem_core as core

log = logging.getLogger('workitem-actions')


def sendToImpl(wi, text, why):
    """给实施任务发消息：上下文超标先换手，结束/投递失败先接回。"""
    return _sendToTask(core._implTask(wi), wi, text, why, kind='impl')


def _sendToTask(task, wi, text, why, kind='impl'):
    if task is not None and not core._ended(task):
        # 上下文换手只对实施会话做（换手=新实施任务接棒）；验收会话照常投递
        if kind == 'impl':
            info = core.worklines_store.contextInfo(task) or {}
            ratio = float(info.get('ratio') or 0)
            threshold = core.CONTEXT_HANDOFF_AFTER_INCIDENT \
                if (wi.get('stalls') or wi.get('earlyFinish')) \
                else core.CONTEXT_HANDOFF
            if ratio >= threshold:
                return handoff(wi, '%s；上下文用量 %d%%，换手'
                               % (why, round(ratio * 100)), instruction=text,
                               task=task)
        try:
            payload, code = tasks_api.deliverMessage(task, text, 'queue',
                                                     by=core.ENGINE_BY)
        except Exception as e:
            payload, code = {'error': str(e)}, 500
        if code < 400:
            return True
        why = '%s（投递失败 %s：%s）' % (why, code,
                                        (payload or {}).get('error') or '')
    return _revive(wi, task, text, why, kind=kind)


def _revive(wi, task, instruction, why, kind='impl'):
    """任务已结束/发不进去：先关掉释放会话，再用 kimiSessionId 接回原
    会话；接回失败才新开会话附上下文摘要（规格 §2：任务已关闭也用
    kimiSessionId 接回——kimi 会话在磁盘上持久，关闭只释放进程锁）。

    kind='verify' 时不接回 kimi 会话——重建/复用验收任务（_ensureVerify
    内部：活着就发重验消息，终态就按平台模板新建），expects 不会错。
    """
    kimiId = ((task or {}).get('sessionId') or '').strip() \
        or ((task or {}).get('kimiSessionId') or '').strip()
    if task is not None and kimiId and not core._ended(task):
        try:
            tasks_api.closeTaskCore(task)
        except Exception as e:
            log.info('revive close %s failed: %s', task.get('id'), e)
    if kind == 'verify':
        from modules import workitem_setup      # 延迟导入：setup→actions
        meta = (wi.get('verifyMeta') or {}).get((task or {}).get('id') or '') \
            or {}
        target = meta.get('target') or wi.get('lastImplCommit') or ''
        if target:
            workitem_setup._ensureVerify(wi, core._project(wi), target,
                                         meta.get('base') or '')
            return True
        core._toJudgment(wi, '验收任务 %s 需要重建但找不到被验收 commit'
                         % ((task or {}).get('id') or '')[:8])
        return False
    if kimiId:
        try:
            if core._startImpl(wi, core._project(wi), prompt=instruction,
                               kimiSessionId=kimiId, fork=False):
                return True
        except Exception as e:
            log.info('readopt %s failed: %s', kimiId[:20], e)
    return handoff(wi, why, instruction=instruction)


def handoff(wi, why, instruction='', task=None, model=''):
    """上下文换手/接回失败：关旧会话、新开会话接棒（平台生成提示）。"""
    project = core._project(wi)
    old = task if task is not None else core._implTask(wi)
    if old is not None and not core._ended(old):
        try:
            tasks_api.closeTaskCore(old)
        except Exception as e:
            log.info('handoff close %s failed: %s', old.get('id'), e)
    head = workitem_git.head(wi.get('worktree') or '')
    last = (wi.get('lastImplResult') or {}).get('summary') or ''
    mustFix = (wi.get('lastVerdict') or {}).get('mustFix') or []
    tmpDir = (old or {}).get('workspace') or wi.get('tmpDir') or ''
    prompt = workitem_prompts.handoffPrompt(
        wi, project, core._specInTree(wi.get('worktree') or ''), head, last,
        mustFix, tmpDir, why)
    if instruction:
        prompt += '\n\n【未完成的指令】\n%s' % instruction
    wi['handoffs'] = int(wi.get('handoffs') or 0) + 1
    return core._startImpl(wi, project, model=model or '',
                           prompt=prompt[:4000]) is not None


def retryStart(wi):
    """任务起不来（会话创建失败）：退避重试——换一个新任务卡重来。"""
    return handoff(wi, '上一次会话没起来（第 %d 次重试）'
                    % (int(wi.get('startFails') or 0) + 1)) is not None


def switchModel(wi):
    """连续提前收工：换项目备选模型新开会话接棒（必须仍满足实施≠验收）。"""
    project = core._project(wi)
    current = wi.get('implModel') or ''
    for candidate in project.get('fallbackModels') or []:
        if not candidate or candidate == wi.get('verifyModel') \
                or candidate == current:
            continue
        wi['implModel'] = candidate
        if handoff(wi, '同一执行连续提前收工，按项目备选模型换手为 %s'
                   % candidate, model=candidate):
            workitem_store.repo.save()
            return True
    core._toJudgment(wi, '连续提前收工，且项目没有可用备选模型'
                        '（fallbackModels 需配 ≠ 验收模型的模型）')
    return False


# ---------------------------------------------------------------- 主管决策

def decide(wid, action, text=''):
    """POST /api/workitems/<id>/decision：continue/rework/integrate/cancel。"""
    with core._lock:
        wi = workitem_store.repo.get(wid)
        if wi is None:
            return {'error': '工作项不存在'}, 404
        if (wi.get('state') or '') != workitem_state.NEEDS_JUDGMENT:
            return {'error': '当前状态 %s 不需要主管判断'
                             % wi.get('state')}, 409
        action = str(action or '').strip().lower()
        if action not in ('continue', 'rework', 'integrate', 'cancel'):
            return {'error': 'action 只能是 continue / rework / integrate / '
                             'cancel'}, 400
        if action == 'cancel':
            return _cancelLocked(wi, '主管取消')
        note = str(text or '').strip()[:800]
        if action == 'integrate':
            err = workitem_state.transition(wi, workitem_state.INTEGRATING,
                                            '主管决定直接集成')
            core._save(wi)
            if err:
                return {'error': err}, 409
            workitem_integrate.start(wi['id'], core._project(wi),
                                     wi.get('branch') or '',
                                     _integrationDone)
            return {'ok': True, 'workItem': _decisionBrief(wi)}, 200
        if action == 'continue':
            frm = wi.get('judgmentFrom') or workitem_state.IMPLEMENTING
            err = workitem_state.transition(
                wi, frm, '主管决定继续：%s' % note[:120])
            core._save(wi)
            if err:
                return {'error': err}, 409
            target = core._verifyTask(wi) if frm == workitem_state.VERIFYING \
                else core._implTask(wi)
            if note and target is not None:
                _sendToTask(target, wi, '【主管说明】%s' % note, '主管放行',
                            kind='verify' if frm == workitem_state.VERIFYING
                            else 'impl')
            return {'ok': True, 'workItem': _decisionBrief(wi)}, 200
        if not core._beginAttempt(wi):
            return {'error': '返工次数已达上限（%d/%d）'
                             % (wi.get('attempt'), wi.get('maxAttempts'))}, 409
        err = workitem_state.transition(wi, workitem_state.IMPLEMENTING,
                                        '主管退回返工：%s' % note[:120],
                                        attempt=wi.get('attempt'))
        core._save(wi)
        if err:
            return {'error': err}, 409
        sendToImpl(wi, workitem_prompts.reworkMessage(
            [note] if note else [], extra='（主管直接指定，按此修改）'),
            '主管返工')
        return {'ok': True, 'workItem': _decisionBrief(wi)}, 200


def _integrationDone(wid, outcome, detail):
    from modules import workitem_pipeline     # 延迟导入：单一写者在 pipeline
    workitem_pipeline.integrationDone(wid, outcome, detail)


def _decisionBrief(wi):
    return workitem_view.brief(wi)


def cancel(wid, by=''):
    with core._lock:
        wi = workitem_store.repo.get(wid)
        if wi is None:
            return {'error': '工作项不存在'}, 404
        return _cancelLocked(wi, by or '取消')


def _cancelLocked(wi, reason):
    if (wi.get('state') or '') in (workitem_state.INTEGRATED,
                                   workitem_state.CANCELLED):
        return {'ok': True, 'workItem': _decisionBrief(wi)}, 200
    # 先收尾再进终态：cancelled 就意味着任务已关、worktree 已拆
    closeOut(wi)
    workitem_state.transition(wi, workitem_state.CANCELLED, reason)
    core._save(wi)
    return {'ok': True, 'workItem': _decisionBrief(wi)}, 200


# ---------------------------------------------------------------- 收尾

def closeOut(wi):
    """integrated/cancelled：关掉名下所有任务（会话可随时接回），拆
    worktree。needs_judgment 不关——主管还要叫醒里面的执行方。"""
    project = core._project(wi)
    for tid in [wi.get('implTask') or ''] + list(wi.get('verifyTasks') or []):
        task = store.repo.get(tid) if tid else None
        if task is not None and not core._ended(task):
            try:
                tasks_api.closeTaskCore(task)
            except Exception as e:
                log.info('closeOut close %s failed: %s', tid, e)
    for worktree in (wi.get('verifyWorktree'), wi.get('worktree')):
        if worktree:
            try:
                workitem_git.worktreeRemove(project.get('repoPath') or '',
                                            worktree)
            except Exception as e:
                log.info('closeOut remove %s failed: %s', worktree, e)
