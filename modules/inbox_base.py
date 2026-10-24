# -*- coding: utf-8 -*-
"""收件箱/AI 交接共享：blueprint 与日志。"""
import logging

from flask import Blueprint

blueprint = Blueprint('aihandoff', __name__)
log = logging.getLogger('aihandoff')
