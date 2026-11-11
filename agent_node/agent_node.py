#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deskport Agent Node — 在远端主机托管 kimi acp 会话，暴露 HTTP API。

纯标准库实现（零第三方依赖），拷贝本目录到目标机即可运行：
    python3 agent_node.py --port 8622 --token-file ~/.agent_node_token

认证：除 /health 外全部要求请求头 X-Agent-Token 与配置的令牌一致；
令牌优先级 --token-file > 环境变量 AGENT_NODE_TOKEN > --token（已废弃，
明文会出现在进程参数里）。
会话模型：一个逻辑会话 = 一个 `kimi acp` 子进程；进程可因空闲被回收，
kimiSessionId 持久化在 sessions.json，随时可用 session/load 接续。
事件模型：内存环形缓冲（seq 单调递增），客户端用 since=<seq> 增量拉取。

模块布局：acp_session.py = ACP 协议托管核心；node_api.py = HTTP 层；
本文件 = 入口（参数、空闲回收、信号、HTTP 服务）。
"""
import argparse
import os
import shutil
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from node_api import Handler, SessionManager, discoverModels, init
from model_probe import ModelProber

MANAGER = None


def main():
    global MANAGER
    if sys.version_info < (3, 11):
        raise SystemExit('agent node 需要 Python ≥ 3.11（tomllib 用于模型自动发现），'
                         '当前 %s。建议用 uv: `uv python install 3.13` 后以该解释器启动'
                         % sys.version.split()[0])
    parser = argparse.ArgumentParser(description='Deskport Agent Node')
    parser.add_argument('--port', type=int, default=8622)
    parser.add_argument('--token', default='',
                        help='[已废弃] 明文令牌会出现在进程参数里（ps 可见），'
                             '改用 --token-file 或环境变量 AGENT_NODE_TOKEN')
    parser.add_argument('--token-file', default='',
                        help='从文件读令牌；不存在则以随机值原子创建（与 Deskport 共享）')
    parser.add_argument('--kimi', default=os.environ.get('KIMI_BIN') or shutil.which('kimi'))
    parser.add_argument('--data', default=os.path.join(os.path.expanduser('~'), '.deskport-agent-node'))
    args = parser.parse_args()
    if not args.kimi:
        print('找不到 kimi 可执行文件，请用 --kimi 指定', file=sys.stderr)
        sys.exit(2)
    # 令牌优先级（R4-6）：token-file > 环境变量 AGENT_NODE_TOKEN > --token。
    # --token 只为旧部署保留：明文会出现在 argv 里（ps -eo args 同机可见）。
    token = ''
    if args.token_file:
        import secrets
        path = args.token_file
        if not os.path.isfile(path):
            try:
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                with open(path, 'x', encoding='utf-8') as f:
                    f.write(secrets.token_hex(24))
            except FileExistsError:
                pass
        try:
            with open(path, 'r', encoding='utf-8') as f:
                token = f.read().strip()
        except OSError:
            token = ''
        if os.name == 'posix' and token:
            try:
                mode = os.stat(path).st_mode & 0o777
                if mode & 0o077:
                    print('警告: 令牌文件 %s 权限 %04o 宽于 600，同机其他用户'
                          '可读；建议 chmod 600 %s'
                          % (path, mode, path), file=sys.stderr)
            except OSError:
                pass
        if not token:
            print('警告: 令牌文件 %s 为空，除 /health 外的请求将全部放行，'
                  '请写入令牌内容' % path, file=sys.stderr)
    elif (os.environ.get('AGENT_NODE_TOKEN') or '').strip():
        token = os.environ['AGENT_NODE_TOKEN'].strip()
    elif args.token:
        print('警告: --token 已废弃——明文令牌会出现在进程参数里'
              '（ps -eo args 同机可见），请改用 --token-file 或环境变量 '
              'AGENT_NODE_TOKEN', file=sys.stderr)
        token = args.token.strip()
    if not token:
        print('警告: 未配置任何令牌，除 /health 外的请求将全部放行；'
              '建议 --token-file 或 AGENT_NODE_TOKEN', file=sys.stderr)
    os.makedirs(args.data, exist_ok=True)
    MANAGER = SessionManager(args.kimi, args.data)
    PROBER = ModelProber(args.kimi, args.data)
    init(MANAGER, token, PROBER)

    # R4-10 默认探测集 = defaultModel + 近 7 天用过的（成本可控；全量走
    # POST /api/probe-models 手动触发）。每 30 分钟复查一次，默认集里
    # 超 24h 过期的自动补测，worker 并发 ≤2、绝不阻塞 /health。
    def sweepModels():
        models = [discoverModels()['default']]
        models += PROBER.recentModels(MANAGER.list())
        PROBER.probe([m for m in models if m], force=False)

    def sweepLoop():
        sweepModels()
        while True:
            time.sleep(1800)
            try:
                sweepModels()
            except Exception:
                pass
    threading.Thread(target=sweepLoop, daemon=True).start()

    def reap():
        while True:
            time.sleep(60)
            try:
                MANAGER.reapIdle()
            except Exception:
                pass
    threading.Thread(target=reap, daemon=True).start()

    def onTerm(signum, frame):
        MANAGER.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, onTerm)
    signal.signal(signal.SIGINT, onTerm)

    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.daemon_threads = True
    print('[agent-node] listening on 0.0.0.0:%d kimi=%s data=%s'
          % (args.port, args.kimi, args.data), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
