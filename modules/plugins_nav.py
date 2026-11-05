# -*- coding: utf-8 -*-
"""插件导航与标签推导：legacy 标签猜测、原生 nav 项、openWith 扩展名。

从 plugins 拆出（2026-11-05）。纯函数，不碰文件系统以外的 IO，
不 import 插件代码（legacyLabel 接收已导入的 module 作参数）。
"""
import os
import re

from modules.plugins_base import DEFAULT_ICON

TITLE_SUFFIX_RE = re.compile(r'\s*[-–—|]\s*RemoteConnecter\s*$', re.I)
TITLE_BLOCKLIST = {'document', 'untitled', 'index', 'home', 'new page', ''}
DOC_PREFIX_RE = re.compile(r'^[^—–-]{1,24}(插件)?\s*[—–-]\s*')


def templateTitle(pluginDir):
    """从插件 templates/ 里第一个像样的 <title> 猜标签。"""
    templateDir = os.path.join(pluginDir, 'templates')
    try:
        names = sorted(os.listdir(templateDir))[:20]
    except OSError:
        return ''
    for name in names:
        if not name.endswith('.html'):
            continue
        try:
            with open(os.path.join(templateDir, name), encoding='utf-8',
                      errors='replace') as f:
                text = f.read(8192)
        except OSError:
            continue
        match = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
        if not match:
            continue
        title = re.sub(r'\s+', ' ', match.group(1)).strip()
        if '{{' in title or '{%' in title:
            continue
        title = TITLE_SUFFIX_RE.sub('', title).strip()
        if title.lower() in TITLE_BLOCKLIST or len(title) > 30:
            continue
        return title
    return ''


def docLabel(module):
    doc = (module.__doc__ or '').strip().splitlines()
    if not doc:
        return ''
    line = doc[0].strip().split('。')[0]
    line = DOC_PREFIX_RE.sub('', line).strip()
    return line if 0 < len(line) <= 30 else ''


def legacyLabel(pluginDir, module, pluginId, manifest):
    """legacy 插件没有 name 字段时的标签推导: 清单名 > 模板标题 > 文档首行 > id。"""
    name = str(manifest.get('name') or '').strip()
    if name:
        return name[:24]
    return templateTitle(pluginDir) or docLabel(module) or pluginId


def nativeNav(pluginId, manifest):
    raw = manifest.get('nav')
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw[:6]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get('label') or '').strip()[:24]
        if not label:
            continue
        path = str(entry.get('path') or '')
        if path and not path.startswith('/'):
            path = '/' + path
        if '..' in path:
            continue
        href = '/p/' + pluginId + (path if path not in ('', '/') else '')
        items.append({'id': pluginId,
                      'label': label,
                      'icon': str(entry.get('icon') or DEFAULT_ICON),
                      'href': href, 'external': False})
    return items


def openWith(manifest):
    raw = manifest.get('openWith')
    if not isinstance(raw, list):
        return []
    exts = []
    for item in raw[:40]:
        ext = str(item or '').strip().lower().lstrip('.')
        if ext and re.match(r'^[a-z0-9]{1,12}$', ext) and ext not in exts:
            exts.append(ext)
    return exts
