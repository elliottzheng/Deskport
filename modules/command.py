# -*- coding: utf-8 -*-
"""命令执行模块 — POST /api/command"""
import subprocess

from flask import Blueprint, jsonify, request

blueprint = Blueprint('command', __name__)

TIMEOUT_SECONDS = 120


def _decode(data):
    """子进程输出编码: 系统可能启用 UTF-8 代码页, 也可能是 GBK, 依次尝试"""
    for encoding in ('utf-8', 'gbk'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')


@blueprint.route('/api/command', methods=['POST'])
def execute():
    data = request.json or {}
    cmd = (data.get('cmd') or '').strip()
    if not cmd:
        return jsonify({'error': '命令不能为空'}), 400

    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # 杀整个进程树: 直接 kill cmd.exe 不会终止孙进程, 管道会被占住挂死
        subprocess.run('taskkill /F /T /PID ' + str(proc.pid),
                       capture_output=True)
        stdout, stderr = proc.communicate()
        return jsonify({
            'error': '执行超时 (' + str(TIMEOUT_SECONDS) + 's), 已终止进程树',
            'exitCode': None,
            'stdout': _decode(stdout),
            'stderr': _decode(stderr),
        }), 504

    return jsonify({
        'exitCode': proc.returncode,
        'stdout': _decode(stdout),
        'stderr': _decode(stderr),
    })
