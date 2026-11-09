# -*- coding: utf-8 -*-
"""agent node 的 SSH 隧道传输层。

node 端口被防火墙过滤时，HTTP 请求经 SSH direct-tcpip 隧道转发：
本地 listener → paramiko channel → 远端 127.0.0.1:port。
连接断开由调用方捕获后 _dropTunnelsFor 强制重建。
"""
import socket
import threading
import atexit
from urllib.parse import urlparse

from modules import ssh_hosts as hosts

_pool = {}
_poolLock = threading.Lock()


class _Tunnel:
    """本地 TCP listener → SSH direct-tcpip 通道的双向泵。"""

    def __init__(self, hostEntry, rhost, rport):
        self.transport = hosts.getConnection(hostEntry).get_transport()
        self.rhost, self.rport = rhost, rport
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._acceptLoop, daemon=True).start()

    def healthy(self):
        return (not self.closed and self.transport is not None
                and self.transport.is_active())

    def _acceptLoop(self):
        while not self.closed:
            try:
                sock, addr = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._pump, args=(sock, addr),
                             daemon=True).start()

    def _pump(self, sock, addr):
        try:
            channel = self.transport.open_channel(
                'direct-tcpip', (self.rhost, self.rport), addr)
        except Exception:
            sock.close()
            return
        if channel is None:
            sock.close()
            return
        def downstream():
            try:
                while True:
                    data = channel.recv(32768)
                    if not data:
                        break
                    sock.sendall(data)
            except Exception:
                pass
            _shutdown(sock, channel)
        def upstream():
            try:
                while True:
                    data = sock.recv(32768)
                    if not data:
                        break
                    channel.sendall(data)
            except Exception:
                pass
            _shutdown(sock, channel)
        threading.Thread(target=downstream, daemon=True).start()
        upstream()

    def close(self):
        self.closed = True
        try:
            self.listener.close()
        except OSError:
            pass


def _shutdown(sock, channel):
    for closer in (sock.close, channel.close):
        try:
            closer()
        except Exception:
            pass


def _resolveViaHost(via):
    """SSH 主机解析：先 id 后 name（与 agent_api 同规则）。"""
    entry = hosts.findHost(via)
    if entry is not None:
        return entry
    wanted = str(via or '').strip().lower()
    for item in hosts.repo.all():
        if str(item.get('name') or '').strip().lower() == wanted:
            return item
    return None


def tunnelPort(node):
    """返回经 SSH 隧道后的本地端口；无 via 返回 None。失败抛异常。"""
    via = node.get('via') or ''
    if not via:
        return None
    entry = _resolveViaHost(via)
    if entry is None:
        raise RuntimeError('隧道主机不存在: %s' % via)
    parsed = urlparse(node['url'])
    rport = parsed.port or 8622
    rhost = '127.0.0.1'
    remote = node.get('remote') or ''
    if remote:
        parts = remote.rsplit(':', 1)
        rhost = parts[0] or rhost
        if len(parts) == 2 and parts[1].isdigit():
            rport = int(parts[1])
    key = (via, rhost, rport)
    with _poolLock:
        tunnel = _pool.get(key)
        if tunnel is not None and tunnel.healthy():
            return tunnel.port
        if tunnel is not None:
            tunnel.close()
        tunnel = _Tunnel(entry, rhost, rport)
        _pool[key] = tunnel
        return tunnel.port


def dropTunnelsFor(node):
    via = node.get('via') or ''
    with _poolLock:
        keys = [k for k in _pool if k[0] == via]
        tunnels = [_pool.pop(k) for k in keys]
    for tunnel in tunnels:
        tunnel.close()


def shutdownTunnels():
    with _poolLock:
        tunnels = list(_pool.values())
        _pool.clear()
    for tunnel in tunnels:
        tunnel.close()


atexit.register(shutdownTunnels)
