# -*- coding: utf-8 -*-
"""任务智能体入口（兼容壳）。

实现已按职责拆分：
- tasks_store.py   状态/持久化/事件
- tasks_runner.py  ACP 会话与轮次编排
- tasks_api.py     /api/tasks 路由
"""
from modules.tasks_api import blueprint, register  # noqa: F401

__all__ = ['blueprint', 'register']