# -*- coding: utf-8 -*-
"""从 ACP permission 请求的 toolCall 抽文件路径（Edit/Write 审批判定用）。

实测（kimi 2.5.3，2026-11-13）：`session/request_permission` 的 toolCall
**没有 rawInput，也没有 locations**；目标路径在 `content[]` 的 diff 块里：

    {"type": "diff",
     "path": "E:/.../note.txt",
     "oldText": "...", "newText": "..."}

ACP 规范里的 `locations[].path` 一并收下。只认结构化 path 字段，不去猜
title / 正文里的字符串；多个路径去重保序返回。
"""


def permissionPaths(toolCall):
    paths = []

    def add(value):
        if isinstance(value, str):
            text = value.strip()
            if text and text not in paths:
                paths.append(text)

    if not isinstance(toolCall, dict):
        return paths
    for block in toolCall.get('content') or []:
        if isinstance(block, dict):
            add(block.get('path'))
    for loc in toolCall.get('locations') or []:
        if isinstance(loc, dict):
            add(loc.get('path'))
    return paths