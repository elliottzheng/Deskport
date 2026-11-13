# -*- coding: utf-8 -*-
"""SSH SFTP 模块入口：装配 base / ops / transfer。

拆分记录（2026-11-05）：原 503 行混了"远程文件 CRUD"与"批量传输打包"两个
变化理由，且共用工具散落其中。现为:
    modules/ssh_sftp_base.py      蓝图/错误/通道会话/条目转换/逐级建目录/重名
    modules/ssh_sftp_ops.py       列表/建目录/重命名/删除/权限/读/写 (7 路由)
    modules/ssh_sftp_transfer.py  下载/打包/上传(三种)/本机互传 (7 路由)

本文件只做 import 装配，不写业务；路由由三个子模块在 import 时注册。
测试应直接 patch 子模块（如 ssh_sftp_base._sftpSession），patch 本文件的
再导出名不会影响子模块内的绑定。
"""
from modules import ssh_sftp_base      # noqa: F401  注册路由
from modules import ssh_sftp_ops       # noqa: F401  注册路由
from modules import ssh_sftp_transfer  # noqa: F401  注册路由
from modules.ssh_sftp_base import _openSftp          # noqa: F401  再导出
from modules.ssh_sftp_base import _sftpSession       # noqa: F401  再导出
from modules.ssh_sftp_base import ensureRemoteDir  # noqa: F401
from modules.ssh_sftp_transfer import ZIP_SIZE_BUDGET       # noqa: F401  再导出
from modules.ssh_sftp_transfer import ZipBudgetExceeded     # noqa: F401  再导出
