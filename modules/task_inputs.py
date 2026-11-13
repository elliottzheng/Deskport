# -*- coding: utf-8 -*-
"""跨机附件投递：把任务输入投到执行点机器的 `<cwd>/.deskport-inputs/<taskId>/`。

本模块只管"文件怎么到执行点机器"，不建任务、不改任务记录（调用方
`tasks_api.createTaskResponse` 在投递成功后才写任务）。通道由执行点类型决定：

    agent node  → task_inputs_sftp（该执行点配置的 SSH 主机，SFTP）；
    federation  → task_inputs_fed（成员 Deskport 的文件写端点，受
                  `remotes[].filesWrite` 与成员端 `federationRoots` 两层约束）。

约定:
    - 全有或全无：任一文件失败就清掉本次已投的部分（倒序删文件，再尽量
      删掉本次建的空目录），抛 DeliverError；清理自身的异常只记审计
      `task_inputs.cleanup`，不覆盖原始错误；
    - 错误里带上是哪个输入失败、为什么；
    - 投递前先按上限拒（成员声明上限与中枢 `uploadLimitMB` 取小）；
    - 同名不覆盖，自动 `(2)`（与本机暂存一致）；
    - 目录名固定为 taskId，重投不产生第二份目录；目录内重名仍走 `(2)`；
    - 本机执行点不走这里（`tasks_store._collectSources` 仍是本机路径）。
"""
import os
import posixpath
import re

from modules import events, fsutil, node_path
from modules.task_inputs_base import (INPUTS_DIR, MAX_FILE_MB, MAX_FILES,
                                      MAX_TOTAL_MB, DeliverError, safeName)
from modules.task_inputs_fed import FedTarget
from modules.task_inputs_sftp import SshTarget


# ---------------------------------------------------------------- 执行点路由

def _memberOf(node):
    from modules import agent_nodes
    return agent_nodes.findFederationMember(node)


def _nodeOf(node):
    from modules import agent_nodes
    return agent_nodes.findNode(node)


def _route(node):
    """返回 (kind, 入口)：kind = 'federation' | 'ssh'。"""
    name = str(node or '').strip()
    if not name:
        raise DeliverError('缺少执行点（node）')
    member = _memberOf(name)
    if member is not None:
        if not member.get('filesWrite'):
            raise DeliverError(
                '中枢未给执行点 %s 开文件写权限（remotes[].filesWrite=true）；'
                '附件未投递' % (member.get('name') or name), 403)
        if not member.get('files'):
            raise DeliverError(
                '中枢未给执行点 %s 开文件浏览（remotes[].files=true，'
                'filesWrite 依赖它）；附件未投递' % (member.get('name') or name), 403)
        return 'federation', member
    entry = _nodeOf(name)
    if entry is None:
        raise DeliverError('执行点不存在: %s' % name)
    return 'ssh', entry


def _pathModule(kind, node):
    """目标机器的 path 模块（批次 NODE-OS）：按执行点自报 os 选分隔符。

    hub 是 Windows、成员是 Linux 时拿本机 os.path 拼会把
    /home/me/project 拼成 /home/me/project\\.deskport-inputs\\<taskId>。
    os 未知（老 node / 老成员）时保持原行为：ssh 通道一直是 posix，
    联邦通道继续用本机模块。
    """
    fallback = posixpath if kind == 'ssh' else os.path
    return node_path.pathModule(node_path.nodeOs(node), fallback)


def dirFor(node, cwd, taskId):
    """远端输入目录（投递前预检 prompt 长度也要用）。"""
    kind, _entry = _route(node)
    join = _pathModule(kind, node).join
    return join(str(cwd or '').strip(), INPUTS_DIR, str(taskId or '').strip())


def _openTarget(kind, entry):
    if kind == 'federation':
        target = FedTarget(entry)
        info = target.info()
        if info and info.get('writeRootsConfigured') is False:
            target.close()
            raise DeliverError(
                '执行点 %s 未配置 federationRoots（成员端空数组 = 联邦一律只读）；'
                '附件未投递' % target.name, 403)
        return target
    return SshTarget(entry)


# ---------------------------------------------------------------- 输入清单

def _walkDir(source):
    files = []
    for dirpath, _dirnames, filenames in os.walk(source):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError as e:
                raise DeliverError('读取输入失败: %s' % e)
            rel = os.path.relpath(dirpath, source)
            files.append(('' if rel == '.' else rel, full, size))
    return files


def _plan(paths, attachments):
    """本地输入 → 投递条目；同时做文件名/总量/数量校验。"""
    entries = []
    total = 0
    count = 0
    for raw in paths or []:
        if not isinstance(raw, str):
            raise DeliverError('paths 必须是路径字符串')
        source = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(source):
            raise DeliverError('输入文件不存在: %s' % raw)
        name = safeName(os.path.basename(source.rstrip('\\/')))
        if not name:
            raise DeliverError('输入名不合法（需纯名称，≤120 字符）: %s' % source)
        if os.path.isdir(source):
            files = _walkDir(source)
            if not files:
                raise DeliverError('输入目录为空: %s' % source)
            count += len(files)
            total += sum(item[2] for item in files)
            entries.append({'kind': 'dir', 'source': source, 'name': name,
                            'files': files})
        else:
            size = os.path.getsize(source)
            count += 1
            total += size
            entries.append({'kind': 'file', 'source': source,
                            'name': name, 'size': size})
    for item in attachments or []:
        if not isinstance(item, dict):
            raise DeliverError('attachments 条目必须是 {name, contentB64} 对象')
        name = safeName(item.get('name'))
        if not name:
            raise DeliverError('附件名不合法（需纯文件名，≤120 字符）')
        try:
            raw = fsutil.decodeBase64(item.get('contentB64'))
        except ValueError:
            raise DeliverError('附件 %s 的 contentB64 不是合法 base64' % name)
        if len(raw) > MAX_FILE_MB * 1024 * 1024:
            raise DeliverError('附件 %s 超过 %dMB 上限' % (name, MAX_FILE_MB), 413)
        total += len(raw)
        count += 1
        entries.append({'kind': 'bytes', 'name': name, 'data': raw,
                        'size': len(raw)})
    if total > MAX_TOTAL_MB * 1024 * 1024:
        raise DeliverError('输入文件超过 %dMB，请先压缩或分开处理' % MAX_TOTAL_MB)
    if count > MAX_FILES:
        raise DeliverError('输入文件数量超过 %d，请先压缩或分开处理' % MAX_FILES)
    return entries


def _checkLimit(entries, limitMB):
    limit = int(limitMB) * 1024 * 1024
    for entry in entries:
        sizes = ([item[2] for item in entry['files']]
                 if entry['kind'] == 'dir' else [entry['size']])
        for size in sizes:
            if size > limit:
                raise DeliverError('单个输入超过执行点上限 %dMB: %s'
                                   % (int(limitMB), entry['name']), 413)


# ---------------------------------------------------------------- 投递

def _place(target, entry, base):
    if entry['kind'] == 'file':
        return target.putFile(entry['source'], entry['name'], base, track=True)
    if entry['kind'] == 'bytes':
        return target.putBytes(entry['data'], entry['name'], base, track=True)
    folder = target.mkdirUnique(entry['name'], base)
    for rel, localPath, _size in entry['files']:
        parts = [p for p in re.split(r'[\\/]+', rel) if p]
        directory = target.join(folder, *parts) if parts else folder
        target.ensureDir(directory)
        target.putFile(localPath, os.path.basename(localPath), directory)
    return folder


def _placeAll(target, entries, base):
    """逐个投递；失败时明确指出是哪个输入、为什么（清理交给调用方）。"""
    out = []
    for item in entries:
        try:
            out.append(_place(target, item, base))
        except DeliverError as e:
            raise DeliverError('附件 %s 投递失败: %s' % (item['name'], e),
                               e.status) from None
        except Exception as e:
            raise DeliverError('附件 %s 投递失败: %s'
                               % (item['name'], str(e)[:200]), 502) from None
    return out


def _cleanup(target, base, made):
    """失败清理：倒序删本次已投的文件，再尽量删掉本次建的空目录。

    清理自身的问题（异常、删不掉的残留）只记审计，**绝不覆盖原始错误**：
    调用方仍然拿到"哪个文件失败、为什么"，残留另有一条 task_inputs.cleanup。
    """
    try:
        left = list(target.cleanup(base, made) or [])
    except Exception as e:
        events.log('task_inputs.cleanup', base, 'fail',
                   '%s: %s' % (type(e).__name__, str(e)[:160]))
        return
    if left:
        events.log('task_inputs.cleanup', base, 'partial',
                   '残留 %d 项: %s' % (len(left), '、'.join(left[:5])))


def deliver(node, cwd, taskId, paths=(), attachments=()):
    """把任务输入投到执行点；成功返回 {'dir': 远端目录, 'paths': [远端路径…]}。

    任一文件失败都会清掉本次已投的部分，然后抛 DeliverError（任务不建）。
    """
    name = str(node or '').strip()
    cwd = str(cwd or '').strip()
    taskId = str(taskId or '').strip()
    if not cwd:
        raise DeliverError('执行点 %s 需要 cwd（该机器上的工作目录）' % name)
    if not taskId or taskId in ('.', '..') or '/' in taskId or '\\' in taskId:
        raise DeliverError('taskId 不合法')
    kind, entry = _route(name)
    entries = _plan(paths, attachments)
    if not entries:
        raise DeliverError('没有可投递的附件')
    target = _openTarget(kind, entry)
    base = target.join(cwd, INPUTS_DIR, taskId)
    made = {'inputs': False, 'task': False}
    try:
        _checkLimit(entries, target.maxMB())
        if not target.isdir(cwd):
            raise DeliverError('执行点 %s 的工作目录不存在: %s' % (name, cwd))
        made['inputs'] = target.ensureDir(target.join(cwd, INPUTS_DIR))
        made['task'] = target.ensureDir(base)
        return {'dir': base, 'paths': _placeAll(target, entries, base)}
    except DeliverError:
        _cleanup(target, base, made)
        raise
    except Exception as e:
        _cleanup(target, base, made)
        raise DeliverError('附件投递失败（%s）: %s' % (name, str(e)[:200]), 502)
    finally:
        target.close()
