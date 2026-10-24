# -*- coding: utf-8 -*-
"""极简 paramiko SSH 服务端 — web-platform SSH 模块的本地集成测试设施。

支持:
    - 密码认证:        用户 test  / 密码 pass123
    - keyboard-interactive: 用户 kbi   / 提示 'Password:' 答案 pass123
    - shell 通道:      行回显语义 (收到一行 -> 回复 'echo:<内容>\r\n')
    - SFTP 子系统:     基于临时目录的内存 VFS (list/stat/read/write/mkdir/rmdir/rename)

用法: python fakessh_server.py [port] [root_dir]   (默认 2222 / 系统临时目录)
"""
import os
import socketserver
import sys
import threading
import tempfile
import time

import paramiko

HOST = '127.0.0.1'
USER_PASSWORD = ('test', 'pass123')
USER_KBI = 'kbi'


class FakeSFTPHandle(paramiko.SFTPHandle):
    """文件句柄: 基于本地真实文件的读写。"""

    def __init__(self, path, flags):
        # paramiko 5.0: SFTP flags 即 os 模块标志 (Windows: O_WRONLY=1, O_CREAT=0x100, O_TRUNC=0x200)
        import os as osMod
        writeMode = bool(flags & (osMod.O_WRONLY | osMod.O_RDWR))
        create = bool(flags & osMod.O_CREAT)
        trunc = bool(flags & osMod.O_TRUNC)
        append = bool(flags & osMod.O_APPEND)
        if append:
            mode = 'ab'
        elif writeMode or create or trunc:
            mode = 'r+b' if not (trunc or (create and not os.path.exists(path))) else 'wb'
        else:
            mode = 'rb'
        try:
            self.f = open(path, mode)
            print('[fakessh] handle opened:', path, 'mode=', mode, 'flags=', flags, flush=True)
        except OSError as e:
            print('[fakessh] handle open failed:', path, repr(e), flush=True)
            self.f = open(path, 'wb')
        super().__init__(flags)

    def read(self, offset, length):
        self.f.seek(offset)
        return self.f.read(length)

    def write(self, offset, data):
        print('[fakessh] handle write offset=', offset, 'len=', len(data), flush=True)
        self.f.seek(offset)
        self.f.write(data)
        return paramiko.SFTP_OK

    def close(self):
        self.f.close()


class FakeSFTPInterface(paramiko.SFTPServerInterface):
    """内存 VFS: 客户端视角的 '/' 映射到服务端 ROOT 目录。"""

    def __init__(self, server):
        self.root = server.root
        super().__init__(server)

    def _abs(self, path):
        rel = (path or '/').lstrip('/')
        full = os.path.abspath(os.path.join(self.root, rel.replace('/', os.sep)))
        base = os.path.abspath(self.root)
        if not (full == base or full.startswith(base + os.sep)):
            raise IOError('路径逃逸')
        return full

    def list_folder(self, path):
        full = self._abs(path)
        out = []
        try:
            for name in os.listdir(full):
                p = os.path.join(full, name)
                size = os.path.getsize(p) if os.path.isfile(p) else 0
                out.append(paramiko.SFTPAttributes.from_stat(os.stat(p), name))
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE
        return out

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._abs(path)))
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE

    lstat = stat

    def remove(self, path):
        try:
            os.remove(self._abs(path))
        except OSError:
            return paramiko.SFTP_FAILURE
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._abs(oldpath), self._abs(newpath))
        except OSError:
            return paramiko.SFTP_FAILURE
        return paramiko.SFTP_OK

    def mkdir(self, path, mode):
        try:
            os.mkdir(self._abs(path))
        except OSError:
            return paramiko.SFTP_FAILURE
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._abs(path))
        except OSError:
            return paramiko.SFTP_FAILURE
        return paramiko.SFTP_OK

    def open(self, path, flags, attr):
        try:
            return FakeSFTPHandle(self._abs(path), flags)
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE


class FakeServer(paramiko.ServerInterface):
    def __init__(self, root):
        self.root = root
        self.event = threading.Event()

    def get_allowed_auths(self, username):
        return 'password,keyboard-interactive'

    def check_auth_password(self, username, password):
        if username == USER_PASSWORD[0] and password == USER_PASSWORD[1]:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        query = paramiko.InteractiveQuery()
        query.add_prompt('Password: ', False)
        return query

    def check_auth_interactive_response(self, responses):
        # paramiko 5.0: responses 元素是字符串 (旧版是带 .value 的对象)
        values = [r.value if hasattr(r, 'value') else r for r in responses]
        if values and values[0] == USER_PASSWORD[1]:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_subsystem_request(self, channel, name):
        if name == 'sftp':
            sftp = paramiko.SFTPServer(channel, None, self, sftp_si=FakeSFTPInterface)
            sftp.start()
            return True
        return False


def shellEchoThread(channel):
    """行回显语义: 收到以 \\r 结尾的行 -> 回复 echo:<内容>"""
    buf = b''
    while True:
        try:
            data = channel.recv(4096)
        except Exception as e:
            print('[fakessh] echo recv exception:', repr(e), flush=True)
            break
        if not data:
            break
        buf += data.replace(b'\r\n', b'\r').replace(b'\n', b'\r')
        while b'\r' in buf:
            line, buf = buf.split(b'\r', 1)
            text = line.decode('utf-8', 'replace').rstrip()
            if text:
                channel.send(('echo:' + text + '\r\n').encode('utf-8'))
    channel.close()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        transport = paramiko.Transport(self.request)
        transport.add_server_key(paramiko.RSAKey.generate(2048))
        server = FakeServer(self.server.root)
        try:
            transport.start_server(server=server)
        except paramiko.SSHException:
            return
        channel = transport.accept(30)
        if channel is None:
            return
        threading.Thread(target=shellEchoThread, args=(channel,), daemon=True).start()
        # 关键: handler 返回会让 socketserver 关闭底层 socket, 杀死 transport
        # 必须在 transport 存活期间阻塞在此 (否则 shell 通道秒关)
        while transport.is_active():
            time.sleep(1)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2222
    root = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix='fakessh-')
    os.makedirs(root, exist_ok=True)
    Server.root = root
    server = Server((HOST, port), Handler)
    print('fake sshd on %s:%d (root=%s) user=%s/%s | kbi=%s' %
          (HOST, port, root, USER_PASSWORD[0], USER_PASSWORD[1], USER_KBI), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
