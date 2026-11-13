# -*- coding: utf-8 -*-
"""公开工具箱 — `GET /api/agent/tools`（无需令牌）。

接入手册教的是裸 HTTP，但读者（还没令牌的智能体）要自己重写轮询/审批/任务
管理。这里把仓库里现成的三个工具公开出来，一条 URL 即取：

    GET /api/agent/tools            → {"tools": [{"name", "bytes", "sha256"}]}
    GET /api/agent/tools/<name>     → 原文件（.md 给 text/markdown，其余 text/plain）
                                      响应头 X-Tools-Sha256 与清单一致

白名单只有三个：`deskport.py`（零依赖单文件客户端）、`driver.py`（主控派活
模板）、`COLLABORATION.md`（主控与执行方的分工/验收）。**SKILL.md 不放进来**：
它是 Kimi 技能格式，给的是另一条自举通道（/api/agent/skills，需令牌），公开
它只会把两种格式混在一起。

文件仍以 `.agents/skills/deskport/` 为唯一来源，本模块只读不复制；三个文件都
不含凭据（令牌/密码一律走环境变量或同目录 .agent-token，由 tests 正则把关）。

开关与手册共用 `config.handbookEnabled`：工具箱的入口写在手册里，手册关掉时
这些地址失去了被发现路径，再开一个独立开关只会造出「手册关了、工具还公开」
的半开状态；显式 false 时两个端点一律 404。

白名单外一律 404（不列目录里还有什么）：SKILL.md、`..`、子目录、不存在
的名字都不区分，避免把目录结构探给未认证调用方。
"""
import hashlib
import os

from flask import Blueprint, Response, abort, jsonify

import config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(BASE_DIR, '.agents', 'skills', 'deskport')
# 公开白名单（顺序即清单顺序）。新增公开工具先过 tests/test_agent_tools.py
# 的凭据正则：token=<长随机串> / password / agentTokens 命中即失败。
TOOLS = ('deskport.py', 'driver.py', 'COLLABORATION.md')

blueprint = Blueprint('tools', __name__)


def _toolEntry(name):
    """清单项 {name, bytes, sha256}；按字节算，与下载后校验一致。"""
    path = os.path.join(TOOLS_DIR, name)
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        blob = f.read()
    return {'name': name, 'bytes': len(blob),
            'sha256': hashlib.sha256(blob).hexdigest()}


@blueprint.route('/api/agent/tools')
def agentToolsList():
    """工具箱清单：无令牌可读，供智能体决定下载哪个。"""
    if not config.isHandbookEnabled():
        abort(404)
    tools = [e for e in (_toolEntry(name) for name in TOOLS) if e]
    return jsonify({'tools': tools})


@blueprint.route('/api/agent/tools/<name>')
def agentToolGet(name):
    """工具原文；`curl -o` 直接落盘，X-Tools-Sha256 供完整性校验。"""
    if not config.isHandbookEnabled():
        abort(404)
    if name not in TOOLS:          # 白名单外不解释、不列目录
        abort(404)
    entry = _toolEntry(name)
    if entry is None:
        abort(404)
    with open(os.path.join(TOOLS_DIR, name), 'rb') as f:
        blob = f.read()
    mimetype = 'text/markdown' if name.endswith('.md') else 'text/plain'
    resp = Response(blob, mimetype=mimetype)
    resp.headers['Content-Type'] = mimetype + '; charset=utf-8'
    resp.headers['X-Tools-Sha256'] = entry['sha256']
    resp.headers['Cache-Control'] = 'no-cache'
    return resp
