# -*- coding: utf-8 -*-
"""交给 AI：落点判断、案例目录、任务.txt 与直接开终端。"""
import datetime
import os
import re
import shutil
import stat

from flask import jsonify, request

import config
from modules import decision, fsutil
from modules.inbox_base import blueprint, log
from modules.inbox_scan import HASH_LIMIT, _under, _sha256, _dedupe, canonicalName  # noqa: F401
import modules.inbox_scan  # noqa: F401  注册 /api/inbox 路由

KICK_TEXT = '请先阅读本目录的 任务.txt，然后按里面的说明处理目录中的文件。'


def _aiCommand():
    """AI CLI 启动命令：配置优先，其次探测常见 CLI，最后 PowerShell。"""
    return (config.getAiCommand() or (shutil.which('omp') and 'omp')
            or (shutil.which('kimi') and 'kimi')
            or ('pwsh.exe' if shutil.which('pwsh.exe') else 'powershell.exe'))

@blueprint.route('/api/ai/config')
def aiConfig():
    detected = [name for name in ('omp', 'kimi', 'claude', 'codex')
                if shutil.which(name)]
    return jsonify({'workDirs': config.getWorkDirs(),
                    'fallbackDir': config.getFallbackDir(),
                    'aiCommand': config.getAiCommand(),
                    'detectedCommands': detected,
                    'inboxDirs': config.getInboxDirs(),
                    'kick': KICK_TEXT})


@blueprint.route('/api/ai/plan', methods=['POST'])
def aiPlan():
    data = request.get_json(force=True, silent=True) or {}
    task = (data.get('task') or '').strip()
    files = data.get('files') or []
    if not task:
        return jsonify({'error': '任务名不能为空'}), 400
    if not isinstance(files, list):
        return jsonify({'error': 'files 必须是数组'}), 400
    return jsonify(decision.plan(task, files))



def _safeName(text, fallback='未命名任务'):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text or '').strip(' .')
    text = re.sub(r'\s+', ' ', text)
    return (text[:40] or fallback)


def _uniqueDir(parent, name):
    return fsutil.uniquePath(parent, name, isDir=True, start=2)


def _uniqueFile(path):
    parent, name = os.path.split(path)
    return fsutil.uniquePath(parent or '.', name), True


def _sameContent(src, dst):
    try:
        if os.path.getsize(src) != os.path.getsize(dst):
            return False
        if os.path.getsize(src) > HASH_LIMIT:
            return False
        return _sha256(src) == _sha256(dst)
    except OSError:
        return False


def _entrySize(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _copyFile(src, target):
    """复制单文件; 同名同内容跳过, 不同则加序号。返回 True=复制 False=跳过。"""
    if os.path.exists(target):
        if os.path.isfile(target) and _sameContent(src, target):
            try:
                os.chmod(target, os.stat(target).st_mode | stat.S_IWRITE)
            except OSError:
                pass
            return False
        target, _ = _uniqueFile(target)
    shutil.copy2(src, target)
    os.chmod(target, os.stat(target).st_mode | stat.S_IWRITE)
    return True


def _copyEntry(src, caseDir):
    """复制文件或目录到案例目录。返回 (copied, skipped, displayName)。"""
    base = os.path.basename(src.rstrip('\\/'))
    if os.path.isfile(src):
        copied = 1 if _copyFile(src, os.path.join(caseDir, base)) else 0
        return copied, (0 if copied else 1), base
    root = os.path.join(caseDir, base)
    if os.path.exists(root) and not os.path.isdir(root):
        root = _uniqueDir(caseDir, base)
    os.makedirs(root, exist_ok=True)
    copied = skipped = 0
    for dirpath, _dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        destDir = root if rel == '.' else os.path.join(root, rel)
        os.makedirs(destDir, exist_ok=True)
        for name in files:
            srcFile = os.path.join(dirpath, name)
            try:
                if _copyFile(srcFile, os.path.join(destDir, name)):
                    copied += 1
                else:
                    skipped += 1
            except OSError as e:
                log.warning('copy failed %s: %s', srcFile, e)
                raise
    return copied, skipped, base + os.sep



@blueprint.route('/api/ai/direct', methods=['POST'])
def aiDirect():
    """单文件夹问 AI：直接用该文件夹作为 cwd，不复制、不写任务.txt。

    kick 是用户在对话框里输入的提示词（可为空=只开交互式 CLI）。
    """
    data = request.get_json(force=True, silent=True) or {}
    raw = (data.get('path') or '').strip()
    task = (data.get('task') or '').strip()
    note = (data.get('note') or '').strip()
    if not raw:
        return jsonify({'error': '缺少目录路径'}), 400
    path = os.path.abspath(raw)
    if not re.match(r'^[A-Za-z]:[\\/]', path):
        return jsonify({'error': '只允许本机磁盘路径: %s' % raw}), 403
    if not os.path.isdir(path):
        return jsonify({'error': '目录不存在: %s' % path}), 400
    kick = task
    if note:
        kick = (kick + '；' + note) if kick else note
    log.info('ai direct dir=%s kick=%s', path, bool(kick))
    return jsonify({'ok': True, 'dir': path,
                    'terminal': {'cwd': path, 'cmd': _aiCommand(), 'kick': kick}})


@blueprint.route('/api/ai/case', methods=['POST'])
def aiCase():
    data = request.get_json(force=True, silent=True) or {}
    task = (data.get('task') or '').strip()
    files = data.get('files') or []
    mode = data.get('mode') or 'new'
    dest = (data.get('dest') or '').strip()
    note = (data.get('note') or '').strip()

    if not task:
        return jsonify({'error': '任务名不能为空'}), 400
    if not files:
        return jsonify({'error': '至少选择一个文件'}), 400
    if mode not in ('new', 'existing'):
        return jsonify({'error': 'mode 只能是 new / existing'}), 400

    srcs = []
    totalSize = 0
    for path in files:
        if not isinstance(path, str) or not os.path.exists(path):
            return jsonify({'error': '文件不存在: %s' % path}), 400
        norm = os.path.abspath(path)
        if not re.match(r'^[A-Za-z]:[\\/]', norm):
            return jsonify({'error': '只允许本机磁盘路径: %s' % path}), 403
        totalSize += _entrySize(path)
        srcs.append(norm)
    if totalSize > 1024 * 1024 * 1024:
        return jsonify({'error': '选中内容超过 1GB，请分开处理'}), 413

    allowed = config.getWorkDirs() + [config.getFallbackDir()]
    if not dest or not _under(dest, allowed):
        return jsonify({'error': '落点必须在常用工作目录内: %s' % dest}), 403
    if not os.path.isdir(dest):
        return jsonify({'error': '落点目录不存在: %s' % dest}), 400

    if mode == 'new':
        name = '%s %s' % (datetime.date.today().isoformat(), _safeName(task))
        caseDir = _uniqueDir(dest, name)
        try:
            os.makedirs(caseDir, exist_ok=False)
        except OSError as e:
            return jsonify({'error': '创建目录失败: %s' % e}), 500
    else:
        caseDir = dest

    copied, skipped, copiedNames = 0, 0, []
    for src in srcs:
        try:
            c, s, name = _copyEntry(src, caseDir)
        except OSError as e:
            log.warning('copy failed %s: %s', src, e)
            return jsonify({'error': '复制失败: %s (%s)' % (os.path.basename(src), e)}), 500
        copied += c
        skipped += s
        copiedNames.append(name)

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = ['任务：' + task, '创建时间：' + now, '文件数量：%d' % len(copiedNames), '',
             '文件清单：']
    lines += ['%d. %s' % (i + 1, n) for i, n in enumerate(copiedNames)]
    lines += ['', '补充要求：', note or '（无）', '',
              '说明：以上文件已复制到本目录，原始文件未被修改。']
    taskFile = os.path.join(caseDir, '任务.txt')
    try:
        with open(taskFile, 'w', encoding='utf-8-sig') as f:
            f.write('\n'.join(lines) + '\n')
    except OSError as e:
        return jsonify({'error': '写入任务.txt 失败: %s' % e}), 500

    aiCommand = _aiCommand()
    log.info('case created dir=%s copied=%d skipped=%d', caseDir, copied, skipped)
    return jsonify({'ok': True, 'dir': caseDir, 'copied': copied, 'skipped': skipped,
                    'files': copiedNames, 'taskFile': taskFile,
                    'terminal': {'cwd': caseDir, 'cmd': aiCommand, 'kick': KICK_TEXT}})