# -*- coding: utf-8 -*-
"""执行点（node）语义：对外只有 node（去哪台机器，缺省 local）与 cwd（在哪跑）。

本模块是唯一认识旧字段 engine 的地方：
  - 旧任务记录/旧工作线元数据里可能只有 engine，读取时映射一次，不批量改盘；
  - 请求里再传 engine 则显式 400（不静默忽略、不映射、不警告）。

别处一律只用 node / cwd；新增代码需要读旧字段时，把映射写在这里。
"""
LOCAL = 'local'


def normalize(value):
    """入参 node 归一化：空 -> local。"""
    return str(value or '').strip() or LOCAL


def isLocal(node):
    name = str(node or '').strip()
    return not name or name == LOCAL


def of(task):
    """任务的执行点：新记录直读 node；旧记录没有 node 时按 local 兜底。"""
    return normalize((task or {}).get('node'))


def cwdOf(task):
    """执行目录：新记录在 cwd；旧远端记录在 remoteCwd（读时兜底）。"""
    task = task or {}
    return task.get('cwd') or task.get('remoteCwd') or task.get('workspace') or ''


def rejectLegacyEngine(data):
    """请求里带 engine -> 错误文案；没有则返回 ''。"""
    if isinstance(data, dict) and 'engine' in data:
        return ('engine 字段已移除：用 node 指执行点（缺省 local），'
                '用 cwd 指执行目录（缺省任务目录）')
    return ''


def forgetLegacyEngine(wl):
    """显式设置 node 后清掉旧字段，避免旧值继续参与读取映射。"""
    if isinstance(wl, dict):
        wl.pop('engine', None)


def worklineNode(wl):
    """工作线的执行点：新字段 node；旧元数据只有 engine 时映射一次。

    acp -> local；remote -> 原来记录的 node。旧 remote 线没有 node 时返回
    missing=True，派活须显式报错让主控补，不能悄悄落到本机。
    返回 (node, missing)。
    """
    wl = wl or {}
    name = str(wl.get('node') or '').strip()
    if name:
        return name, False
    if str(wl.get('engine') or '').strip().lower() == 'remote':
        return '', True
    return LOCAL, False