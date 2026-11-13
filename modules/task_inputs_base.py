# -*- coding: utf-8 -*-
"""跨机附件投递的共用小件：错误类型、远端目录名、文件名校验。

只放两种通道（task_inputs_sftp / task_inputs_fed）都要用的东西，
避免它们互相 import（公共 API 在 task_inputs）。
"""
from modules import staging

INPUTS_DIR = '.deskport-inputs'
MAX_FILE_MB = 32          # 与 tasks_store.MAX_UPLOAD_MB 同口径（单个附件）
MAX_TOTAL_MB = 500        # 与 tasks_store.MAX_TASK_MB 同口径（本任务全部输入）
MAX_FILES = 3000          # 与 tasks_store.MAX_TASK_FILES 同口径


class DeliverError(Exception):
    """投递失败；status 直接作为 HTTP 状态码返回给调用方。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def safeName(name):
    """与暂存区同一套文件名规则（纯文件名、≤120）。"""
    return staging._safeAttachmentName(name)


def memberError(status, data, what):
    """联邦成员的非 200 → DeliverError；403 明确指出是根白名单这一层。"""
    detail = str((data or {}).get('error') or '').strip() or 'HTTP %s' % status
    if status == 403:
        return DeliverError('执行点拒绝联邦写入（成员端 federationRoots）: %s'
                            % detail, 403)
    if status == 404:
        return DeliverError('执行点上的目录不存在（先确认 cwd）: %s' % detail, 400)
    return DeliverError('执行点写入失败: %s' % detail, 502)
