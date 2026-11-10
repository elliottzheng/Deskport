#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deskport Agent Node — 在远端主机托管 kimi acp 会话，暴露 HTTP API。

纯标准库实现（零第三方依赖），拷贝本目录到目标机即可运行：
    python3 agent_node.py --port 8622 --token <TOKEN>

认证：除 /health 外全部要求请求头 X-Agent-Token 与 --token 一致。
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

from node_api import Handler, SessionManager, init

MANAGER = None


def main():
    global MANAGER
    if sys.version_info < (3, 11):
        raise SystemExit('agent node 需要 Python ≥ 3.11（tomllib 用于模型自动发现），'
                         '当前 %s。建议用 uv: `uv python install 3.13` 后以该解释器启动'
                         % sys.version.split()[0])
    parser = argparse.ArgumentParser(description='Deskport Agent Node')
    parser.add_argument('--port', type=int, default=8622)
    parser.add_argument('--token', default=os.environ.get('AGENT_NODE_TOKEN', ''))
    parser.add_argument('--token-file', default='',
                        help='从文件读令牌；不存在则以随机值原子创建（与 Deskport 共享）')
    parser.add_argument('--kimi', default=os.environ.get('KIMI_BIN') or shutil.which('kimi'))
    parser.add_argument('--data', default=os.path.join(os.path.expanduser('~'), '.deskport-agent-node'))
    args = parser.parse_args()
    if not args.kimi:
        print('找不到 kimi 可执行文件，请用 --kimi 指定', file=sys.stderr)
        sys.exit(2)
    token = args.token
    if not token and args.token_file:
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
    os.makedirs(args.data, exist_ok=True)
    MANAGER = SessionManager(args.kimi, args.data)
    init(MANAGER, token)

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
