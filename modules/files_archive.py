# -*- coding: utf-8 -*-
"""压缩与解压：流式 ZIP、extract、compress。"""
import os
import queue
import shutil
import tarfile
import threading
import zipfile

from flask import Response, jsonify, request

from modules import fsutil
from modules.files_base import blueprint, log

ARCHIVE_EXTS = ('.zip', '.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar')

def isArchivePath(path):
    lower = path.lower()
    return lower.endswith(ARCHIVE_EXTS)


def _safeJoin(destDir, name):
    """归一化压缩包内路径, 拒绝 zip-slip (../ 绝对路径 盘符逃逸)。"""
    dest = os.path.abspath(os.path.join(destDir, name))
    if not fsutil.isUnder(dest, destDir):
        raise ValueError('压缩包内存在非法路径: ' + name)
    return dest


def _decodeZipName(info):
    """Windows 压缩的 ZIP 常为 GBK 文件名且无 UTF-8 标志位:
    zipfile 对无标志条目按 cp437 解码, 需回退 GBK 才能得到正确中文名。"""
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode('cp437').decode('gbk')
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def _uniqueDir(base):
    parent, name = os.path.split(base)
    return fsutil.uniquePath(parent or '.', name, isDir=True, start=2, sep='')


def _guardSpace(dest, totalNeeded):
    usage = shutil.disk_usage(dest)
    if totalNeeded > usage.free - 256 * 1024 * 1024:
        raise ValueError('目标磁盘剩余空间不足')



class _ChunkSink:
    """zipfile 的写目标: 产出字节块的有界队列 (背压式)。"""

    def __init__(self, chunkSize=256 * 1024, maxQueue=8):
        self.chunks = queue.Queue(maxsize=maxQueue)
        self.error = None
        self.closed = False

    def write(self, data):
        if self.error:
            raise self.error
        while not self.closed:
            try:
                self.chunks.put(data, timeout=0.5)
                return len(data)   # zipfile 依赖 write 返回写入字节数
            except queue.Full:
                continue
        raise ConnectionAbortedError('客户端已断开')

    def close(self):
        self.closed = True
        self.chunks.put(None)

    def flush(self):
        pass


def _buildZip(sink, paths):
    try:
        with zipfile.ZipFile(sink, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in paths:
                if os.path.isfile(path):
                    with open(path, 'rb') as src:
                        with zf.open(os.path.basename(path), 'w') as dst:
                            shutil.copyfileobj(src, dst, 256 * 1024)
                elif os.path.isdir(path):
                    baseDir = os.path.dirname(path.rstrip('\\/'))
                    for root, _dirs, files in os.walk(path):
                        for name in files:
                            full = os.path.join(root, name)
                            arc = os.path.relpath(full, baseDir).replace('\\', '/')
                            try:
                                with open(full, 'rb') as src:
                                    with zf.open(arc, 'w') as dst:
                                        shutil.copyfileobj(src, dst, 256 * 1024)
                            except OSError:
                                continue
    except Exception as e:  # noqa: BLE001 — 磁盘错误/客户端断开, 经哨兵结束流
        sink.error = e
    finally:
        sink.close()


@blueprint.route('/api/files/extract', methods=['POST'])
def extract():
    """解压 ZIP / tar.gz / tar.bz2 / tar.xz (纯标准库)。默认解到同名文件夹。"""
    data = request.json or {}
    archive = data.get('archivePath') or ''
    if not archive or not os.path.isfile(archive):
        return jsonify({'error': '压缩包不存在'}), 404
    lower = archive.lower()
    if not isArchivePath(archive):
        return jsonify({'error': '不支持的压缩格式 (支持 zip / tar.gz / tar.bz2 / tar.xz)'}), 400

    base = archive
    for ext in ARCHIVE_EXTS:
        if lower.endswith(ext):
            base = archive[:-len(ext)]
            break
    dest = _uniqueDir(base)

    try:
        os.makedirs(dest, exist_ok=True)
        extracted = 0
        if lower.endswith('.zip'):
            total = 0
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    name = _decodeZipName(info)
                    target = _safeJoin(dest, name)
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    total += info.file_size
                    _guardSpace(dest, total)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, 'wb') as f:
                        shutil.copyfileobj(src, f, 256 * 1024)
                    extracted += 1
        else:
            mode = 'r'
            if lower.endswith('.tar.gz') or lower.endswith('.tgz'):
                mode = 'r:gz'
            elif lower.endswith('.tar.bz2'):
                mode = 'r:bz2'
            elif lower.endswith('.tar.xz'):
                mode = 'r:xz'
            with tarfile.open(archive, mode) as tf:
                # Python 3.12+ data 过滤器: 天然拒绝绝对路径/.. 逃逸与特殊设备
                tf.extractall(dest, filter='data')
                extracted = len(tf.getmembers())
        return jsonify({'ok': True, 'dest': dest, 'count': extracted})
    except (ValueError, tarfile.TarError, zipfile.BadZipFile) as e:
        shutil.rmtree(dest, ignore_errors=True)   # 失败清理半解压产物
        code = 400 if isinstance(e, ValueError) else 500
        return jsonify({'error': '解压失败: ' + str(e)}), code
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return jsonify({'error': '解压失败: ' + str(e)}), 500


@blueprint.route('/api/files/compress', methods=['POST'])
def compress():
    """把多选内容压缩为 ZIP, 保存到所在目录。"""
    data = request.json or {}
    paths = [p for p in (data.get('paths') or []) if os.path.exists(p)]
    parent = data.get('parent') or (os.path.dirname(paths[0]) if paths else '')
    name = (data.get('name') or '').strip() or 'archive'
    if not paths:
        return jsonify({'error': '没有有效的目标'}), 404
    if not parent or not os.path.isdir(parent):
        return jsonify({'error': '目标目录不存在'}), 404
    if any(c in name for c in '\\/:*?"<>|'):
        return jsonify({'error': '名称含非法字符'}), 400
    if not name.lower().endswith('.zip'):
        name += '.zip'
    dest = os.path.join(parent, name)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(parent, '%s(%d).zip' % (name[:-4], n))
        n += 1
    try:
        with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in paths:
                if os.path.isfile(path):
                    zf.write(path, os.path.basename(path))
                elif os.path.isdir(path):
                    baseDir = os.path.dirname(path.rstrip('\\/'))
                    for root, _dirs, files in os.walk(path):
                        for fname in files:
                            full = os.path.join(root, fname)
                            zf.write(full, os.path.relpath(full, baseDir).replace('\\', '/'))
        return jsonify({'ok': True, 'path': dest})
    except OSError as e:
        if os.path.isfile(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        return jsonify({'error': str(e)}), 500



class StreamingZipJob:
    """有界队列的流式 ZIP 任务：路由只负责 HTTP，压缩线程由本类管理。"""

    def __init__(self, paths):
        self.paths = list(paths)
        self.sink = _ChunkSink()
        self.builder = None

    def start(self):
        self.builder = threading.Thread(target=_buildZip, args=(self.sink, self.paths),
                                        daemon=True)
        self.builder.start()

    def chunks(self):
        while True:
            chunk = self.sink.chunks.get()
            if chunk is None:
                return
            yield chunk

    def close(self):
        self.sink.closed = True
        while True:
            try:
                if self.sink.chunks.get(timeout=0.1) is None:
                    break
            except queue.Empty:
                if not self.builder or not self.builder.is_alive():
                    break
        if self.builder:
            self.builder.join()
        if self.sink.error is not None and not isinstance(self.sink.error, ConnectionAbortedError):
            log.warning('zip 流中断: %r', self.sink.error)


@blueprint.route('/api/files/zip', methods=['POST'])
def zipDownload():
    paths = (request.json or {}).get('paths') or []
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return jsonify({'error': '没有有效的目标'}), 404
    baseName = os.path.basename(paths[0].rstrip('\\/')) or 'download'
    zipName = (baseName if len(paths) == 1 else 'files') + '.zip'

    job = StreamingZipJob(paths)

    def generate():
        job.start()
        try:
            yield from job.chunks()
        finally:
            job.close()

    headers = {'Content-Disposition': "attachment; filename*=UTF-8''" + zipName,
               'X-Content-Type-Options': 'nosniff'}
    return Response(generate(), mimetype='application/zip', headers=headers)
