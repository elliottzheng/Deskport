# -*- coding: utf-8 -*-
"""文件系统共享常量/工具：避免功能模块为了同一份规则互相 import。"""
import base64
import os
import re

SKIP_DIRS = {'node_modules', '__pycache__', '.git', '.venv', 'venv', 'logs',
             'thumbs', '.run', 'dist', 'build', 'tmp', 'temp'}


def decodeBase64(contentB64):
    """严格 base64 解码：剔除空白后必须只含 base64 字母表；坏了抛 ValueError。

    智能体拼 base64 截断/混入杂字符是常见事故——宁可 400 也不静默写空文件。
    """
    s = re.sub(r'\s+', '', str(contentB64 or ''))
    if not s:
        raise ValueError('empty')
    if not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', s):
        raise ValueError('not base64')
    return base64.b64decode(s + '=' * (-len(s) % 4), validate=True)


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

