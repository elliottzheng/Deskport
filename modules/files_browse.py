# -*- coding: utf-8 -*-
"""文件浏览与轻量编辑：列表/下载/重命名/新建/读写/搜索。"""
import hashlib
import os
import string
import tempfile

from flask import jsonify, request, send_file

from modules.files_base import (blueprint, FILE_ATTRIBUTE_HIDDEN,
                                FILE_ATTRIBUTE_SYSTEM, FILE_ATTRIBUTE_READONLY)
from modules import federation_write, textio

def _drives():
    return [letter + ':\\' for letter in string.ascii_uppercase
            if os.path.exists(letter + ':\\')]


SEARCH_MAX_RESULTS = 2000
SEARCH_MAX_DEPTH = 5


def _parent(path):
    parent = os.path.dirname(path.rstrip('\\/'))
    if not parent or parent == path.rstrip('\\/'):
        return None
    return parent


def _statEntry(fullPath, name, entryType):
    try:
        st = os.stat(fullPath)
        size = st.st_size
        mtime = int(st.st_mtime)
        attrs = getattr(st, 'st_file_attributes', 0)
    except OSError:
        size = 0
        mtime = 0
        attrs = 0
    return {'name': name, 'path': fullPath, 'type': entryType,
            'size': size, 'mtime': mtime,
            'hidden': bool(attrs & FILE_ATTRIBUTE_HIDDEN),
            'system': bool(attrs & FILE_ATTRIBUTE_SYSTEM),
            'readonly': bool(attrs & FILE_ATTRIBUTE_READONLY)}



@blueprint.route('/api/files')
def browse():
    path = request.args.get('path') or '/'
    showHidden = request.args.get('hidden') == '1'
    if path in ('/', '\\'):
        return jsonify({'currentPath': '/', 'parentPath': None,
                        'entries': [{'name': d.rstrip('\\'), 'path': d, 'type': 'drive',
                                     'size': 0, 'mtime': 0, 'hidden': False,
                                     'system': False, 'readonly': False}
                                    for d in _drives()],
                        })
    if not os.path.isdir(path):
        return jsonify({'error': '路径不存在'}), 404
    entries = []
    try:
        names = os.listdir(path)
    except PermissionError:
        return jsonify({'error': '拒绝访问: ' + path, 'currentPath': path,
                        'parentPath': _parent(path), 'entries': []}), 403
    for name in names:
        fullPath = os.path.join(path, name)
        entryType = 'folder' if os.path.isdir(fullPath) else 'file'
        entries.append(_statEntry(fullPath, name, entryType))
    entries.sort(key=lambda e: (e['type'] != 'folder', e['name'].lower()))
    if not showHidden:
        entries = [e for e in entries if not e['hidden'] and not e['system']]
    return jsonify({'currentPath': path, 'parentPath': _parent(path),
                    'entries': entries})


@blueprint.route('/api/files/download')
def download():
    path = request.args.get('path') or ''
    ua = (request.headers.get('User-Agent') or '')[:160]
    if not path or not os.path.isfile(path):
        print('[download] miss %r ua=%s' % (path, ua), flush=True)
        return jsonify({'error': '文件不存在'}), 404
    print('[download] %s size=%d ua=%s' % (os.path.basename(path), os.path.getsize(path), ua),
          flush=True)
    # inline=1: 给 <img> 预览用原图 (缩略图放大会糊); 子资源加载不受
    # Content-Disposition 影响, 但语义上不该标成附件
    inline = request.args.get('inline') == '1'
    return send_file(path, as_attachment=not inline,
                     download_name=os.path.basename(path))


@blueprint.route('/api/files/rename', methods=['POST'])
def rename():
    data = request.json or {}
    path = data.get('path') or ''
    newName = (data.get('newName') or '').strip()
    if not path or not os.path.exists(path):
        return jsonify({'error': '路径不存在'}), 404
    if not newName or any(c in newName for c in '\\/:*?"<>|'):
        return jsonify({'error': '名称为空或含非法字符 \\ / : * ? " < > |'}), 400
    newPath = os.path.join(os.path.dirname(path), newName)
    if os.path.exists(newPath):
        return jsonify({'error': '同名条目已存在'}), 409
    try:
        os.rename(path, newPath)
        return jsonify({'ok': True, 'path': newPath})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


@blueprint.route('/api/files/new-folder', methods=['POST'])
def newFolder():
    data = request.json or {}
    parent = data.get('path') or ''
    name = (data.get('name') or '').strip()
    if not parent or not os.path.isdir(parent):
        return jsonify({'error': '父目录不存在'}), 404
    if not name or any(c in name for c in '\\/:*?"<>|'):
        return jsonify({'error': '名称为空或含非法字符'}), 400
    newPath = os.path.join(parent, name)
    if os.path.exists(newPath):
        return jsonify({'error': '同名条目已存在'}), 409
    try:
        os.mkdir(newPath)
        return jsonify({'ok': True, 'path': newPath})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


# ---- 文本预览/编辑 (FM-05/07, 档位A) ----
TEXT_READ_LIMIT = 20 * 1024 * 1024   # 文本预览/打开上限 20MB, 超限拒绝仅可下载 (FM-18)


def _sha256File(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _atomicWrite(path, payload):
    """同目录临时文件 + os.replace；失败不留半成品。"""
    parent = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.deskport-write-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _versionDenied(path, data):
    """乐观锁统一入口（本机与联邦同一套）: 版本不符 → 409，绝不写入。

    不带 baseMtime / baseSha256 时必须显式 force=true；force 只对本机开放
    （联邦请求一律要求版本，防止中枢把成员文件静默覆盖）。
    """
    rawMtime = data.get('baseMtime')
    hasMtime = rawMtime is not None and rawMtime != ''
    baseSha = str(data.get('baseSha256') or '').strip().lower()
    force = data.get('force') is True
    if not hasMtime and not baseSha:
        if not force:
            return jsonify({'error': '写文件需携带 baseMtime 或 baseSha256（并发修改保护）',
                            'hint': '重新读取后再保存；确需强制覆盖用 force=true（仅本机）'}), 400
        if request.headers.get('X-Deskport-From'):
            return jsonify({'error': '联邦写操作不允许 force，必须带版本校验'}), 403
        return None
    currentMtime = int(os.stat(path).st_mtime)
    if hasMtime:
        try:
            if isinstance(rawMtime, bool):
                raise ValueError
            wantMtime = int(str(rawMtime).strip())
        except (TypeError, ValueError):
            return jsonify({'error': 'baseMtime 必须是整数'}), 400
        if wantMtime != currentMtime:
            return jsonify({'error': '文件已在别处被修改，请重新加载后再保存',
                            'currentMtime': currentMtime}), 409
    if baseSha and baseSha != _sha256File(path):
        return jsonify({'error': '文件内容已在别处被修改，请重新加载后再保存',
                        'currentMtime': currentMtime}), 409
    return None


@blueprint.route('/api/files/federation-info')
def federationInfo():
    """成员端向中枢声明能力（不给路径）：上传上限与写根是否已配置。"""
    return jsonify({'maxUploadMB': federation_write.uploadLimitMB(),
                    'writeRootsConfigured': bool(federation_write.roots())})


@blueprint.route('/api/files/read', methods=['POST'])
def readTextFile():
    data = request.json or {}
    path = data.get('path') or ''
    if not path or not os.path.isfile(path):
        return jsonify({'error': '文件不存在'}), 404
    st = os.stat(path)
    size = st.st_size
    if size > TEXT_READ_LIMIT:
        return jsonify({'error': '文件超过 20MB，仅可下载'}), 413
    with open(path, 'rb') as f:
        raw = f.read(TEXT_READ_LIMIT + 1)
    # FM-16: 二进制也显示乱码原文 — latin-1 无损兜底; 含 NUL 视为有损, 保存禁用
    text, encoding, lossy = textio.decodeSmart(raw)
    return jsonify({'content': text, 'size': size, 'encoding': encoding,
                    'lossy': lossy, 'truncated': size > TEXT_READ_LIMIT,
                    'mtime': int(st.st_mtime),
                    'sha256': hashlib.sha256(raw).hexdigest()})


@blueprint.route('/api/files/write', methods=['POST'])
def writeTextFile():
    data = request.json or {}
    path = data.get('path') or ''
    content = data.get('content')
    if content is None or not path or not os.path.isfile(path):
        return jsonify({'error': '文件不存在'}), 404
    denied = _versionDenied(path, data)
    if denied is not None:
        return denied
    try:
        payload = textio.encodeUsing(content, data.get('encoding') or 'utf-8')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _atomicWrite(path, payload)
    return jsonify({'ok': True, 'encoding': (data.get('encoding') or 'utf-8').lower(),
                    'mtime': int(os.stat(path).st_mtime),
                    'sha256': hashlib.sha256(payload).hexdigest()})


# ---- 快速访问固定 (FM-04): 固定列表持久化在状态目录 ----
DEFAULT_PINS = None   # 首次使用时迁移为默认四项



@blueprint.route('/api/files/search')
def search():
    """文件夹内递归文件名搜索: 深度 ≤5, 结果 ≤2000。"""
    root = request.args.get('path') or ''
    q = (request.args.get('q') or '').lower()
    if not root or not os.path.isdir(root):
        return jsonify({'error': '目录不存在'}), 404
    if not q:
        return jsonify({'results': [], 'truncated': False})
    results = []
    truncated = False

    def walk(dirPath, depth):
        nonlocal truncated
        if depth > SEARCH_MAX_DEPTH or len(results) >= SEARCH_MAX_RESULTS:
            truncated = len(results) >= SEARCH_MAX_RESULTS
            return
        try:
            names = os.listdir(dirPath)
        except OSError:
            return
        for name in names:
            if len(results) >= SEARCH_MAX_RESULTS:
                truncated = True
                return
            full = os.path.join(dirPath, name)
            if q in name.lower():
                entryType = 'folder' if os.path.isdir(full) else 'file'
                entry = _statEntry(full, name, entryType)
                entry['parent'] = dirPath
                results.append(entry)
            if os.path.isdir(full):
                walk(full, depth + 1)

    walk(root, 0)
    return jsonify({'results': results, 'truncated': truncated})

