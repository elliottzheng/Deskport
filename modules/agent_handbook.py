# -*- coding: utf-8 -*-
"""公开接入手册 — `GET /api/agent/handbook`（无需令牌）。

外部智能体拿到一条 URL 就能读到用法，令牌由部署者另外发放。正文在
`docs/agent-handbook.md`，运行时只做三处替换：`{{BASE}}` 换成本次请求的
地址（scheme://host[:port]）、`{{VERSION}}` 换成本次构建号、`{{LAN_LINE}}`
换成「局域网直连」整行（配了 `lanAddress` 才有，没配整行删掉）。

scheme 的坑（批次 I 实测）：frp/反代终结 TLS 后进程看到的 `wsgi.url_scheme`
永远是 `http`，手册与 `.deskport.json` 会印出 `http://dx.frp-leg.com:40963`
这种根本不通的地址（隧道对明文请求返 501）。`trustProxyProto`（缺省 false）
打开时采信上游的 `X-Forwarded-Proto`（仅 http/https）修正对外印出的协议
前缀；`X-Forwarded-Host`/`For` 一律不采信——Host 头可伪造，采信它等于让
调用方决定手册印什么地址。

公开端点，正文不得含密钥、令牌明文、主机名清单或本机路径——只讲义端点
形状与用法。`config.handbookEnabled=false` 时整条路由 404（手册地址本身
也变成不可用）；`?format=json` 返回 `{version, base, markdown}` 供程序解析。
"""
import os
import re

from flask import Blueprint, Response, abort, jsonify, request

import config
from modules import meta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(BASE_DIR, 'docs', 'agent-handbook.md')

blueprint = Blueprint('handbook', __name__)

_TRUSTED_PROTO = ('http', 'https')
# 局域网直连地址：host[:port]，容忍误带 http:// 前缀；回环/通配不算「局域网」
_LAN_RE = re.compile(r'^(?:http://)?([A-Za-z0-9._-]{1,255})(?::(\d{1,5}))?$')
_LAN_NOT_LAN = ('localhost', '127.0.0.1', '::1', '0.0.0.0')


def isTrustProxyProto():
    """部署者是否显式打开「采信上游 X-Forwarded-Proto」；缺省 false。

    只有 Deskport 确实挂在反向代理/隧道之后（TLS 在代理端终结、进程看到
    的 scheme 永远是 http）时才开；直连/联邦/内网场景该头可由客户端任意
    伪造，缺省必须关。采信它只影响对外印出的协议前缀，不放开任何权限。
    """
    return config.load().get('trustProxyProto') is True


def lanDirect():
    """局域网直连 base（http://<lanAddress>[:port]）；未配置/非法 = 空串。

    部署者在 config.json 显式填 `lanAddress`（"192.168.1.23" 或
    "192.168.1.23:6789"）才印「局域网直连」一行；缺省省略该行。不自动
    探测网卡——多网卡/VPN/容器环境下猜的地址不可靠，印错比不印更糟。
    """
    text = str(config.load().get('lanAddress') or '').strip()
    m = _LAN_RE.match(text)
    if not m or m.group(1).lower() in _LAN_NOT_LAN:
        return ''
    if m.group(2) is None:
        port = config.getPort()
    else:
        port = int(m.group(2))
        if not 1 <= port <= 65535:
            return ''
    return 'http://%s:%d' % (m.group(1), port)


def render(template, base, version, lanDirect=''):
    """替换占位符；`{{LAN_LINE}}` 有局域网地址就换成整行，没有则整行删掉。"""
    text = template.replace('{{BASE}}', base).replace('{{VERSION}}', version)
    if lanDirect:
        return text.replace('{{LAN_LINE}}',
                            '- 局域网直连：%s（同网段更快，不需要 -k）' % lanDirect)
    return '\n'.join(line for line in text.split('\n')
                     if '{{LAN_LINE}}' not in line)


def loadTemplate():
    with open(TEMPLATE_PATH, encoding='utf-8') as f:
        return f.read()


def baseUrl():
    """本次请求的 API base：含 scheme/host/port，无尾斜杠。

    scheme 取 `wsgi.url_scheme`；`trustProxyProto` 打开时以上游的
    `X-Forwarded-Proto` 为准（合法值只有 http/https）——否则公网入口下
    印出的 base 是进程看到的 http，而隧道对明文请求返 501，外部智能体
    照手册抄的第一条命令就是坏的。

    **绝不采信 `X-Forwarded-Host` / `X-Forwarded-For`**：Host 头客户端
    可任意伪造，采信它等于让调用方决定手册印什么地址；这里只按受信
    的 scheme 重组前缀，host 仍取请求实际的 Host。
    """
    scheme = request.scheme
    if isTrustProxyProto():
        proto = (request.headers.get('X-Forwarded-Proto') or '').strip().lower()
        if proto in _TRUSTED_PROTO:
            scheme = proto
    return '%s://%s' % (scheme, request.host)


@blueprint.route('/api/agent/handbook')
def agentHandbook():
    if not config.isHandbookEnabled():
        abort(404)
    base = baseUrl()
    text = render(loadTemplate(), base, meta.BUILD, lanDirect())
    if request.args.get('format') == 'json':
        return jsonify({'version': meta.BUILD, 'base': base, 'markdown': text})
    resp = Response(text, mimetype='text/markdown')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp
