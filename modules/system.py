# -*- coding: utf-8 -*-
"""系统信息模块 — GET /api/system"""
import platform
import time

import psutil
from flask import jsonify, request

BOOT_TIME = psutil.boot_time()

_LOOPBACK = ('127.0.0.1', '::1', '::ffff:127.0.0.1')


def _ipList():
    """网卡与 IPv4 列表, 链路本地地址(169.*)标记为次要"""
    result = []
    for name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family.name == 'AF_INET':
                result.append({'name': name, 'ip': addr.address,
                               'linkLocal': addr.address.startswith('169')})
    return result


def register(app):
    @app.route('/api/system')
    def systemInfo():
        mem = psutil.virtual_memory()
        return jsonify({
            'pcName': platform.node(),
            'user': psutil.users()[-1].name if psutil.users() else '-',
            'os': platform.system() + ' ' + platform.release(),
            'python': platform.python_version(),
            'memory': {
                'percent': mem.percent,
                'used': mem.used,
                'total': mem.total,
                'available': mem.available,
            },
            'uptimeSeconds': int(time.time() - BOOT_TIME),
            'interfaces': _ipList(),
        })

    @app.route('/api/system/drain', methods=['POST'])
    def systemDrain():
        """部署排空（批次 DR）：deploy.ps1 同步代码前调用——中枢停止接受
        新的一轮开始（新消息/新任务照常排队），等在飞回合自然结束或超时；
        超时也放行，仍在跑的标记 pausedBy='restart' 交批次 L3 续接。

        只认网页会话或本机回环地址（端点自检，见 auth.EXEMPT_PREFIXES）。
        timeoutSec 缺省 300；0 = 探针（只报在飞回合，不进入排空）。"""
        import auth
        from modules import task_drain
        if (request.remote_addr or '') not in _LOOPBACK \
                and not auth.isAuthenticated():
            return jsonify({'error': 'drain 只允许网页会话或本机回环地址调用'}), 403
        data = request.get_json(force=True, silent=True) or {}
        report = task_drain.runDrain(data.get('timeoutSec'))
        if 'error' in report:
            return jsonify(report), 409
        return jsonify(report)
