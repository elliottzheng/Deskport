# -*- coding: utf-8 -*-
"""本机任务的「工作目录」（cwd）：与 Deskport 造的任务目录（workspace）分开。

engine=acp 也可以指定 cwd——ACP 进程在那个目录里跑，便于直接改项目；
workspace 仍然由 Deskport 建，日志 / inputs/ / close 清理只认 workspace。
cwd 是非空入参，所以必须校验：绝对路径、存在、是目录、且在允许的根之下
（config.workDirs 或 fallbackDir），不能变成任意目录入口。
"""
import os

import config
from modules import fsutil

MAX_CWD = 500   # 与 remote cwd 的上限一致


def resolve(value, engine='acp'):
    """校验并归一化 acp 任务的 cwd；返回 '' = 未指定（跑在 workspace）。

    engine != acp 时原样忽略（remote 的 cwd 在远端，由远端校验）；
    非法/越界抛 ValueError（路由层转 400）。
    """
    if (engine or 'acp').lower() != 'acp':
        return ''
    raw = str(value or '').strip()
    if not raw:
        return ''
    if len(raw) > MAX_CWD:
        raise ValueError('cwd 过长（上限 %d 字符）' % MAX_CWD)
    if not os.path.isabs(raw):
        raise ValueError('cwd 必须是绝对路径: %s' % raw)
    path = os.path.realpath(raw)
    if not os.path.isdir(path):
        raise ValueError('cwd 不存在或不是目录: %s' % raw)
    if not any(fsutil.isUnder(path, root) for root in _roots()):
        raise ValueError('cwd 越界：必须在工作目录或默认工作空间之下'
                         '（config.workDirs / fallbackDir）: %s' % raw)
    return path


def _roots():
    roots = [os.path.realpath(d) for d in config.getWorkDirs() if d]
    fallback = config.getFallbackDir()
    if fallback:
        roots.append(os.path.realpath(fallback))
    return roots


def differs(task):
    """任务的工作目录是否与 workspace 不同（决定 prompt 要不要指路）。"""
    cwd = task.get('cwd') or ''
    workspace = task.get('workspace') or ''
    if not cwd or not workspace:
        return False
    return os.path.normcase(os.path.abspath(cwd)) != \
        os.path.normcase(os.path.abspath(workspace))


def inputLabel(task, rel):
    """输入副本在 prompt 里的写法：cwd 不同时给绝对路径，否则保持相对。"""
    if differs(task):
        return os.path.join(task.get('workspace') or '', rel)
    return rel


def notice(task):
    """cwd 与 workspace 分离时追加的一行说明（否则模型会到处找附件）。"""
    if not differs(task):
        return ''
    return ('你的工作目录是 %s；附件在 %s 下（绝对路径见上）。'
            % (task.get('cwd'), os.path.join(task.get('workspace') or '', 'inputs')))