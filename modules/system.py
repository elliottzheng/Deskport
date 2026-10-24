# -*- coding: utf-8 -*-
"""系统信息模块 — GET /api/system"""
import platform
import time

import psutil
from flask import jsonify

BOOT_TIME = psutil.boot_time()


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
