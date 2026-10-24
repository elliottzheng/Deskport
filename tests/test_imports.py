# -*- coding: utf-8 -*-
"""导入冒烟：模块拆分后最容易漏的是模块级未定义名。"""
import importlib

MODULES = [
    'modules.system', 'modules.files', 'modules.command', 'modules.screen',
    'modules.aihandoff', 'modules.control', 'modules.ssh', 'modules.tasks',
    'modules.terminal', 'modules.sessionproxy', 'modules.decision',
    'modules.kimi_acp', 'modules.tasks_runner', 'modules.tasks_api',
]


def test_import_all():
    for name in MODULES:
        importlib.import_module(name)
