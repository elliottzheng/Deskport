# -*- coding: utf-8 -*-
"""文件操作：回收站/删除/粘贴/启动/上传/远程下载。"""
import ctypes
import os
import re
import secrets
import shutil
from ctypes import wintypes

import requests
import urllib3
from flask import jsonify, request

from modules import federation_write, fsutil
from modules.files_base import blueprint

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FO_DELETE = 3
FOF_ALLOWUNDO = 0x40
FOF_NOCONFIRMATION = 0x10
FOF_SILENT = 0x4
FOF_NOERRORUI = 0x400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def recyclePaths(paths):
    """移入回收站。paths 必须为绝对路径。返回 (ok, aborted)。

    注意 pFrom 是双 NUL 结尾的多串, 必须用 create_unicode_buffer 而非
    c_wchar_p 直接赋值 (后者会在第一个 \\0 截断, 只删第一个)。
    """
    if not paths:
        return True, False
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0")
    ops = SHFILEOPSTRUCTW()
    ops.hwnd = None
    ops.wFunc = FO_DELETE
    ops.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    ops.pTo = None
    ops.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(ops))
    return result == 0 and not ops.fAnyOperationsAborted, bool(ops.fAnyOperationsAborted)



def _uniqueDest(destDir, name):
    return fsutil.uniquePath(destDir, name)


MAX_UPLOAD_NAME = 120
_BAD_UPLOAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safeUploadName(name):
    """上传只接受纯文件名：去掉客户端带来的目录部分，其余非法字符拒绝。"""
    text = str(name or '').replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not text or len(text) > MAX_UPLOAD_NAME or _BAD_UPLOAD_CHARS.search(text) \
            or text in ('.', '..'):
        return ''
    return text


def _saveUpload(file, saveDir, limitBytes=None):
    """流式落盘: <name>.part-<rand> → 原子 rename；失败清理，同名 (2) 不覆盖。"""
    name = _safeUploadName(file.filename)
    if not name:
        return None, ('文件名不合法（需纯文件名，≤120 字符）', 400)
    part = os.path.join(saveDir, '%s.part-%s' % (name, secrets.token_hex(4)))
    try:
        size = 0
        with open(part, 'wb') as out:
            while True:
                chunk = file.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if limitBytes is not None and size > limitBytes:
                    return None, ('超过单文件上限 %dMB'
                                  % (limitBytes // (1024 * 1024)), 413)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        for _ in range(20):
            target = fsutil.uniquePath(saveDir, name, start=2, sep='')
            try:
                os.rename(part, target)   # Windows: 目标存在时失败，不会覆盖
                return target, None
            except FileExistsError:
                continue
        return None, ('同名条目过多: %s' % name, 409)
    except OSError as e:
        return None, ('写盘失败: %s' % e, 500)
    finally:
        if os.path.isfile(part):
            try:
                os.remove(part)
            except OSError:
                pass


@blueprint.route('/api/files/paste', methods=['POST'])
def pasteEntries():
    data = request.json or {}
    mode = data.get('mode')            # 'copy' | 'move'
    paths = [p for p in (data.get('paths') or []) if p and os.path.exists(p)]
    dest = data.get('dest') or ''
    if mode not in ('copy', 'move'):
        return jsonify({'error': 'mode 应为 copy/move'}), 400
    if not paths:
        return jsonify({'error': '没有有效的目标'}), 404
    if not dest or not os.path.isdir(dest):
        return jsonify({'error': '目标目录不存在'}), 404
    done, errors = [], []
    for p in paths:
        name = os.path.basename(p.rstrip('\\/'))
        if os.path.abspath(os.path.dirname(p.rstrip('\\/'))) == os.path.abspath(dest) and mode == 'move':
            continue   # 移动到原位置: 跳过
        target = _uniqueDest(dest, name)
        try:
            if mode == 'copy':
                if os.path.isdir(p):
                    shutil.copytree(p, target)
                else:
                    shutil.copy2(p, target)
            else:
                shutil.move(p, target)
            done.append(target)
        except (OSError, shutil.Error) as e:
            errors.append(f"{name}: {e}")
    result = {'ok': True, 'done': len(done)}
    if errors:
        result['errors'] = errors
    return jsonify(result), (200 if done and not errors else 207 if done else 500)



@blueprint.route('/api/files/recycle-delete', methods=['POST'])
def recycleDelete():
    paths = (request.json or {}).get('paths') or []
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return jsonify({'error': '没有有效的目标'}), 404
    ok, aborted = recyclePaths(paths)
    if aborted:
        return jsonify({'error': '操作被取消'}), 400
    if not ok:
        return jsonify({'error': '移入回收站失败 (目标可能被占用)'}), 500
    return jsonify({'ok': True, 'count': len(paths)})


@blueprint.route('/api/files/delete', methods=['POST'])
def delete():
    """永久删除 (显式 API; UI 默认走回收站)。"""
    paths = (request.json or {}).get('paths') or []
    if isinstance(paths, str):
        paths = [paths]
    deleted, failed = 0, []
    for path in paths:
        if not path or not os.path.isfile(path):
            failed.append(path or '?')
            continue
        try:
            os.remove(path)
            deleted += 1
        except OSError as e:
            failed.append(os.path.basename(path) + ': ' + str(e))
    if failed:
        return jsonify({'error': '；'.join(failed), 'deleted': deleted}), 500
    return jsonify({'ok': True, 'deleted': deleted})


@blueprint.route('/api/files/start', methods=['POST'])
def start():
    path = (request.json or {}).get('path') or ''
    if not path or not os.path.exists(path):
        return jsonify({'error': '路径不存在'}), 404
    try:
        os.startfile(path)  # noqa: S606
        return jsonify({'ok': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


@blueprint.route('/api/files/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    saveDir = request.form.get('path') or ''
    tempdir = request.form.get('tempdir') == 'true'
    if tempdir:
        import tempfile
        saveDir = tempfile.gettempdir()
    federated = bool(request.headers.get('X-Deskport-From'))
    if not file or file.filename == '':
        return jsonify({'error': '未选择文件'}), 400
    # 「此电脑」根视图 (path='/') 没有可保存的目录; 相对路径会落到守护进程的
    # cwd (曾表现为 500 No such file / 写在 Git 安装目录里), 一律明确拒绝
    if not saveDir or saveDir in ('/', '.'):
        return jsonify({'error': '无效的保存目录'}), 400
    if not os.path.isabs(saveDir):
        return jsonify({'error': '保存目录必须是绝对路径: %s' % saveDir}), 400
    if not os.path.isdir(saveDir):
        return jsonify({'error': '保存目录不存在: %s' % saveDir}), 400
    if federated:
        # 入口钩子已做过路径校验；这里再用最终文件名复核一次（文件名可含 ..
        if tempdir:
            return jsonify({'error': '联邦上传不允许 tempdir'}), 403
        if request.form.get('execute') == 'true':
            return jsonify({'error': '联邦上传不允许 execute（成员端不启动文件）'}), 403
        name = _safeUploadName(file.filename)
        if not name:
            return jsonify({'error': '文件名不合法（需纯文件名，≤120 字符）'}), 400
        ok, why = federation_write.checkPaths([os.path.join(saveDir, name)])
        if not ok:
            return jsonify({'error': '联邦写操作被拒绝: ' + why}), 403
    limitBytes = None
    if federated:
        limitBytes = federation_write.uploadLimitMB() * 1024 * 1024
    target, failure = _saveUpload(file, saveDir, limitBytes)
    if failure:
        return jsonify({'error': failure[0]}), failure[1]
    if request.form.get('execute') == 'true':
        try:
            os.startfile(target)  # noqa: S606
        except OSError as e:
            return jsonify({'ok': True, 'filename': os.path.basename(target),
                            'warn': '已保存但启动失败: ' + str(e)})
    return jsonify({'ok': True, 'filename': os.path.basename(target), 'path': target})


@blueprint.route('/api/files/remote-download', methods=['POST'])
def remoteDownload():
    data = request.json or {}
    url = data.get('url') or ''
    saveDir = data.get('path') or ''
    if not url:
        return jsonify({'error': '缺少 URL'}), 400
    if not saveDir or saveDir == '/':
        return jsonify({'error': '无效的保存目录'}), 400
    filename = os.path.basename(url.split('?')[0].rstrip('/')) or 'download.bin'
    savePath = os.path.join(saveDir, filename)
    try:
        with requests.get(url, stream=True, timeout=30, verify=False,
                          proxies={'http': None, 'https': None}) as resp:
            resp.raise_for_status()
            with open(savePath, 'wb') as f:
                for chunk in resp.iter_content(64 * 1024):
                    f.write(chunk)
    except requests.exceptions.RequestException as e:
        if os.path.isfile(savePath):
            try:
                os.remove(savePath)
            except OSError:
                pass
        return jsonify({'error': '下载失败: ' + str(e)}), 502
    if data.get('execute') == 'true':
        try:
            os.startfile(savePath)  # noqa: S606
        except OSError:
            pass
    return jsonify({'ok': True, 'filename': filename, 'path': savePath})


# ---- 多选打包下载: 有界队列真流式 (内存恒定, 客户端断开自动终止压缩线程) ----