# -*- coding: utf-8 -*-
"""公开接入手册 — `GET /api/agent/handbook`（无需令牌）。

外部智能体拿到一条 URL 就能读到用法，令牌由部署者另外发放。正文在
`docs/agent-handbook.md`，运行时只做两处替换：`{{BASE}}` 换成本次请求的
地址（scheme://host[:port]），`{{VERSION}}` 换成本次构建号。

公开端点，正文不得含密钥、令牌明文、主机名清单或本机路径——只讲义端点
形状与用法。`config.handbookEnabled=false` 时整条路由 404（手册地址本身
也变成不可用）；`?format=json` 返回 `{version, base, markdown}` 供程序解析。
"""
import os

from flask import Blueprint, Response, abort, jsonify, request

import config
from modules import meta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(BASE_DIR, 'docs', 'agent-handbook.md')

blueprint = Blueprint('handbook', __name__)


def render(template, base, version):
    """替换两个占位符；其它内容原样保留。"""
    return template.replace('{{BASE}}', base).replace('{{VERSION}}', version)


def loadTemplate():
    with open(TEMPLATE_PATH, encoding='utf-8') as f:
        return f.read()


def baseUrl():
    """本次请求的 API base：含 scheme/host/port，无尾斜杠。"""
    return request.host_url.rstrip('/')


@blueprint.route('/api/agent/handbook')
def agentHandbook():
    if not config.isHandbookEnabled():
        abort(404)
    text = render(loadTemplate(), baseUrl(), meta.BUILD)
    if request.args.get('format') == 'json':
        return jsonify({'version': meta.BUILD, 'base': baseUrl(), 'markdown': text})
    resp = Response(text, mimetype='text/markdown')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp