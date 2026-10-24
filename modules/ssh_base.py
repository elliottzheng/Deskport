# -*- coding: utf-8 -*-
"""SSH 共享契约：blueprint、异常、超时常量。"""
import logging

from flask import Blueprint

blueprint = Blueprint('ssh', __name__)
log = logging.getLogger('ssh')

CONNECT_TIMEOUT = 15
AUTH_TIMEOUT = 120


class SftpAuthError(Exception):
    """SFTP 连接的 SSH 认证失败 (HTTP 层转 401)"""


class HostKeyChangedError(Exception):
    """主机密钥与已存指纹不符 (可能中间人), 拒绝连接"""
