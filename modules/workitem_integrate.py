# -*- coding: utf-8 -*-
"""集成作业（批次 G2）：平台作业，不经智能体——主仓 merge → gates → tests
→ 全绿提交合并、任一失败 abort 并把输出发回实施任务（规格 §2）。

同一项目的集成串行排队（§2b 收尾）：一把每项目一个的锁，作业线程先拿锁
再动主仓——两个工作项同时集成会互相踩 merge 状态。输出完整写工作项日志
（workitem_store.appendLog），返工消息只带截尾 4KB。

结果经回调交回流水线（单一写者仍是 workitem_pipeline）：done(wid, outcome,
detail)，outcome ∈ ok（detail=commit）/ sync（合并冲突，回同步路径）/
rework（门禁或测试红，detail={step,out}）。本模块不 import tasks_* / Flask。
"""
import logging
import shutil
import subprocess
import tempfile
import threading
import time

from modules import workitem_git, workitem_store

log = logging.getLogger('workitem-integrate')

GATES_TIMEOUT = 900
TEST_TIMEOUT = 1800
REWORK_OUT_MAX = 4096

_locks = {}
_locksGuard = threading.Lock()


def projectLock(projectId):
    with _locksGuard:
        if projectId not in _locks:
            _locks[projectId] = threading.Lock()
        return _locks[projectId]


def resolveCmd(cmd, repo, tmp):
    """项目命令里的 {repo}/{tmp} 占位符替换。"""
    return (cmd or '').replace('{repo}', repo).replace('{tmp}', tmp or '')


def _runShell(cmd, cwd, timeout):
    """跑项目命令（字符串，shell 执行）。Windows 不弹窗；超时强杀。"""
    try:
        proc = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=timeout,
                              creationflags=getattr(subprocess,
                                                    'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as e:
        out = (e.stdout if isinstance(e.stdout, str) else '') \
            + (e.stderr if isinstance(e.stderr, str) else '')
        return 'timeout', out[-workitem_git.MAX_OUT:]
    out = ((proc.stdout or '') + (proc.stderr or ''))
    return ('ok' if proc.returncode == 0 else 'fail'), out


def _step(wid, name, cmd, cwd, timeout, tmp):
    """跑一段并记日志；返回 (status, out)。"""
    resolved = resolveCmd(cmd, cwd, tmp)
    workitem_store.appendLog(wid, '\n===== %s =====\n$ %s\n' % (name, resolved))
    started = time.monotonic()
    status, out = _runShell(resolved, cwd, timeout)
    workitem_store.appendLog(wid, (out or '(无输出)')[-workitem_git.MAX_OUT:])
    workitem_store.appendLog(wid, '----- %s: %s（%.1fs）'
                             % (name, status, time.monotonic() - started))
    return status, out


def start(wid, project, branch, done):
    """起集成作业（后台线程；同项目串行）。done(wid, outcome, detail) 回流水线。"""
    thread = threading.Thread(target=_job,
                              args=(wid, project, branch, done),
                              daemon=True, name='wi-integrate-%s' % wid)
    thread.start()
    return thread


def _job(wid, project, branch, done):
    repo = project.get('repoPath') or ''
    lock = projectLock(project.get('id') or '')
    acquired = lock.acquire(timeout=6 * 3600)
    if not acquired:
        done(wid, 'rework', {'step': 'queue',
                        'out': '集成排队超时（同项目另一集成跑了 6 小时？）'})
        return
    tmp = ''
    try:
        workitem_store.appendLog(wid, '\n########## 集成开始 %s ##########'
                                 % time.strftime('%F %T'))
        ok, out = workitem_git.mergeNoCommit(repo, branch)
        if not ok:
            workitem_git.mergeAbort(repo)
            workitem_store.appendLog(wid, out[-4000:])
            if '<<<<<<<' in out or 'CONFLICT' in out \
                    or 'Automatic merge failed' in out:
                done(wid, 'sync', out[-4000:])   # §2b：main 又动了 → 回同步
            else:
                done(wid, 'rework', {'step': 'merge', 'out': out[-REWORK_OUT_MAX:]})
            return
        tmp = tempfile.mkdtemp(prefix='wi-integrate-')
        status, out = _step(wid, 'gates', project.get('gatesCmd') or 'true',
                            repo, GATES_TIMEOUT, tmp)
        if status != 'ok':
            workitem_git.mergeAbort(repo)
            done(wid, 'rework', {'step': 'gates', 'out': out[-REWORK_OUT_MAX:]})
            return
        status, out = _step(wid, 'tests', project.get('testCmd') or 'true',
                            repo, TEST_TIMEOUT, tmp)
        if status != 'ok':
            workitem_git.mergeAbort(repo)
            done(wid, 'rework', {'step': 'tests', 'out': out[-REWORK_OUT_MAX:]})
            return
        commit = workitem_git.commitMerge(
            repo, 'Merge workitem %s（批次 G2 集成作业）' % wid)
        workitem_store.appendLog(wid, '----- 合并已提交：%s' % commit)
        done(wid, 'ok', commit)
    except Exception as e:      # 作业自己炸了：abort 保主仓干净，交回流水线
        log.warning('integrate job %s failed: %s', wid, e)
        try:
            workitem_git.mergeAbort(repo)
        except Exception:
            pass
        workitem_store.appendLog(wid, '作业异常：%s' % e)
        done(wid, 'rework', {'step': 'job',
                        'out': '集成作业异常：%s' % str(e)[:REWORK_OUT_MAX]})
    finally:
        lock.release()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
