# -*- coding: utf-8 -*-
"""Deskport 本机作为内置主机（host='local'）——与 SSH 主机全等价（反馈 §7b）。

原则：能 exec、能开会话、能 SFTP 的目标不区分本机与远端，接口、参数、
响应格式、鉴权、审计完全一致，只换 host 名与默认 shell。

shell:
    bash        Git Bash（默认）——智能体写的命令多为 POSIX 风格
    powershell  Windows PowerShell 5.1（服务端统一转 UTF-8 / 统一退出码）
    pwsh        PowerShell 7
    会话（常驻通道）目前只支持 bash；powershell/pwsh 用单发 exec。

路径: `E:\事项` 与 `/e/事项` 双向接受，响应统一返回 Windows 原生写法。
边界: agentLocalEnabled=false 时 local 从 hosts 消失且所有调用 404。
"""
import base64
import hashlib
import os
import re
import secrets
import stat as statMod
import subprocess
import threading
import time

from flask import jsonify, request

from modules import events
from modules.agent_api import MAX_OUTPUT_BYTES, _agentRequired, blueprint, \
    parseTimeoutSec
from modules.fsutil import decodeBase64 as _decodeB64
from modules.fsutil import ensureParentDir
from modules.agent_sftp import _mapSftpError

MAX_FILE_BYTES = 8 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
BASH_CANDIDATES = ('bash',)
_LOCAL_SESSES = {}
_LOCAL_SESS_LOCK = threading.Lock()
LOCAL_SESSION_MAX = 8


# ---------------------------------------------------------------- 开关与主机

def localEnabled():
    """agentLocalEnabled 缺省开启；显式 false 才关闭（开源用户可控）。"""
    import config
    return config.load().get('agentLocalEnabled') is not False


def localHostEntry():
    return {'id': 'local', 'name': 'local', 'host': '127.0.0.1',
            'port': 0, 'os': 'windows',
            'defaultShell': 'bash',
            'shells': ['bash', 'powershell', 'pwsh']}


def isLocalHost(nameOrId):
    return str(nameOrId or '').strip().lower() == 'local'


def _resolveShell(data):
    """shell 参数校验：缺省 bash；不支持的返回 (None, 错误响应体)。"""
    shells = ('bash', 'powershell', 'pwsh')
    shell = str(data.get('shell') or 'bash').strip().lower()
    if shell not in shells:
        return None, {'error': '本机不支持 shell: %s' % shell,
                      'shells': list(shells)}
    return shell, None


def normalizeLocalPath(path):
    """`/e/事项` → `E:\事项`；已是 Windows 写法原样返回（统一 POSIX 风格入参）。"""
    text = str(path or '').strip()
    m = re.match(r'^/([A-Za-z])(/.*)?$', text)
    if m:
        rest = (m.group(2) or '').replace('/', '\\')
        return m.group(1).upper() + ':' + (rest or '\\')
    return text


def _findBash():
    import shutil
    return shutil.which('bash')


# ---------------------------------------------------------------- exec

def _psEncode(script):
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


_PS_PREFIX = ('$ErrorActionPreference="Continue";'
              '$ProgressPreference="SilentlyContinue";'
              '[Console]::OutputEncoding=[Text.UTF8Encoding]::new();')
_PS_SUFFIX = '\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } else { exit 0 }\n'


def _buildLocalCmd(command, shell):
    """本地子进程参数。bash 直接传；PS 走 -EncodedCommand 免引号地狱。

    PowerShell 5.1 在输出重定向时：默认把进度流序列化成 CLIXML 噪音进
    stderr（ProgressPreference 压掉）；且 exit 语句会终止在对象格式化器
    之前——`Get-Location` 这类对象输出被整个吞掉（字符串没事）。因此命令
    包进 `& { ... } | Out-Default`：格式化在管道内同步完成，exit 前已落盘；
    `$LASTEXITCODE` 仍穿透为进程退出码。"""
    if shell == 'bash':
        bash = _findBash()
        if not bash:
            return None, '本机找不到 bash（Git Bash）'
        return [bash, '-c', command], None
    exe = 'powershell' if shell == 'powershell' else 'pwsh'
    script = (_PS_PREFIX + '\n& {\n' + command + '\n} | Out-Default' + _PS_SUFFIX)
    encoded = _psEncode(script)
    argv = [exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-OutputFormat', 'Text', '-EncodedCommand', encoded]
    return argv, None


def _killTree(pid):
    if os.name != 'nt' or not pid:
        return
    try:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def execLocal(command, timeoutSec, cwd='', shell='bash'):
    """与 agent_api._execOnHost 同响应契约的本机执行。"""
    nativeCwd = normalizeLocalPath(cwd) or None
    if nativeCwd and not os.path.isdir(nativeCwd):
        return {'exit': 1, 'out': b'', 'err': ('cd: %s: No such file or directory'
                                               % nativeCwd).encode('utf-8'),
                'timedOut': False, 'reason': '', 'pid': None,
                'truncated': False, 'durationMs': 0}
    argv, err = _buildLocalCmd(command, shell)
    if err:
        return {'exit': 1, 'out': b'', 'err': err.encode('utf-8'),
                'timedOut': False, 'reason': '', 'pid': None,
                'truncated': False, 'durationMs': 0}
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=nativeCwd, creationflags=CREATE_NO_WINDOW)
    timedOut = False
    try:
        out, errMsg = proc.communicate(timeout=timeoutSec)
    except subprocess.TimeoutExpired:
        timedOut = True
        _killTree(proc.pid)
        try:
            out, errMsg = proc.communicate(timeout=3)
        except (subprocess.TimeoutExpired, ValueError):
            out, errMsg = b'', b''
    return {'exit': None if timedOut else (proc.returncode if proc.returncode is not None else 1),
            'out': out or b'', 'err': errMsg or b'',
            'timedOut': timedOut, 'reason': 'timeout' if timedOut else '',
            'pid': proc.pid,
            'truncated': max(len(out or b''), len(errMsg or b'')) > MAX_OUTPUT_BYTES,
            'durationMs': int((time.monotonic() - started) * 1000)}


# ---------------------------------------------------------------- 会话通道

class LocalBashSession:
    """本机常驻 bash（与 SSH 会话同一套哨兵协议，duck-typing 通道）。

    实现与 tests/test_agent_sessions.LocalBashChannel 相同的接口约定，
    让 agent_sessions 的命令投递/游标/哨兵逻辑原样复用。
    """

    def __init__(self, bootCwd=''):
        bash = _findBash()
        if not bash:
            raise FileNotFoundError('本机找不到 bash（Git Bash）')
        nativeCwd = normalizeLocalPath(bootCwd) or None
        if nativeCwd and not os.path.isdir(nativeCwd):
            raise NotADirectoryError('cd: %s: No such file or directory' % nativeCwd)
        self.proc = subprocess.Popen(
            [bash, '--norc', '-s'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=nativeCwd, creationflags=CREATE_NO_WINDOW)
        self.out = bytearray()
        self.err = bytearray()
        self._readPos = 0
        self._errPos = 0
        self._outDone = False
        self._errDone = False
        self._closedFlag = False
        self._lock = threading.Lock()
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.out,
                                                  '_outDone'), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.err,
                                                  '_errDone'), daemon=True).start()

    def _pump(self, pipe, buf, doneAttr):
        # read1 而不是 read(n)：read(n) 等满 n 字节/EOF，bash 的少量输出会饿死
        while True:
            chunk = pipe.read1(4096)
            if not chunk:
                break
            with self._lock:
                buf.extend(chunk)
        setattr(self, doneAttr, True)

    # ---- agent_sessions._SessionChannel 需要的接口 ----
    def get_transport(self):
        import types
        return types.SimpleNamespace(is_active=lambda: True)

    @property
    def closed(self):
        if self._closedFlag:
            return True
        if self.proc.poll() is not None:
            return self._outDone and self._errDone and self._drained()
        return False

    def _drained(self):
        with self._lock:
            return self._readPos >= len(self.out) and self._errPos >= len(self.err)

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv_ready(self):
        with self._lock:
            return self._readPos < len(self.out)

    def recv(self, n=65536):
        with self._lock:
            chunk = bytes(self.out[self._readPos:self._readPos + n])
            self._readPos += len(chunk)
            return chunk

    def recv_stderr_ready(self):
        with self._lock:
            return self._errPos < len(self.err)

    def recv_stderr(self, n=65536):
        with self._lock:
            chunk = bytes(self.err[self._errPos:self._errPos + n])
            self._errPos += len(chunk)
            return chunk

    def close(self):
        self._closedFlag = True
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            _killTree(self.proc.pid)


def openLocalSession(cwd, shell):
    """开会话；powershell/pwsh 暂不支持常驻通道（§7b.2 会话 shell 定死在创建时）。"""
    if shell != 'bash':
        return None, ({'error': '本机持久会话目前只支持 bash（%s 请用单发 exec）'
                       % shell, 'shells': ['bash']}, 400)
    with _LOCAL_SESS_LOCK:
        alive = sum(1 for s in _LOCAL_SESSES.values() if s['alive'])
    if alive >= LOCAL_SESSION_MAX:
        return None, ({'error': '本机存活会话已达上限 (%d)' % LOCAL_SESSION_MAX}, 429)
    try:
        chan = LocalBashSession(cwd)
    except FileNotFoundError as e:
        return None, ({'error': str(e)}, 502)
    except NotADirectoryError as e:
        return None, ({'error': 'cwd 不可用: %s' % e,
                       'hint': 'cwd 必须是本机已存在的目录（接受 /e/事项 写法）'}, 400)
    return chan, None


# ---------------------------------------------------------------- SFTP

def _localType(stMode):
    table = {statMod.S_IFDIR: 'dir', statMod.S_IFREG: 'file',
             statMod.S_IFLNK: 'symlink', statMod.S_IFIFO: 'fifo',
             statMod.S_IFSOCK: 'socket', statMod.S_IFCHR: 'chardev',
             statMod.S_IFBLK: 'blockdev'}
    return table.get(stMode & 0o170000, 'other')


def sftpListLocal(path):
    """os 版 list：与 agent_sftp.agentSftpList 同响应契约。"""
    raw = str(path or '.').strip() or '.'
    try:
        native = normalizeLocalPath(os.path.expanduser(raw))
        st = os.stat(native)
        if not statMod.S_ISDIR(st.st_mode):
            return jsonify({'error': '不是目录: %s' % native,
                            'hint': 'list 只能列目录，读文件请用 sftp/get'}), 400
        entries = []
        for name in sorted(os.listdir(native)):
            full = os.path.join(native, name)
            try:
                lst = os.lstat(full)
            except OSError:
                continue
            ftype = _localType(lst.st_mode)
            entry = {'name': name, 'path': full, 'size': lst.st_size,
                     'mtime': int(lst.st_mtime),
                     'dir': ftype == 'dir', 'type': ftype,
                     'mode': '%04o' % (lst.st_mode & 0o7777)}
            if ftype == 'symlink':
                try:
                    entry['target'] = os.readlink(full)
                except OSError:
                    entry['target'] = ''
                try:
                    target = os.stat(full)
                    entry['dir'] = statMod.S_ISDIR(target.st_mode)
                    entry['size'] = target.st_size
                except OSError:
                    pass
            entries.append(entry)
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except OSError as e:
        status, msg = _mapSftpError(e)
        return jsonify({'error': msg}), status
    return jsonify({'path': native, 'entries': entries})


def sftpGetLocal(path, offset=0, length=0):
    try:
        native = normalizeLocalPath(os.path.expanduser(str(path or '')))
        st = os.stat(native)
        if statMod.S_ISDIR(st.st_mode):
            return jsonify({'error': '是目录: %s' % native,
                            'hint': '读目录内容请用 sftp/list'}), 400
        total = st.st_size
        if length == 0:
            if offset:
                length = MAX_FILE_BYTES
            elif total > MAX_FILE_BYTES:
                return jsonify({'error': '文件超过整读上限 (%d bytes)' % MAX_FILE_BYTES,
                                'hint': '用 offset/length 分块读取（本响应含 total）',
                                'total': total, 'chunkLimit': MAX_FILE_BYTES}), 413
        with open(native, 'rb') as f:
            if offset:
                f.seek(offset)
            chunk = f.read(min(length or MAX_FILE_BYTES, MAX_FILE_BYTES))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except OSError as e:
        status, msg = _mapSftpError(e)
        return jsonify({'error': msg}), status
    return jsonify({'ok': True, 'path': native, 'size': len(chunk),
                    'total': total, 'offset': offset,
                    'contentB64': base64.b64encode(chunk).decode('ascii')})


def sftpPutLocal(path, contentB64, wantSha='', mkdirs=False):
    try:
        native = normalizeLocalPath(os.path.expanduser(str(path or '')))
        raw = _decodeB64(contentB64)
    except ValueError:
        return jsonify({'error': 'contentB64 不是合法 base64（已拒绝写入，'
                                 '原文件未被改动）'}), 400
    if len(raw) > MAX_FILE_BYTES:
        return jsonify({'error': '文件超过上限 (%d bytes)' % MAX_FILE_BYTES}), 413
    if wantSha and hashlib.sha256(raw).hexdigest() != wantSha.lower():
        return jsonify({'error': 'sha256 校验失败：内容与调用方摘要不一致'}), 400
    parent = os.path.dirname(native) or '.'
    bad = ensureParentDir(parent, mkdirs)   # R5-3：缺父目录时创建或干净报错
    if bad:
        return jsonify(bad[1]), bad[0]
    try:
        oldMode = None
        try:
            oldMode = os.stat(native).st_mode & 0o7777
        except OSError:
            pass
        tmp = os.path.join(parent, '.deskport-put-%s.tmp' % secrets.token_hex(3))
        with open(tmp, 'wb') as f:
            f.write(raw)
        os.replace(tmp, native)
        if oldMode is not None:
            try:
                os.chmod(native, oldMode)
            except OSError:
                pass
    except PermissionError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return jsonify({'error': str(e)}), 403
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except OSError as e:
        status, msg = _mapSftpError(e)
        return jsonify({'error': msg}), status
    return jsonify({'ok': True, 'path': native, 'size': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest()})


# ---------------------------------------------------------------- 路由

@blueprint.route('/api/agent/local/exec', methods=['POST'])
def agentLocalExec():
    """本机 exec（host='local' 的等价入口；/api/agent/ssh/exec 亦可路由到此）。"""
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    rawCommand = data.get('command')
    command = str(rawCommand or '').strip()
    if not command:
        return jsonify({'error': '缺少 command'}), 400
    if isinstance(rawCommand, str) and '\x00' in rawCommand:
        return jsonify({'error': 'command 含 NUL 字符'}), 400
    cwd = str(data.get('cwd') or '').strip()
    if len(cwd) > 500:
        return jsonify({'error': 'cwd 过长'}), 400
    encoding = str(data.get('encoding') or 'text').lower()
    if encoding not in ('text', 'base64'):
        return jsonify({'error': 'encoding 只能是 text / base64'}), 400
    timeoutSec, timeoutErr = parseTimeoutSec(data)
    if timeoutErr:
        return jsonify({'error': timeoutErr}), 400
    shell, shellErr = _resolveShell(data)
    if shellErr:
        return jsonify(shellErr), 400
    result = execLocal(command, timeoutSec, cwd, shell)
    status = 'timeout' if result['timedOut'] else 'ok'
    events.log('agent.exec', 'local(%s)' % shell, status, command[:160],
               request.remote_addr)
    body = {'exit': result['exit'], 'timedOut': result['timedOut'],
            'reason': result['reason'], 'outTruncated': result['truncated'],
            'durationMs': result['durationMs'], 'timeoutSec': timeoutSec,
            'pid': result['pid'], 'shell': shell,
            'cwd': normalizeLocalPath(cwd) if cwd else ''}
    if encoding == 'base64':
        body['outB64'] = base64.b64encode(result['out'][:MAX_OUTPUT_BYTES]).decode('ascii')
        body['errB64'] = base64.b64encode(result['err'][:MAX_OUTPUT_BYTES]).decode('ascii')
    else:
        body['out'] = result['out'][:MAX_OUTPUT_BYTES].decode('utf-8', 'replace')
        body['err'] = result['err'][:MAX_OUTPUT_BYTES].decode('utf-8', 'replace')
    return jsonify(body)


@blueprint.route('/api/agent/local/sftp/list', methods=['POST'])
def agentLocalSftpList():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    resp = sftpListLocal(data.get('path'))
    if resp[1] == 200:
        events.log('agent.sftp.list', data.get('path'), 'ok', '', request.remote_addr)
    return resp


@blueprint.route('/api/agent/local/sftp/get', methods=['POST'])
def agentLocalSftpGet():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    return sftpGetLocal(data.get('path'),
                        int(data.get('offset') or 0), int(data.get('length') or 0))


@blueprint.route('/api/agent/local/sftp/put', methods=['POST'])
def agentLocalSftpPut():
    agent, err = _agentRequired()
    if agent is None:
        return err
    if request.content_length and request.content_length > MAX_FILE_BYTES * 2 + 65536:
        return jsonify({'error': '请求体超过上限（文件上限 %d bytes）' % MAX_FILE_BYTES}), 413
    data = request.get_json(silent=True) or {}
    resp = sftpPutLocal(data.get('path'), data.get('contentB64'),
                        str(data.get('sha256') or ''),
                        mkdirs=bool(data.get('mkdirs')))
    if isinstance(resp, tuple):   # 错误 = (jsonify, status)；成功 = 裸 Response
        return resp
    events.log('agent.sftp.put', data.get('path'), 'ok', '', request.remote_addr)
    return resp
