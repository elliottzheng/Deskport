# -*- coding: utf-8 -*-
"""文件系统共享常量/工具：避免功能模块为了同一份规则互相 import。"""
import os

SKIP_DIRS = {'node_modules', '__pycache__', '.git', '.venv', 'venv', 'logs',
             'thumbs', '.run', 'dist', 'build', 'tmp', 'temp'}


def isUnder(path, root):
    """路径是否在 root 之下（大小写不敏感，跨盘返回 False）。"""
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except (OSError, ValueError):
        return False


def uniquePath(directory, name, isDir=False, start=1, sep=' ',
               pathmod=os.path, exists=None):
    """在 directory 下给 name 找一个不冲突的路径。

    默认为本机路径；SSH 远端传 pathmod=posixpath 并注入 exists=sftp 探测函数。
    """
    exists = exists or os.path.exists
    candidate = pathmod.join(directory, name)
    if not exists(candidate):
        return candidate
    stem, ext = (name, '') if isDir else pathmod.splitext(name)
    for i in range(start, 1000):
        cand = pathmod.join(directory, '%s%s(%d)%s' % (stem, sep, i, ext))
        if not exists(cand):
            return cand
    raise OSError('同名条目过多: %s' % name)

