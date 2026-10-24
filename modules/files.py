# -*- coding: utf-8 -*-
"""文件模块入口：装配 browse/transfer/archive/pins。"""
from modules.files_base import blueprint
from modules import files_browse, files_transfer, files_archive, files_pins  # noqa: F401
from modules.files_pins import thumb

__all__ = ['blueprint', 'thumb']
