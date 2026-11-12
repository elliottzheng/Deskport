# -*- coding: utf-8 -*-
"""任务暂存区：把上传流式落到 `_agent_tasks/_uploads/`。

2026-11-13 从 tasks_store 抽出（结构门禁：tasks_store 是存量例外，
只许变短）。`/api/tasks/files` 与 `/api/agent/share` 共用这一份落盘逻辑。

层次：本模块不 import 任何特性层模块——暂存区根由调用方传入
（`tasks_store.taskRoot()`），依赖方向仍是 特性层 → 本模块。文件名
校验 `_safeAttachmentName` 也在这里，tasks_store 反向导入使用。
"""
import os
import re
import uuid

from modules import fsutil

STAGING_CHUNK = 1024 * 1024   # 暂存区写盘块大小（不整读进内存）


def _safeAttachmentName(name):
    """附件只允许纯文件名：无路径分隔符、无控制字符、≤120 字符。"""
    text = str(name or '').strip()
    if not text or len(text) > 120:
        return ''
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', text):
        return ''
    return text


def saveStagedStream(name, stream, limitBytes, root, chunkSize=STAGING_CHUNK,
                     folder=None):
    """流式写入任务暂存区，返回绝对路径。

    root = 暂存区根（`tasks_store.taskRoot()`）。不传 folder 时每批文件
    一个随机子目录（与既有行为一致）；传 folder 时写进该目录
    （organize=folder）。重名走 uniquePath 的 (2)，绝不覆盖。
    文件名不合法 / 超过 limitBytes 抛 ValueError（调用方区分 400/413）；
    写盘失败抛 OSError。任何失败都不留半成品。
    """
    safe = _safeAttachmentName(name)
    if not safe:
        raise ValueError('name 必须是纯文件名（无路径分隔符，≤120 字符）')
    if folder:
        dest = folder
        os.makedirs(dest, exist_ok=True)
    else:
        dest = os.path.join(root, '_uploads', uuid.uuid4().hex[:8])
        os.makedirs(dest, exist_ok=True)
    path = fsutil.uniquePath(dest, safe, start=2, sep='')
    size = 0
    try:
        with open(path, 'wb') as f:
            while True:
                chunk = stream.read(chunkSize)
                if not chunk:
                    break
                size += len(chunk)
                if size > limitBytes:
                    raise ValueError('文件超过 %dMB 上限'
                                     % max(1, limitBytes // (1024 * 1024)))
                f.write(chunk)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        if not folder:
            try:
                if not os.listdir(dest):
                    os.rmdir(dest)
            except OSError:
                pass
        raise
    return path


def createStagedFolder(name, root):
    """在暂存区建一个命名文件夹（organize=folder），重名 (2)。

    直接建在 `<root>/_uploads/` 下（同名的第二份得到 `name(2)`），
    返回绝对路径；名称不合法抛 ValueError。调用方负责在失败时清理。
    """
    safe = _safeAttachmentName(name)
    if not safe or safe in ('.', '..'):
        raise ValueError('文件夹名不合法（需纯名称，≤120 字符）')
    parent = os.path.join(root, '_uploads')
    os.makedirs(parent, exist_ok=True)
    path = fsutil.uniquePath(parent, safe, isDir=True, start=2, sep='')
    os.makedirs(path)
    return path