# -*- coding: utf-8 -*-
"""工作项展示口径（批次 G2）：brief（列表/决策响应）与 detail（详情）。

指挥台一行要用的全在这：标题、阶段进度（实施/验收/集成）、attempt、最新
verdict、排队在等谁共享什么、集成结果。完整任务摘要走 tasks_store
summaryBrief——这里只做聚合，不复制任务字段。
"""
from modules import workitem_state, workitem_store
from modules import tasks_store as store
from modules import workitem_core as core


def brief(wi):
    verdict = wi.get('lastVerdict') or {}
    return {
        'id': wi.get('id') or '', 'title': wi.get('title') or '',
        'projectId': wi.get('projectId') or '', 'lead': wi.get('lead') or '',
        'state': wi.get('state') or '',
        'stage': workitem_state.stageOf(wi.get('state') or ''),
        'attempt': int(wi.get('attempt') or 0),
        'maxAttempts': int(wi.get('maxAttempts') or 0),
        'branch': wi.get('branch') or '', 'worktree': wi.get('worktree') or '',
        'implModel': wi.get('implModel') or '',
        'verifyModel': wi.get('verifyModel') or '',
        'implTask': wi.get('implTask') or '',
        'verifyTasks': list(wi.get('verifyTasks') or []),
        'footprint': list(wi.get('footprint') or []),
        'footprintActual': list(wi.get('footprintActual') or []),
        'queuedFor': wi.get('queuedFor') or '',
        'queuedShare': list(wi.get('queuedShare') or []),
        'forceParallel': bool(wi.get('forceParallel')),
        'lastImplCommit': wi.get('lastImplCommit') or '',
        'lastImplSummary': (wi.get('lastImplResult') or {}).get('summary') or '',
        'lastVerdict': verdict.get('verdict') or '',
        'lastVerdictSummary': verdict.get('summary') or '',
        'mustFix': list(verdict.get('mustFix') or []),
        'integration': dict(wi.get('integration') or {}),
        'judgmentFrom': wi.get('judgmentFrom') or '',
        'judgmentReason': wi.get('judgmentReason') or '',
        'handoffs': int(wi.get('handoffs') or 0),
        'createdAt': wi.get('createdAt') or 0,
        'updatedAt': wi.get('updatedAt') or 0,
        'history': list(wi.get('history') or [])[-20:],
    }


def detail(wid):
    """工作项详情（API 用）：brief + 名下任务摘要 + 集成日志尾部。"""
    wi = workitem_store.repo.get(wid)
    if wi is None:
        return None
    out = brief(wi)
    out['specPath'] = wi.get('specPath') or ''
    out['setupNotes'] = (core._project(wi).get('setupNotes') or '')
    tasks = []
    for tid in [wi.get('implTask') or ''] + list(wi.get('verifyTasks') or []):
        task = store.repo.get(tid) if tid else None
        if task is not None:
            tasks.append(store.repo.summaryBrief(task))
    out['tasks'] = tasks
    out['integrateLogTail'] = workitem_store.readLog(wid)[-8000:]
    return out


def listBriefs(lead=''):
    """列表（API 用）：主管只看自己的，网页全看。"""
    items = [brief(wi) for wi in workitem_store.repo.list()]
    if lead:
        items = [w for w in items if w.get('lead') == lead]
    items.sort(key=lambda w: -w.get('createdAt', 0))
    return items


def briefOf(wid):
    """单个 brief（鉴权与决策前用）；不存在返回 None。"""
    wi = workitem_store.repo.get(wid)
    return brief(wi) if wi is not None else None
