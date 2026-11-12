# -*- coding: utf-8 -*-
"""收件箱扫描：目录观察、内容去重、缩略图入口。"""
import hashlib
import json
import os
import re
import time

from flask import jsonify, request, send_file

import config
from modules import fsutil, media
from modules.inbox_base import blueprint, log

MAX_AGE_DAYS = 45
MAX_SCAN_FILES = 6000
HASH_LIMIT = 64 * 1024 * 1024
SKIP_EXTS = {'.lnk', '.url', '.tmp', '.part', '.crdownload', '.ini'}

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
KIND_MAP = {
    'image': IMAGE_EXTS,
    'pdf': {'.pdf'},
    'doc': {'.doc', '.docx', '.wps', '.rtf', '.md', '.txt'},
    'sheet': {'.xls', '.xlsx', '.csv'},
    'slide': {'.ppt', '.pptx'},
    'archive': {'.zip', '.rar', '.7z', '.tar', '.gz'},
    'media': {'.mp4', '.mov', '.avi', '.mkv', '.mp3', '.wav', '.m4a'},
}

class _DigestCache:
    """有界内存摘要缓存: 按 (path, size, mtime) 键记忆, 不落盘。

    原先是模块级裸字典 + 在调用点里判长度清空; 收进类后"满了怎么办"
    只有一个实现。
    """

    def __init__(self, limit=2000):
        self.limit = limit
        self.map = {}

    def get(self, key):
        return self.map.get(key)

    def put(self, key, value):
        if len(self.map) > self.limit:
            self.map.clear()
        self.map[key] = value


_hashCache = _DigestCache()

# 微信/浏览器重复保存时的副本后缀: (1)/(2)、全角（1）、 - 副本、副本
_COPY_SUFFIX = re.compile(
    r'(?:\s*[\(（]\d+[\)）]|\s*-\s*副本(?:\s*[\(（]\d+[\)）])?|\s*副本(?:\s*[\(（]\d+[\)）])?)$')


def _stripCopy(stem):
    """反复剥掉副本后缀, 返回 (干净 stem, 剥离层数)。"""
    depth = 0
    while True:
        stripped = _COPY_SUFFIX.sub('', stem).strip()
        if not stripped or stripped == stem:
            return stem, depth
        stem = stripped
        depth += 1


def canonicalName(name):
    """去掉副本序号/副本标记后的文件名 (保留扩展名)。用于"同一份文件"归组。"""
    stem, ext = os.path.splitext(name)
    stem, _ = _stripCopy(stem)
    return (stem + ext).lower()


def _copyDepth(name):
    stem, _ext = os.path.splitext(name)
    return _stripCopy(stem)[1]


def _under(path, roots):
    return any(fsutil.isUnder(path, root) for root in roots)


def _sourceOf(root):
    low = root.lower()
    base = os.path.basename(root).lower()
    if base == '手机上传':
        return '手机'
    if 'xwechat_files' in low or 'wechat' in low:
        return '微信'
    if 'qq' in low or 'tencent' in low or base == 'filerecv':
        return 'QQ'
    if base == 'downloads':
        return '下载'
    if base == 'desktop':
        return '桌面'
    return '其他'


def _kindOf(name):
    ext = os.path.splitext(name)[1].lower()
    for kind, exts in KIND_MAP.items():
        if ext in exts:
            return kind
    return 'other'


def _sha256(path, limit=HASH_LIMIT):
    try:
        size = os.path.getsize(path)
        if size > limit:
            return None
        st = os.stat(path)
        key = (path, size, st.st_mtime_ns)
        hit = _hashCache.get(key)
        if hit:
            return hit
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        digest = h.hexdigest()
        _hashCache.put(key, digest)
        return digest
    except OSError:
        return None


FINGERPRINT_BYTES = 64 * 1024


class _FingerprintCache:
    """内容指纹缓存: 内存字典 + 落盘, 重启不重算。

    原先是 4 个模块级全局 (_fpCache/_fpLoaded/_fpDirty/_fpLastSave), 每个
    函数各自 global 声明, 状态散落三处。收进类后只有一个持有者
    (与 tasks_store._LazyRepo 同一做法)。

    pathFn 延迟求值: config.stateDir 可能随配置变化, 不能在 import 时定格。
    """

    SAVE_INTERVAL = 30.0
    LIMIT = 5000

    def __init__(self, pathFn):
        self.pathFn = pathFn
        self.map = {}
        self.loaded = False
        self.dirty = False
        self.lastSave = 0.0

    def load(self):
        """把上次算过的指纹从状态目录读回，重启后不必重新读盘/过杀软。"""
        if self.loaded:
            return
        self.loaded = True
        try:
            with open(self.pathFn(), encoding='utf-8') as f:
                for path, size, mtime, digest in json.load(f):
                    self.map[(path, int(size), int(mtime))] = digest
        except (OSError, ValueError, TypeError):
            self.map.clear()

    def get(self, key):
        self.load()
        return self.map.get(key)

    def put(self, key, digest):
        self.load()
        if len(self.map) > self.LIMIT:
            self.map.clear()
        self.map[key] = digest
        self.dirty = True
        self.save()

    def save(self, force=False):
        if not self.dirty:
            return
        now = time.time()
        if not force and now - self.lastSave < self.SAVE_INTERVAL:
            return
        try:
            target = self.pathFn()
            tmp = target + '.tmp'
            payload = [[k[0], k[1], k[2], v] for k, v in self.map.items()]
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, target)
            self.dirty = False
            self.lastSave = now
        except OSError:
            pass


_fpCache = _FingerprintCache(lambda: config.statePath('inbox-fingerprints.json'))


def _fingerprint(path):
    """低成本内容指纹: 大小 + 头尾各 64KB。

    收件箱去重只需要判断"同名同大小"的副本是否为同一份内容。对 2GB 级文件
    做全量 SHA-256 会让冷启动扫盘耗时数分钟(实测 544 个副本共 2.19GB)；
    头+尾采样已足够区分实际内容不同的文件。指纹落盘缓存, 重启不重算。
    """
    try:
        st = os.stat(path)
        size = st.st_size
        key = (path, size, st.st_mtime_ns)
        hit = _fpCache.get(key)
        if hit:
            return hit
        h = hashlib.sha256()
        h.update(str(size).encode())
        with open(path, 'rb') as f:
            h.update(f.read(FINGERPRINT_BYTES))
            if size > FINGERPRINT_BYTES * 2:
                f.seek(-FINGERPRINT_BYTES, os.SEEK_END)
                h.update(f.read(FINGERPRINT_BYTES))
            elif size > FINGERPRINT_BYTES:
                h.update(f.read())
        digest = h.hexdigest()
        _fpCache.put(key, digest)
        return digest
    except OSError:
        return None


def _scanFiles():
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    items = []
    for root in config.getInboxDirs():
        source = _sourceOf(root)
        stack = [(root, 0)]
        while stack and len(items) < MAX_SCAN_FILES:
            path, depth = stack.pop()
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if e.name.startswith('.') or e.name.lower() in fsutil.SKIP_DIRS:
                                    continue
                                if depth < 3 and e.stat().st_mtime > cutoff:
                                    stack.append((e.path, depth + 1))
                                continue
                            if not e.is_file(follow_symlinks=False):
                                continue
                            if os.path.splitext(e.name)[1].lower() in SKIP_EXTS:
                                continue
                            st = e.stat()
                            if st.st_mtime < cutoff:
                                continue
                            attrs = getattr(st, 'st_file_attributes', 0)
                            if attrs & 0x2 or attrs & 0x4:   # hidden/system
                                continue
                            items.append({'name': e.name, 'path': e.path,
                                          'size': st.st_size, 'mtime': int(st.st_mtime),
                                          'source': source})
                        except OSError:
                            continue
            except OSError:
                continue
    return items


def _dedupe(items):
    """去掉副本序号后同名 + 同大小 → 哈希确认；一致合并，不一致分开。

    微信重发的文件常是 "报告.pdf" / "报告(1).pdf" / "报告(2).pdf"，
    仅按原始文件名分组会漏掉它们。
    """
    groups = {}
    for it in items:
        groups.setdefault((canonicalName(it['name']), it['size']), []).append(it)
    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        buckets = {}
        for it in group:
            h = _fingerprint(it['path'])
            buckets.setdefault(h if h else id(it), []).append(it)
        for bucket in buckets.values():
            # 优先展示副本序号最少的名字 (原名优先), 同等时取最新
            bucket.sort(key=lambda x: (_copyDepth(x['name']), -x['mtime']))
            head = dict(bucket[0])
            if len(bucket) > 1:
                head['dupes'] = [{'path': x['path'], 'mtime': x['mtime'],
                                  'source': x['source']} for x in bucket[1:]]
            merged.append(head)
    for m in merged:
        m['ext'] = os.path.splitext(m['name'])[1].lower()
        m['kind'] = _kindOf(m['name'])
        m['thumb'] = ('/api/inbox/thumb?path=' + _quote(m['path'])) if m['kind'] == 'image' else None
        m['id'] = hashlib.sha1(m['path'].encode('utf-8', 'replace')).hexdigest()[:16]
        m.setdefault('dupes', [])
    merged.sort(key=lambda x: -x['mtime'])
    return merged


def _quote(text):
    from urllib.parse import quote
    return quote(text, safe='')


@blueprint.route('/api/inbox/recent')
def inboxRecent():
    limit = min(int(request.args.get('limit') or 80), 300)
    t0 = time.perf_counter()
    items = _dedupe(_scanFiles())
    _fpCache.save(force=True)
    total = len(items)
    log.info('inbox scan items=%d total=%d %.0fms', len(items), total,
             (time.perf_counter() - t0) * 1000)
    return jsonify({'items': items[:limit], 'total': total,
                    'enabled': config.isInboxEnabled(),
                    'dirs': [{'path': d, 'source': _sourceOf(d),
                              'label': os.path.basename(d.rstrip('\\'))}
                             for d in config.getInboxDirs()],
                    'scannedAt': int(time.time())})


@blueprint.route('/api/inbox/thumb')
def inboxThumb():
    path = request.args.get('path') or ''
    if not os.path.isfile(path) or not _under(path, config.getInboxDirs()):
        return jsonify({'error': '文件不存在'}), 404
    try:
        cached = media.thumbPath(path)
    except media.MediaError as e:
        return jsonify({'error': str(e)}), e.status
    return send_file(cached, mimetype='image/png')

