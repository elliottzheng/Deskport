# -*- coding: utf-8 -*-
"""智能体持久会话: bash 常驻通道, 多次 exec 共享 cwd 与环境。

端点 (路由挂在 agent_api 的 blueprint 上, 前缀 /api/agent/ssh/sessions):
    POST   /api/agent/ssh/sessions          开会话 {host, cwd}
    GET    /api/agent/ssh/sessions          会话列表
    POST   /api/agent/ssh/sessions/<sid>    执行命令 {command, timeoutSec, tailBytes, combined, full}
    DELETE /api/agent/ssh/sessions/<sid>    关闭会话

可靠性设计（外部智能体实测反馈 P0-2/P0-3/P1-5/P1-6/P2-23）:
    - 开会话时 cd 失败立即 400（带回远端报错），不再"创建成功、执行全 500"；
    - 每条命令经 base64 进 eval 执行：解析错误（未闭合引号等）在 eval 内部
      爆掉（rc=2 + bash 原始报错），持久 shell 的输入流没有用户原文，
      永不被语法错误卡死；
    - 命令以 </dev/null 执行：read x/cat 不再吞掉哨兵或挂死；
      cd/export/venv 激活仍在持久 shell 里生效；
    - 每条命令唯一哨兵 __DONE_<nonce>=<rc>:<pwd>；超时如实返回
      timedOut:true / exit:null，会话进入 pending——下次调用先等旧哨兵
      落地（最多 2s，期间旧输出整段丢弃），仍没落地则 409 busy；
    - EXIT trap 捕获 shell 退出码：exit 7 → {exit:7, alive:false}，
      之后该会话的调用返回 410 JSON；死会话从列表自动回收。
"""
import base64
import re
import secrets
import shlex
import threading
import time

import paramiko
from flask import jsonify, request

from modules import events
from modules.agent_api import (requireAgentScope, _resolveHost, _safeHosts,
                               blueprint, parseTimeoutSec)
from modules.ssh_hosts import getConnection

_sessions = {}
_sessionsLock = threading.Lock()
SESSION_MAX_OPEN = 16
PENDING_GRACE = 2.0        # 等上一条超时命令哨兵落地的宽限
DEAD_PRUNE_SECONDS = 1800  # 死会话在列表里的最长保留时间
BOOT_GRACE = 3.0           # 开会话等待 __READY 的宽限


class _SessionChannel:
    """bash 常驻通道: stdin 喂命令, 双流按游标累积。"""

    def __init__(self, channel):
        self.ch = channel
        self.out = bytearray()
        self.err = bytearray()

    def sendLine(self, line):
        self.ch.sendall(line.encode('utf-8') + b'\n')

    def sendall(self, data):
        self.ch.sendall(data)

    def pump(self):
        got = False
        while self.ch.recv_ready():
            self.out += self.ch.recv(65536)
            got = True
        while self.ch.recv_stderr_ready():
            self.err += self.ch.recv_stderr(65536)
            got = True
        return got

    def dead(self):
        if self.ch.closed:
            return True
        transport = self.ch.get_transport()
        return transport is None or not transport.is_active()

    def drain(self):
        while self.ch.recv_ready():
            self.out += self.ch.recv(65536)
        while self.ch.recv_stderr_ready():
            self.err += self.ch.recv_stderr(65536)


def _findDone(buf, start, nonce):
    """在 buf[start:] 找 `__DONE_<nonce>=<rc>:<pwd>` 完整行。

    返回 (起点, 行尾后, rc, pwd)；行没收全返回 (None, None, None, '')。
    """
    needle = ('__DONE_' + nonce + '=').encode('utf-8')
    searchFrom = start
    while True:
        idx = buf.find(needle, searchFrom)
        if idx < 0:
            return None, None, None, ''
        lineEnd = buf.find(b'\n', idx)
        if lineEnd < 0:
            return None, None, None, ''
        m = re.search(rb'=(-?\d+):(.*)\s*$', bytes(buf[idx:lineEnd]))
        if m:
            pwd = m.group(2).decode('utf-8', 'replace').strip()
            return idx, lineEnd + 1, int(m.group(1)), pwd
        searchFrom = lineEnd + 1


def _findShellExit(buf, nonce):
    """找 EXIT trap 打的 `__SHELL_EXIT_<nonce>=<rc>` 行；返回 rc 或 None。"""
    needle = ('__SHELL_EXIT_' + nonce + '=').encode('utf-8')
    idx = buf.find(needle)
    if idx < 0:
        return None
    lineEnd = buf.find(b'\n', idx)
    end = lineEnd if lineEnd >= 0 else len(buf)
    m = re.search(rb'=(-?\d+)', bytes(buf[idx:end]))
    return int(m.group(1)) if m else None


def _openChannel(hostEntry, cwd):
    client = getConnection(hostEntry)
    transport = client.get_transport()
    channel = transport.open_session(timeout=10)
    channel.settimeout(1.0)
    boot = ''
    if cwd:
        boot = 'cd ' + shlex.quote(cwd) + ' && '
    # 不用 exec：外层 shell 留着，内层 bash --norc -s 读命令；
    # 开会后立刻喂 __READY 探针即可正向确认 bash 就绪；cd 失败则整链短路、
    # 通道关闭（全用 && 保证 cd 失败时 bash 绝不启动）
    channel.exec_command(
        boot + 'export LANG=C.UTF-8 LC_ALL=C.UTF-8 && bash --norc -s')
    return channel


def _buildCommandPayload(command, nonce):
    """一条命令的完整投递文本：eval 隔离 + stdin 关闭 + 唯一哨兵。

    用户命令经 base64 进入 eval——解析错误（未闭合引号、坏 heredoc）在
    eval 内部爆掉（rc=2 + bash 原始报错），持久 shell 的输入流里没有任何
    用户原文，永远不会被语法错误卡死；`</dev/null` 让 read x/cat 直接见到
    EOF；eval 在持久 shell 里展开，cd/export/venv 激活照常延续。
    """
    b64 = base64.b64encode(command.encode('utf-8')).decode('ascii')
    return '\n'.join([
        'trap \'printf "__SHELL_EXIT_' + nonce + '=%s\\n" "$?"\' EXIT',
        'rc=0',
        'eval "$(printf %s ' + b64 + ' | base64 -d)" </dev/null || rc=$?',
        'printf \'__DONE_' + nonce + '=%s:%s\\n\' "$rc" "$PWD"',
    ])


@blueprint.route('/api/agent/ssh/sessions', methods=['POST'])
def agentSessionOpen():
    agent, err = requireAgentScope()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    cwd = str(data.get('cwd') or '').strip()
    isLocal = str(data.get('host') or '').strip().lower() == 'local'
    with _sessionsLock:
        openCount = sum(1 for s in _sessions.values() if s['alive'])
    if openCount >= SESSION_MAX_OPEN:
        return jsonify({'error': '存活会话已达上限 (%d)，请先关闭部分会话'
                        % SESSION_MAX_OPEN}), 429
    if isLocal:
        from modules.agent_local import (localEnabled, openLocalSession,
                                         normalizeLocalPath, _resolveShell)
        if not localEnabled():
            names = [h.get('name') or h.get('id') for h in _safeHosts()][:8]
            return jsonify({'error': '主机不存在', 'availableHosts': names}), 404
        shell, shellErr = _resolveShell(data)
        if shellErr:
            return jsonify(shellErr), 400
        chan, errResp = openLocalSession(cwd, shell)
        if errResp:
            return jsonify(errResp[0]), errResp[1]
        hostName = 'local'
        cwd = normalizeLocalPath(cwd)
    else:
        hostEntry = _resolveHost(str(data.get('host') or ''))
        if hostEntry is None:
            names = [h.get('name') or h.get('id') for h in _safeHosts()][:8]
            return jsonify({'error': '主机不存在', 'availableHosts': names}), 404
        try:
            channel = _openChannel(hostEntry, cwd)
        except paramiko.SSHException as e:
            return jsonify({'error': 'SSH 会话建立失败: %s' % e}), 502
        chan = channel
        hostName = hostEntry.get('name') or hostEntry.get('host')

    chan = _SessionChannel(chan)
    # 就绪探针：内层 bash 能读 stdin 时回显 __READY；cd 失败则通道直接关闭
    readyNonce = secrets.token_hex(6)
    chan.sendLine('printf "__READY_' + readyNonce + '\\n"')
    deadline = time.monotonic() + BOOT_GRACE
    ready = False
    while time.monotonic() < deadline:
        chan.pump()
        if ('__READY_' + readyNonce).encode('utf-8') in chan.out:
            ready = True
            break
        if chan.dead():
            errText = chan.err.decode('utf-8', 'replace').strip()
            return jsonify({'error': 'cwd 不可用: %s' % (errText[:300] or 'cd 失败'),
                            'hint': 'cwd 必须是远端已存在的目录'}), 400
        time.sleep(0.05)
    if not ready:
        try:
            chan.ch.close()
        except Exception:
            pass
        return jsonify({'error': '会话启动超时，请重试'}), 504
    sid = secrets.token_hex(8)
    with _sessionsLock:
        _sessions[sid] = {'id': sid, 'chan': chan, 'lock': threading.Lock(),
                          'host': hostName, 'local': isLocal,
                          'cwd': cwd or '~', 'alive': True, 'pending': '',
                          'pendingFrom': 0, 'createdAt': time.time(),
                          'deadAt': 0}
    events.log('agent.session.open', hostName, 'ok', cwd or '~',
               request.remote_addr)
    return jsonify({'sessionId': sid, 'host': hostName,
                    'cwd': cwd or '~'})


def _pruneAndListLocked():
    """标记死会话并清理过期死会话；调用方需已持有 _sessionsLock。"""
    now = time.time()
    items = []
    for sid in list(_sessions.keys()):
        sess = _sessions[sid]
        if sess['alive'] and sess['chan'].dead():
            sess['alive'] = False
            sess['deadAt'] = now
        if not sess['alive'] and sess['deadAt'] \
                and now - sess['deadAt'] > DEAD_PRUNE_SECONDS:
            del _sessions[sid]
            continue
        items.append({'id': sess['id'], 'host': sess['host'],
                      'cwd': sess['cwd'], 'alive': sess['alive']})
    return items


@blueprint.route('/api/agent/ssh/sessions')
def agentSessionList():
    agent, err = requireAgentScope()
    if agent is None:
        return err
    with _sessionsLock:
        items = _pruneAndListLocked()
    return jsonify({'sessions': items})


@blueprint.route('/api/agent/ssh/sessions/<sid>', methods=['DELETE'])
def agentSessionClose(sid):
    agent, err = requireAgentScope()
    if agent is None:
        return err
    with _sessionsLock:
        sess = _sessions.pop(sid, None)
    if sess is None:
        return jsonify({'error': '会话不存在'}), 404
    sess['alive'] = False
    try:
        sess['chan'].ch.close()
    except Exception:
        pass
    return jsonify({'ok': True})


@blueprint.route('/api/agent/ssh/sessions/<sid>', methods=['POST'])
def agentSessionExec(sid):
    agent, err = requireAgentScope()
    if agent is None:
        return err
    with _sessionsLock:
        sess = _sessions.get(sid)
    if sess is None:
        return jsonify({'error': '会话不存在'}), 404
    if not sess['alive']:
        with _sessionsLock:
            _sessions.pop(sid, None)
        return jsonify({'error': '会话已结束（shell 退出或通道断开），请新开会话',
                        'reason': 'dead'}), 410
    data = request.get_json(silent=True) or {}
    rawCommand = data.get('command')
    command = str(rawCommand or '').strip()
    if not command:
        return jsonify({'error': '缺少 command'}), 400
    if isinstance(rawCommand, str) and '\x00' in rawCommand:
        return jsonify({'error': 'command 含 NUL 字符'}), 400
    timeoutSec, timeoutErr = parseTimeoutSec(data)
    if timeoutErr:
        return jsonify({'error': timeoutErr}), 400
    tailBytes = max(256, min(int(data.get('tailBytes') or 8192), 65536))
    combined = bool(data.get('combined'))
    fullOut = bool(data.get('full'))

    chan = sess['chan']
    exitCode = None
    cwdReport = ''
    errBytes = b''
    reason = ''
    with sess['lock']:
        busy = _resolvePending(sess, chan)
        if busy:
            return jsonify({'error': busy, 'reason': 'busy',
                            'hint': '可稍后重试（旧命令落地后自动恢复），'
                                    '或 DELETE 会话强制重建'}), 409
        outStart = len(chan.out)
        errStart = len(chan.err)
        nonce = secrets.token_hex(8)
        command = command.replace('\r\n', '\n').replace('\r', '\n')
        chan.sendall(_buildCommandPayload(command, nonce).encode('utf-8') + b'\n')
        deadline = time.monotonic() + timeoutSec
        done = None
        while True:
            chan.pump()
            idx, lineEnd, rc, pwd = _findDone(chan.out, outStart, nonce)
            if idx is not None:
                done = (idx, lineEnd, rc, pwd)
                break
            if chan.dead():
                chan.drain()
                idx, lineEnd, rc, pwd = _findDone(chan.out, outStart, nonce)
                if idx is not None:
                    done = (idx, lineEnd, rc, pwd)
                break
            if time.monotonic() >= deadline:
                cut = _trimPendingBuffer(len(chan.out))
                if cut:
                    outStart -= cut
                    sess['pendingFrom'] = max(0, sess['pendingFrom'] - cut)
                if len(chan.out) <= 1_000_000 or not cut:
                    sess['pending'] = nonce
                    sess['pendingFrom'] = outStart
                break
            time.sleep(0.05)
        if done:
            idx, lineEnd, exitCode, cwdReport = done
            # stderr 与 stdout 是两条流，错误报文可能晚于哨兵几毫秒——
            # 静默 60ms 再切游标，语法错误等信息才不会被截丢
            settle = time.monotonic() + 0.06
            while time.monotonic() < settle:
                if chan.pump():
                    settle = time.monotonic() + 0.06
                else:
                    time.sleep(0.02)
            outBytes = bytes(chan.out[outStart:idx])
            errBytes = bytes(chan.err[errStart:])
            del chan.out[:lineEnd]
            del chan.err[:errStart]
            timedOut = False
            alive = True
        elif sess.get('pending') == nonce:
            # 超时且命令仍在跑：如实上报，输出游标不推进，交给 pending 机制
            outBytes = bytes(chan.out[outStart:])
            del chan.err[:errStart]
            timedOut = True
            alive = True
            reason = 'timeout'
        else:
            # 通道已死：EXIT trap 兜到退出码则如实回报
            exitCode = _findShellExit(chan.out, nonce)
            outBytes = bytes(chan.out[outStart:])
            # 剥离 trap 标记行，不把实现细节漏给调用方
            marker = ('__SHELL_EXIT_' + nonce).encode('utf-8')
            at = outBytes.find(marker)
            if at >= 0:
                end = outBytes.find(b'\n', at)
                if end >= 0:
                    outBytes = outBytes[:at] + outBytes[end + 1:]
            errBytes = bytes(chan.err[errStart:])
            del chan.err[:errStart]
            timedOut = False
            alive = False
            reason = 'shell exited' if exitCode is not None else 'channel closed'
            sess['alive'] = False
            sess['deadAt'] = time.time()

    outText = outBytes.decode('utf-8', 'replace')
    errText = errBytes.decode('utf-8', 'replace')
    truncated = False
    if not fullOut and len(outText) > tailBytes:
        outText = outText[-tailBytes:]
        truncated = True
    if combined:
        outText = (outText.rstrip('\n') + '\n' + errText).strip('\n')
        errText = ''
    if cwdReport:
        if sess.get('local'):
            from modules.agent_local import normalizeLocalPath
            sess['cwd'] = normalizeLocalPath(cwdReport)
        else:
            sess['cwd'] = cwdReport
    events.log('agent.exec.session', sess['host'],
               reason or 'ok', command[:160], request.remote_addr)
    return jsonify({'exit': exitCode, 'out': outText,
                    'err': '' if combined else errText,
                    'timedOut': timedOut, 'reason': reason,
                    'outTruncated': truncated,
                    'cwd': sess['cwd'], 'alive': alive})


def _resolvePending(sess, chan):
    """上一条超时命令的哨兵落地则丢弃其整段输出并放行；否则返回 busy 说明。

    哨兵没落地 = 旧命令还在跑（或会话已死），此时绝不能叠加新命令——
    这是「迟到的输出串进下一条命令」的根治手段。
    """
    pending = sess.get('pending')
    if not pending:
        return ''
    deadline = time.monotonic() + PENDING_GRACE
    while time.monotonic() < deadline:
        chan.pump()
        idx, lineEnd, _rc, pwd = _findDone(chan.out, sess['pendingFrom'], pending)
        if idx is not None:
            del chan.out[:lineEnd]
            del chan.err[:]
            sess['pending'] = ''
            sess['pendingFrom'] = 0
            if pwd:
                if sess.get('local'):
                    from modules.agent_local import normalizeLocalPath
                    sess['cwd'] = normalizeLocalPath(pwd)
                else:
                    sess['cwd'] = pwd
            return ''
        if chan.dead():
            sess['alive'] = False
            sess['deadAt'] = time.time()
            sess['pending'] = ''
            return ''
        time.sleep(0.05)
    return ('上一条命令仍在运行（上次超时未完成，哨兵 %s 未落地）' % pending[:8])


def _trimPendingBuffer(bufLen):
    """pending 等待期间缓冲超限时返回应截断的字节数；不超限返回 0。

    防止 tail -f 这类永不结束的命令在等哨兵时吃满内存。
    """
    if bufLen <= 1_000_000:
        return 0
    return bufLen - 500_000
