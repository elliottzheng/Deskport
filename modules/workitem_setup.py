# -*- coding: utf-8 -*-
"""工作项创建与调度（批次 G2）：worktree/分支、足迹排队、验收任务。

create()：校验 → 平台建 worktree 与分支（不经智能体）→ 规格副本落盘 →
落卡 → 调度。_schedule() 是 §2b 的并行闸门：同项目足迹有交集的工作项
不并行实施，后来者进 queued 并在指挥台显示在等谁、共享什么。
"""
import logging
import os

from modules import tasks_api, workitem_git, workitem_integrate
from modules import workitem_prompts, workitem_projects, workitem_state
from modules import workitem_store, workitem_view
from modules import workitem_core as core

log = logging.getLogger('workitem-setup')


def create(data):
    """POST /api/workitems 的核心。返回 (payload, status)。

    worktree 建失败（分支重名/磁盘/权限）直接 400 并不留半张卡；specPath
    读不到不拦（specText 场景本来就没有文件）。
    """
    clean, errors = workitem_state.validateCreate(data or {})
    if errors:
        return {'error': '工作项参数不符合要求', 'fields': errors}, 400
    project = workitem_projects.repo.get(str((data or {}).get('projectId') or ''))
    if project is None:
        return {'error': '项目不存在: %s' % (data or {}).get('projectId')}, 404
    lead = str((data or {}).get('lead') or '').strip()
    if lead and not _leadExists(lead):
        return {'error': '主管不存在: %s' % lead}, 400
    wid = workitem_store.newId()
    worktree = os.path.join(project.get('worktreeRoot') or '', wid)
    branch = 'wi/%s' % wid
    repo = project.get('repoPath') or ''
    with core._lock:
        try:
            workitem_git.worktreeAdd(repo, worktree, branch,
                                     project.get('mainBranch') or 'main')
            core._writeSpecCopy(worktree, clean['specPath'], clean['specText'])
        except workitem_git.GitError as e:
            workitem_git.worktreeRemove(repo, worktree)
            return {'error': '建 worktree 失败：%s' % e}, 400
        except OSError as e:
            workitem_git.worktreeRemove(repo, worktree)
            return {'error': '写规格副本失败：%s' % e}, 400
        tmpDir = os.path.join(os.path.dirname(workitem_store.storePath()),
                              'workitem_logs', 'tmp', wid)
        try:
            os.makedirs(tmpDir, exist_ok=True)
        except OSError:
            tmpDir = ''
        wi = workitem_store.repo.create({
            'id': wid, 'projectId': project['id'], 'lead': lead,
            'title': clean['title'] or _defaultTitle(clean),
            'specPath': clean['specPath'], 'specText': clean['specText'],
            'branch': branch, 'worktree': worktree,
            'implModel': clean['implModel'], 'verifyModel': clean['verifyModel'],
            'maxAttempts': clean['maxAttempts'],
            'footprint': clean['footprint'],
            'forceParallel': clean['forceParallel'], 'tmpDir': tmpDir})
        _schedule(wi)
    return {'ok': True, 'workItem': workitem_view.brief(wi),
            'project': project['id']}, 201


def _leadExists(lead):
    from modules import leads_store          # 延迟导入：core 已 import，这里只取存在性
    return leads_store.get(lead) is not None


def _defaultTitle(clean):
    if clean['specPath']:
        return os.path.splitext(os.path.basename(clean['specPath']))[0][:40]
    first = (clean['specText'] or '').strip().splitlines()
    return (first[0] if first else '未命名工作项')[:40]


# ---------------------------------------------------------------- 调度（§2b）

def _schedule(wi):
    """足迹有交集的在跑工作项 → queued；无交集（或主管显式并行）→ 开工。"""
    blockers, share = [], []
    for other in workitem_store.repo.list():
        if other.get('id') == wi.get('id') or other.get('state') not in (
                workitem_state.IMPLEMENTING, workitem_state.VERIFYING):
            continue
        if other.get('projectId') != wi.get('projectId'):
            continue
        hit = [p for p in (wi.get('footprint') or [])
               for q in (other.get('footprint') or [])
               if workitem_state.footprintsIntersect(p, q)]
        if hit:
            blockers.append(other)
            share.extend(hit)
    if blockers and not wi.get('forceParallel'):
        wi['queuedFor'] = blockers[0].get('id') or ''
        wi['queuedShare'] = sorted(set(share))[:10]
        core._save(wi)
        return False
    reason = '调度放行'
    if wi.get('forceParallel') and blockers:
        reason += '（主管显式 --force-parallel，与 %s 并行）' % blockers[0].get('id')
    err = workitem_state.transition(wi, workitem_state.IMPLEMENTING, reason)
    if err:
        core._toJudgment(wi, '调度失败：%s' % err)
        return False
    if not int(wi.get('attempt') or 0):
        wi['attempt'] = 1
    core._save(wi)
    return core._startImpl(wi, core._project(wi)) is not None


def _ensureVerify(wi, project, target, base):
    """建/复用验收任务（规格 §2）。

    - 活着：复用同一验收会话（worktree 切到新 commit + 发重验消息）；
    - 已关闭：先用其 kimiSessionId 接回原验收会话（规格"同理接回"），
      接回失败才新开会话（平台验方模板自含规格/target/基线/次数）；
    - 从没有过验收任务：按平台验方模板新建（--detach 到 target）。
    """
    verifyWorktree = wi.get('verifyWorktree') or os.path.join(
        project.get('worktreeRoot') or '', '%s.verify' % wi.get('id'))
    wi['verifyWorktree'] = verifyWorktree
    vtask = core._verifyTask(wi)
    if vtask is not None and not core._ended(vtask):
        if vtask.get('status') in ('running', 'queued'):
            return                  # 验方正忙：等它这轮结束再切
        try:
            workitem_git.checkoutDetached(verifyWorktree, target)
        except workitem_git.GitError as e:
            core._toJudgment(wi, '验收 worktree 切到 %s 失败：%s'
                             % (target[:12], e))
            return
        core._writeSpecCopy(verifyWorktree, wi.get('specPath') or '',
                            wi.get('specText') or '')
        meta = wi.setdefault('verifyMeta', {}).setdefault(vtask['id'], {})
        meta['target'] = target
        meta['base'] = base
        wi['lastImplCommit'] = target
        err = workitem_state.transition(wi, workitem_state.VERIFYING,
                                        '返工后再验收（复用同一验收会话）',
                                        task=vtask['id'], commit=target)
        core._save(wi)
        if err:
            core._toJudgment(wi, '返回验收态失败：%s' % err)
            return
        from modules import workitem_actions    # 延迟导入：actions → core，不成环
        workitem_actions._sendToTask(
            vtask, wi, workitem_prompts.reverifyMessage(target, base),
            '返工后再验收')
        return
    # 先建/切 worktree 再落规格副本——反序会让 makedirs 占住路径，git add 报
    # "already exists"（实测：2026-11-15 流水线首跑）
    try:
        if os.path.isdir(verifyWorktree):
            workitem_git.checkoutDetached(verifyWorktree, target)
        else:
            workitem_git.worktreeAddDetached(project.get('repoPath') or '',
                                             verifyWorktree, target)
    except workitem_git.GitError as e:
        core._toJudgment(wi, '验收 worktree 准备失败：%s' % e)
        return
    core._writeSpecCopy(verifyWorktree, wi.get('specPath') or '',
                        wi.get('specText') or '')
    # 终态验收任务：先接回原会话；接回失败（在忙/会话丢了）才新开
    kimiId = ''
    if vtask is not None:
        kimiId = ((vtask.get('sessionId') or '').strip()
                  or (vtask.get('kimiSessionId') or '').strip())
    vid = ''
    if kimiId:
        vid = _createVerifyTask(
            wi, project, target, base, kimiSessionId=kimiId, quiet=True,
            prompt=workitem_prompts.reverifyMessage(target, base))
    if not vid:
        vid = _createVerifyTask(wi, project, target, base)
    if not vid:
        return                      # _createVerifyTask 已转 needs_judgment
    err = workitem_state.transition(
        wi, workitem_state.VERIFYING,
        '返工后再验收（接回原验收会话）' if kimiId else '实施交付，建验收任务',
        task=vid, commit=target)
    core._save(wi)
    if err:
        core._toJudgment(wi, '进入验收态失败：%s' % err)


def _createVerifyTask(wi, project, target, base, kimiSessionId='', quiet=False,
                      prompt=''):
    """建一张验收任务卡（expects=verify）。返回任务 id；失败转判断返回 ''。

    quiet=True（接回尝试）：失败不转判断，交还调用方决定是否新开。
    """
    verifyWorktree = wi.get('verifyWorktree') or ''
    gates = workitem_integrate.resolveCmd(project.get('gatesCmd') or '',
                                          verifyWorktree, '')
    tests = workitem_integrate.resolveCmd(project.get('testCmd') or '',
                                          verifyWorktree, '')
    policy = dict(project.get('policy') or {})
    # 验方只读不提交：只放行自己的 detached worktree
    policy['allowPaths'] = [verifyWorktree]
    if not prompt:
        prompt = workitem_prompts.verifyPrompt(
            wi, project, core._specInTree(verifyWorktree), target, base,
            int(wi.get('attempt') or 1), gates, tests)
    with core._taskCtx(wi.get('lead') or ''):
        payload, code = tasks_api.createTaskResponse({
            'prompt': prompt,
            'node': project.get('node') or 'local',
            'cwd': verifyWorktree, 'model': wi.get('verifyModel') or '',
            'mode': 'default', 'policy': policy, 'expects': 'verify',
            'title': '%s · 验收' % (wi.get('title') or wi.get('id')),
            'workItemId': wi.get('id') or '',
            'kimiSessionId': kimiSessionId, 'fork': False}, start=True)
    if code >= 400:
        if not quiet:
            core._toJudgment(wi, '建验收任务失败（%s）：%s'
                             % (code, (payload or {}).get('error') or ''))
        return ''
    vid = (payload.get('task') or {}).get('id') or ''
    wi.setdefault('verifyTasks', []).append(vid)
    wi.setdefault('verifyMeta', {})[vid] = {'target': target, 'base': base,
                                            'attempt': int(wi.get('attempt') or 1)}
    wi['lastImplCommit'] = target
    return vid
