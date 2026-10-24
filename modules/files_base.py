# -*- coding: utf-8 -*-
"""文件模块共享：blueprint 与属性位常量。"""
import logging

from flask import Blueprint

blueprint = Blueprint('files', __name__)
log = logging.getLogger('files')

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_READONLY = 0x1
