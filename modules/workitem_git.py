# -*- coding: utf-8 -*-
"""平台侧 git 执行（批次 G2）：worktree/分支/合并/差异，全部由平台跑，
不经智能体（规格 §2）。本机执行点用子进程直跑 git（项目 node 暂只支持
local，见 workitem_projects.validate）。

命令一律走 run()：argv 列表（不做 shell 拼接，避免引号地狱）、cwd 指向
目标仓库、超时强杀（Windows 上 taskkill /T /F 按 PID 杀进程树——只杀自己
起的这一棵）。输出按 (exit, text) 返回，截断到 MAX_OUT 防日志爆炸。
"""
import logging
import os
import subprocess

log = logging.getLogger('workitem-git')

MAX_OUT = 64 * 1024
DEFAULT_TIMEOUT = 120

if os.name == 'nt':
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0


class GitError(Exception):
    """git 执行失败（非零退出或起不来）。带 exit 与输出尾部。"""

    def __init__(self, args, code, out):
        self.args_ = args
        self.exit = code
        self.out = out
        super().__init__('git %s 失败（exit=%s）：%s'
                         % (' '.join(args[:3]), code, (out or '')[-400:]))


def run(repo, args, timeout=DEFAULT_TIMEOUT, check=True):
    """在 repo 里跑一条 git 命令，返回 (exit, out)。check=True 时非零抛
    GitError（调用方多数场景失败即止损，少数场景自己看 exit）。"""
    try:
        proc = subprocess.run(['git'] + [str(a) for a in args], cwd=repo,
                              capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or '') if isinstance(e.stdout, str) else '') \
            + ((e.stderr or '') if isinstance(e.stderr, str) else '')
        raise GitError(args, 'timeout', out[-MAX_OUT:]) from None
    except OSError as e:
        raise GitError(args, 'oserror', str(e)) from None
    out = ((proc.stdout or '') + (proc.stderr or ''))[-MAX_OUT:]
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, out)
    return proc.returncode, out


def head(path):
    """路径（仓库/worktree）当前 HEAD 的完整 commit；失败返回 ''。"""
    try:
        code, out = run(path, ['rev-parse', 'HEAD'])
    except GitError:
        return ''
    return out.strip().splitlines()[0] if out.strip() else ''


def branchExists(repo, branch):
    code, _out = run(repo, ['rev-parse', '--verify', '--quiet',
                            'refs/heads/%s' % branch], check=False)
    return code == 0


def worktreeAdd(repo, worktree, branch, base):
    """建 worktree 与分支：git worktree add <worktree> -b <branch> <base>。

    分支已存在（上次崩在这之后）先删再建——幂等，供重试路径调用。
    """
    if branchExists(repo, branch):
        run(repo, ['branch', '-D', branch])
    if os.path.isdir(worktree):
        run(repo, ['worktree', 'remove', '--force', worktree], check=False)
        run(repo, ['worktree', 'prune'], check=False)
    run(repo, ['worktree', 'add', worktree, '-b', branch, base])
    return head(worktree)


def worktreeAddDetached(repo, worktree, commit):
    """独立验收 worktree：--detach 到交付的 commit（规格 §2）。"""
    if os.path.isdir(worktree):
        run(repo, ['worktree', 'remove', '--force', worktree], check=False)
        run(repo, ['worktree', 'prune'], check=False)
    run(repo, ['worktree', 'add', '--detach', worktree, commit])
    return head(worktree)


def checkoutDetached(worktree, commit):
    """把已有 worktree 切到另一个 commit（返工后再验收：复用同一 worktree）。"""
    run(worktree, ['checkout', '--detach', commit])
    return head(worktree)


def worktreeRemove(repo, worktree):
    """收尾拆 worktree（分支留着——集成/回溯还要用）。"""
    if not worktree or not os.path.isdir(worktree):
        return
    try:
        run(repo, ['worktree', 'remove', '--force', worktree], check=False)
        run(repo, ['worktree', 'prune'], check=False)
    except GitError as e:
        log.info('worktree remove skipped %s: %s', worktree, e)


def mergeMain(worktree, mainBranch):
    """验收前同步（§2b）：在工作项 worktree 里 git merge <mainBranch>。

    返回 (ok, conflict)。冲突不算错误——调用方走"发回实施任务解决"。
    """
    code, out = run(worktree, ['merge', mainBranch, '--no-edit'], check=False)
    if code == 0:
        return True, ''
    conflict = '<<<<<<<' in out or 'CONFLICT' in out \
        or 'Automatic merge failed' in out
    if conflict:
        run(worktree, ['merge', '--abort'], check=False)
        return False, out[-4000:]
    raise GitError(['merge', mainBranch], code, out)


def mergeNoCommit(repo, branch):
    """集成第一步：主仓 git merge --no-ff --no-commit <branch>。

    返回 (ok, out)：ok=False 且 out 含冲突标记 → 走"同步"路径（§2b）；
    其它失败由调用方按返工处理。
    """
    code, out = run(repo, ['merge', '--no-ff', '--no-commit', branch],
                    check=False)
    return code == 0, out


def mergeAbort(repo):
    run(repo, ['merge', '--abort'], check=False)


def commitMerge(repo, message):
    """提交合并（--no-edit 之外显式给信息，便于回溯到工作项）。"""
    run(repo, ['commit', '--no-edit', '-m', message])
    return head(repo)


def mergeBase(repo, refA, refB):
    code, out = run(repo, ['merge-base', refA, refB], check=False)
    if code != 0:
        return ''
    return out.strip().splitlines()[0] if out.strip() else ''


def diffFiles(worktree, base):
    """base..HEAD 改动的文件清单（footprint 校正用，§2b）。"""
    code, out = run(worktree, ['diff', '--name-only', '%s..HEAD' % base],
                    check=False)
    if code != 0:
        return []
    return [ln.strip().replace('\\', '/') for ln in out.splitlines()
            if ln.strip()]


def isAncestor(repo, maybeAncestor, descendant):
    code, _out = run(repo, ['merge-base', '--is-ancestor', maybeAncestor,
                            descendant], check=False)
    return code == 0
