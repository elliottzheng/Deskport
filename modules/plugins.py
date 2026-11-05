# -*- coding: utf-8 -*-
"""插件系统入口：装配 discover / load / nav / base，并保留对外兼容名。

拆分记录（2026-11-05）：原 439 行混了目录扫描/清单解析、动态导入与注册、
导航与标签推导、登记查询四个变化理由。现为:
    modules/plugins_base.py      登记表/错误/蓝图/版本与 id 常量
    modules/plugins_discover.py  根目录/plugin.json 校验/entry 安全
    modules/plugins_load.py      动态 import/蓝图注册/自动路由/覆盖/登记
    modules/plugins_nav.py       legacy 标签推导/原生 nav/openWith

本文件只做 import 装配，不写业务；路由由 plugins_base 注册。
"""
from modules.plugins_base import (          # noqa: F401  再导出
    API_VERSION, DEFAULT_ICON, blueprint, getNavItems, getRegistry,
    isPluginApiPath,
)
from modules.plugins_discover import (      # noqa: F401  再导出
    defaultNavDir, discover, getRoots,
)
from modules.plugins_load import (          # noqa: F401  再导出
    applyOverrides, autoRoute, initSocks, load, loadOne,
)
from modules.plugins_nav import (           # noqa: F401  再导出
    legacyLabel, nativeNav, openWith, templateTitle,
)
