# -*- coding: utf-8 -*-
"""流水线共用内核（批次 G2）：装配、工作项小助手与建任务原语。

拆自单文件流水线（400 行预算）：core 是被 setup/flow/actions/pipeline 共同
依赖的底账——app 注入、任务上下文、指针查询、结果水位、判断入口、以及
"建一个实施任务"这个原语（_startImpl）。单写者纪律：工作项状态只经
transition() 改；任务实体归 tasks_store。
"""
import contextlib
import logging
import os
import threading

from flask import g

from modules import leads_inbox, leads_store, task_result, tasks_store as store
from modules import tasks_api, workitem_git, workitem_prompts
from modules import workitem_projects, workitem_state, workitem_store
from modules import worklines_store

log = logging.getLogger('workitem-core')

ENGINE_BY = '流水线'          # 平台代发消息的发信人（不能落成"面板"）
SYNC_RETRY_CAP = 3
CONTEXT_HANDOFF = 0.5         # 上下文换手阈值（规格 §2）
CONTEXT_HANDOFF_AFTER_INCIDENT = 0.3

_app = None
_lock = threading.RLock()


def bindApp(app):
    """create_app 注入（巡检线程由 workitem_watch 起，见 app.py）。"""
    global _app
    _app = app


@contextlib.contextmanager
def _taskCtx(lead=''):
    """建任务的 Flask 上下文：g.agentLead 让任务卡记上归属（主管有效性也走
    这条校验），发信人记「流水线」。没有 app（单测直调）时直通。"""
    if _app is None:
        yield
        return
    with _app.app_context():
        g.agentLead = lead or ''
        g.agentTokenName = ENGINE_BY
        yield


def _save(wi):
    workitem_store.repo.save()


# ---------------------------------------------------------------- 小助手

def _project(wi):
    return workitem_projects.repo.get(wi.get('projectId') or '') or {}


def _implTask(wi):
    return store.repo.get(wi.get('implTask') or '') if wi.get('implTask') else None


def _verifyTask(wi):
    tasks = [t for t in (wi.get('verifyTasks') or [])
             if store.repo.get(t) is not None]
    return store.repo.get(tasks[-1]) if tasks else None


def _ended(task):
    return task is None or bool(task_result.ended(task))


def _specInTree(worktree):
    return os.path.join(worktree, workitem_prompts.SPEC_IN_TREE).replace('\\', '/')


def _writeSpecCopy(worktree, specPath, specText):
    """规格全文落一份到 worktree（.wi-spec.md）：执行方相对 cwd 就能读，
    不依赖外部路径；并挡掉误提交（per-worktree 的 git exclude）。"""
    text = specText
    if not text and specPath:
        try:
            with open(specPath, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            text = '（规格文件读不到：%s）' % specPath
    path = os.path.join(worktree, workitem_prompts.SPEC_IN_TREE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text or '')
    code, gitDir = workitem_git.run(worktree, ['rev-parse', '--git-dir'],
                                    check=False)
    if code == 0 and gitDir.strip():
        exclude = os.path.join(worktree, gitDir.strip(), 'info', 'exclude')
        try:
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, 'a', encoding='utf-8') as f:
                f.write('/%s\n' % workitem_prompts.SPEC_IN_TREE)
        except OSError:
            pass


def _resultCount(task):
    return int(task.get('resultCount') or 0)


def _seen(wi, task):
    return int((wi.get('resultSeen') or {}).get(task.get('id') or '') or 0)


def _markSeen(wi, task):
    wi.setdefault('resultSeen', {})[task['id']] = _resultCount(task)


def _toJudgment(wi, reason, frm=''):
    """进 needs_judgment：写 history + 主管收件箱（带原因与可选动作）。"""
    frm = frm or wi.get('state') or ''
    err = workitem_state.transition(wi, workitem_state.NEEDS_JUDGMENT, reason)
    if err:
        log.warning('toJudgment rejected %s: %s', wi.get('id'), err)
        return False
    wi['judgmentFrom'] = frm
    wi['judgmentReason'] = str(reason)[:300]
    _save(wi)
    lead = wi.get('lead') or ''
    if lead:
        leads_inbox.append(lead, 'up', 'workitem_judgment',
                           workitem_prompts.judgmentNote(reason),
                           {'workItem': wi.get('id') or '', 'from': frm})
    log.info('workitem %s needs judgment: %s', wi.get('id'), reason)
    return True


def _beginAttempt(wi):
    """返工/同步冲突/集成失败都算一次 attempt；到顶返回 False（转判断）。"""
    if not workitem_state.canRework(wi):
        return False
    wi['attempt'] = int(wi.get('attempt') or 0) + 1
    return True


# ---------------------------------------------------------------- 建任务原语

def _startImpl(wi, project, model='', prompt='', kimiSessionId='', fork=True):
    """建实施任务（expects=impl，cwd=worktree，策略=项目写边界）。

    返回 payload；建卡失败（主管停用、节点不存在等）转 needs_judgment 并
    返回 None——调用方据此止损。
    """
    with _taskCtx(wi.get('lead') or ''):
        payload, code = tasks_api.createTaskResponse({
            'prompt': prompt or workitem_prompts.implPrompt(
                wi, project, workitem_prompts.specRef(
                    wi.get('worktree') or '', wi.get('specPath') or '',
                    wi.get('specText') or '')[0]),
            'node': project.get('node') or 'local',
            'cwd': wi.get('worktree') or '',
            'model': model or wi.get('implModel') or '',
            'mode': 'default',
            'policy': project.get('policy') or {},
            'expects': 'impl',
            'title': '%s · 实施' % (wi.get('title') or wi.get('id')),
            'workItemId': wi.get('id') or '',
            'kimiSessionId': kimiSessionId, 'fork': fork}, start=True)
    if code >= 400:
        _toJudgment(wi, '建实施任务失败（%s）：%s'
                    % (code, (payload or {}).get('error') or ''))
        return None
    wi['implTask'] = (payload.get('task') or {}).get('id') or ''
    _save(wi)
    return payload
