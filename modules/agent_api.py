# -*- coding: utf-8 -*-
"""智能体 API — 给外部 AI 程序的机器接口（令牌认证，非交互式执行 + 主机）。

认证:
    请求头 X-Agent-Token 或 Authorization: Bearer <token>，
    与 config.json agentTokens[].tokenHash（SHA-256）比对；未命中一律 401。
    浏览器 Cookie 登录态同样放行（供面板 UI 未来复用）。
    令牌 scope: agentTokens[].scopes 白名单；没写 = 全权（向后兼容）。
    现有端点要求 full，手机上传要求 upload；越权 403 {"error","need"}。

端点:
    GET  /api/agent/hosts          主机列表（只含 id/name/host/port，无凭据）
    POST /api/agent/ssh/exec       非交互执行命令（paramiko exec_command）

配套路由（蓝图同源, 在 app.py 显式登记）:
    modules/agent_sftp.py          SFTP list/get/put（错误码、分块、原子写）
    modules/agent_sessions.py      持久会话（bash 常驻通道）
    modules/agent_local.py         Deskport 本机作为内置主机（全等价）
    modules/agent_upload.py        手机分享上传（scope=upload）
    modules/agent_skill_files.py   技能包附件（客户端/驱动/协作原则，同一套自举）
    modules/device_auth.py         设备登录（App 密码换 upload 设备令牌, 自吊销）

审计: 每次调用写 events（agent.exec / agent.sftp.*）。
安全: 插件安全模型同档——外部智能体 = 可信代码；危险命令审批是后续增强项。
"""
import base64
import hashlib
import hmac
import os
import re
import shlex
import time

import paramiko
from flask import Blueprint, jsonify, request

import config
from modules import events
from modules import ssh_hosts as hosts

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 600
MAX_OUTPUT_BYTES = 2 * 1024 * 1024

blueprint = Blueprint('agent', __name__)


# ---------------------------------------------------------------- 令牌

SCOPE_FULL = 'full'
SCOPE_UPLOAD = 'upload'


def tokenHash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def agentHasScope(agent, scope):
    """令牌是否持有 scope。没写 scopes（或为空列表）= 全权，向后兼容；
    显式 'full' 同样视为全权；写了 scopes 则按白名单精确匹配。"""
    scopes = agent.get('scopes')
    if not isinstance(scopes, list) or not scopes:
        return True
    names = [str(s).strip() for s in scopes if str(s).strip()]
    return scope in names or SCOPE_FULL in names


def scopeDenied(agent, scope):
    """scope 不满足时返回 403 应答；满足返回 None。

    供无逐端点守卫的入口（authGuard 的 /api/tasks*）复用；越权尝试写审计，
    记令牌名与所需 scope。"""
    if agentHasScope(agent, scope):
        return None
    events.log('agent.scope', agent.get('name') or '', 'deny',
               'need=%s' % scope, request.remote_addr)
    return jsonify({'error': '令牌无此权限', 'need': scope}), 403


def _tokenFromRequest():
    token = (request.headers.get('X-Agent-Token') or '').strip()
    if not token:
        auth = request.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            token = auth[7:].strip()
    return token


def agentFromRequest():
    """返回命中的令牌条目；未命中返回 None。"""
    token = _tokenFromRequest()
    if not token:
        return None
    digest = tokenHash(token)
    for item in config.getAgentTokens():
        stored = str(item.get('tokenHash') or '')
        if stored and hmac.compare_digest(stored, digest):
            return dict(item)
    # 设备令牌（App 密码换发, kind='device', 固定 upload）独立存储在
    # devices.json, 经同一入口鉴权: upload 放行, full 端点照常 403。
    from modules import device_auth
    return device_auth.agentFromDevices(digest)


def _agentRequired(scope=SCOPE_FULL):
    """守卫: 无有效令牌 401；令牌缺所需 scope 403；通过返回令牌条目。

    缺省要求 full——现有 /api/agent/* 端点（exec/sftp/sessions/skills）
    不改调用即收紧到全权令牌；新端点显式传自己的 scope（如上传=upload）。
    """
    agent = agentFromRequest()
    if agent is None:
        return None, (jsonify({'error': 'unauthorized (需要 X-Agent-Token)'}), 401)
    denied = scopeDenied(agent, scope)
    if denied is not None:
        return None, denied
    return agent, None


# ---------------------------------------------------------------- 主机解析

def _resolveHost(nameOrId):
    entry = hosts.findHost(nameOrId)
    if entry is not None:
        return entry
    wanted = str(nameOrId or '').strip().lower()
    for item in hosts.repo.all():
        if str(item.get('name') or '').strip().lower() == wanted:
            return item
    return None


def _safeHosts():
    out = []
    for item in hosts.repo.all():
        out.append({'id': item.get('id'), 'name': item.get('name'),
                    'host': item.get('host'), 'port': item.get('port')})
    # Deskport 本机也是一台主机（§7b 全等价）；agentLocalEnabled=false 时不出现
    from modules.agent_local import localEnabled, localHostEntry
    if localEnabled():
        out.append(localHostEntry())
    return out


# ---------------------------------------------------------------- 执行

def parseTimeoutSec(data):
    """timeoutSec 校验：缺省 30；非整数或越界(1–600)返回错误说明。"""
    raw = data.get('timeoutSec')
    if raw is None or raw == '':
        return DEFAULT_TIMEOUT, None
    if isinstance(raw, bool):
        return None, 'timeoutSec 必须是 1–600 的整数'
    if isinstance(raw, str):
        try:
            raw = int(raw.strip())
        except ValueError:
            return None, 'timeoutSec 必须是 1–600 的整数'
    if isinstance(raw, float):
        if raw != int(raw):
            return None, 'timeoutSec 必须是 1–600 的整数'
        raw = int(raw)
    if not isinstance(raw, int):
        return None, 'timeoutSec 必须是 1–600 的整数'
    if raw < 1 or raw > MAX_TIMEOUT:
        return None, 'timeoutSec 超出范围（1–%d）' % MAX_TIMEOUT
    return raw, None


def _buildExecScript(command, cwd):
    """把用户命令包进服务端可控的外壳（cd / UTF-8 locale / setsid 进程组）。

    用户命令经 base64 进入内层 eval——引号、heredoc、多行、NUL 之外的任意
    内容都不会破坏外壳语法；外壳每一行都是服务端固定文本，不拼接用户输入。
    setsid 让命令独立成进程组：超时可整组击杀；无 setsid 的系统退化为直跑。
    """
    b64 = base64.b64encode(command.encode('utf-8')).decode('ascii')
    inner = ('printf "__PID__%%s\\n" "$$"; '
             'eval "$(printf %%s %s | base64 -d)"' % b64)
    quoted = shlex.quote(inner)
    lines = ['export LANG=C.UTF-8 LC_ALL=C.UTF-8']
    if cwd:
        lines.append('cd -- ' + shlex.quote(cwd) + ' || exit')
    lines.append('if command -v setsid >/dev/null 2>&1; then')
    lines.append('  setsid -w bash -c ' + quoted)
    lines.append('else')
    lines.append('  bash -c ' + quoted)
    lines.append('fi')
    return '\n'.join(lines)


def _killRemoteGroup(transport, pid):
    """超时后整组击杀：先 TERM，1 秒后兜底 KILL（nohup 免被会话回收带走）。"""
    if not pid:
        return
    try:
        ch = transport.open_session(timeout=5)
        ch.exec_command(
            'kill -TERM -- -%d 2>/dev/null; '
            'nohup sh -c \'sleep 1; kill -KILL -- -%d 2>/dev/null\' '
            '>/dev/null 2>&1 &' % (pid, pid))
        time.sleep(0.15)
        ch.close()
    except Exception:
        pass


def _execOnHost(hostEntry, command, timeoutSec, cwd=''):
    """paramiko exec_command: 非 pty 通道, 轮询读双流, 到总时限掐断。

    stdin 立即 EOF（读 stdin 的命令不再挂起）；超时返回 exit=null 并整组
    击杀远端进程；stdout 里的 __PID__ 标记行是外壳产物，出参前剥离。
    """
    client = hosts.getConnection(hostEntry)
    transport = client.get_transport()
    channel = transport.open_session(timeout=10)
    channel.settimeout(1.0)
    channel.exec_command(_buildExecScript(command, cwd))
    channel.shutdown_write()
    stdoutBuf = bytearray()
    stderrBuf = bytearray()
    deadline = time.monotonic() + timeoutSec
    timedOut = False
    while True:
        got = False
        if channel.recv_ready():
            stdoutBuf += channel.recv(65536)
            got = True
        if channel.recv_stderr_ready():
            stderrBuf += channel.recv_stderr(65536)
            got = True
        if channel.exit_status_ready() and not channel.recv_ready() \
                and not channel.recv_stderr_ready():
            break
        if time.monotonic() >= deadline:
            timedOut = True
            pid = None
            m = re.search(rb'__PID__(\d+)\r?\n', bytes(stdoutBuf))
            if m:
                pid = int(m.group(1))
            _killRemoteGroup(transport, pid)
            break
        if not got:
            time.sleep(0.05)
    while channel.recv_ready():
        stdoutBuf += channel.recv(65536)
    while channel.recv_stderr_ready():
        stderrBuf += channel.recv_stderr(65536)
    # 剥离外壳打印的 PID 标记行
    m = re.search(rb'__PID__(\d+)\r?\n', bytes(stdoutBuf))
    pid = int(m.group(1)) if m else None
    if m:
        del stdoutBuf[m.start():m.end()]
    exitCode = channel.recv_exit_status() if channel.exit_status_ready() else None
    return {
        'exit': None if timedOut else exitCode,
        'out': bytes(stdoutBuf), 'err': bytes(stderrBuf),
        'timedOut': timedOut, 'reason': 'timeout' if timedOut else '',
        'pid': pid,
        'truncated': max(len(stdoutBuf), len(stderrBuf)) > MAX_OUTPUT_BYTES,
    }


@blueprint.route('/api/agent/hosts')
def agentHosts():
    agent, err = _agentRequired()
    if agent is None:
        return err
    return jsonify({'hosts': _safeHosts(), 'tokenName': agent.get('name') or ''})


@blueprint.route('/api/agent/ssh/exec', methods=['POST'])
def agentExec():
    agent, err = _agentRequired()
    if agent is None:
        return err
    data = request.get_json(silent=True) or {}
    from modules.agent_local import isLocalHost as _isLocalHost, localEnabled
    if _isLocalHost(data.get('host')):
        if not localEnabled():
            return jsonify({'error': '主机不存在'}), 404
        from modules.agent_local import agentLocalExec as _localExecRoute
        return _localExecRoute()
    rawCommand = data.get('command')
    command = str(rawCommand or '').strip()
    cwd = str(data.get('cwd') or '').strip()
    if not command:
        return jsonify({'error': '缺少 command'}), 400
    if isinstance(rawCommand, str) and '\x00' in rawCommand:
        return jsonify({'error': 'command 含 NUL 字符'}), 400
    if len(cwd) > 500:
        return jsonify({'error': 'cwd 过长'}), 400
    encoding = str(data.get('encoding') or 'text').lower()
    if encoding not in ('text', 'base64'):
        return jsonify({'error': 'encoding 只能是 text / base64'}), 400
    timeoutSec, timeoutErr = parseTimeoutSec(data)
    if timeoutErr:
        return jsonify({'error': timeoutErr}), 400

    hostEntry = _resolveHost(str(data.get('host') or ''))
    if hostEntry is None:
        names = [h.get('name') or h.get('id') for h in _safeHosts()][:8]
        return jsonify({'error': '主机不存在', 'availableHosts': names}), 404
    hostName = hostEntry.get('name') or hostEntry.get('host') or ''

    started = time.monotonic()
    try:
        result = _execOnHost(hostEntry, command, timeoutSec, cwd)
    except paramiko.SSHException as e:
        events.log('agent.exec', hostName, 'fail', command[:160], request.remote_addr)
        return jsonify({'error': 'SSH 执行失败: %s' % e}), 502
    durationMs = int((time.monotonic() - started) * 1000)

    events.log('agent.exec', hostName,
               result['reason'] or 'ok', command[:160], request.remote_addr)
    body = {'exit': result['exit'],
            'timedOut': result['timedOut'], 'reason': result['reason'],
            'outTruncated': result['truncated'],
            'durationMs': durationMs, 'timeoutSec': timeoutSec,
            'pid': result['pid']}
    if encoding == 'base64':
        body['outB64'] = base64.b64encode(result['out'][:MAX_OUTPUT_BYTES]).decode('ascii')
        body['errB64'] = base64.b64encode(result['err'][:MAX_OUTPUT_BYTES]).decode('ascii')
    else:
        body['out'] = result['out'][:MAX_OUTPUT_BYTES].decode('utf-8', 'replace')
        body['err'] = result['err'][:MAX_OUTPUT_BYTES].decode('utf-8', 'replace')
    return jsonify(body)


# ---------------------------------------------------------------- SFTP
# SFTP list/get/put 在 modules/agent_sftp.py（同蓝图, app.py 显式登记）。


# ---------------------------------------------------------------- 技能分发

def _skillsDir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        '.agents', 'skills')


@blueprint.route('/api/agent/skills')
def agentSkillsList():
    """可分发的技能清单（供外部智能体自举与检测更新）。"""
    agent, err = _agentRequired()
    if agent is None:
        return err
    root = _skillsDir()
    skills = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            skillFile = os.path.join(root, name, 'SKILL.md')
            if os.path.isfile(skillFile):
                try:
                    stat = os.stat(skillFile)
                except OSError:
                    continue
                folder = os.path.dirname(skillFile)
                extras = sorted(f for f in os.listdir(folder)
                                if f != 'SKILL.md' and os.path.isfile(os.path.join(folder, f)))
                skills.append({'name': name, 'size': stat.st_size,
                               'mtime': int(stat.st_mtime), 'files': extras})
    return jsonify({'skills': skills})


@blueprint.route('/api/agent/skills/<name>')
def agentSkillGet(name):
    """返回技能原文（text/markdown），供智能体拉取安装到本地技能目录。"""
    agent, err = _agentRequired()
    if agent is None:
        return err
    if not re.match(r'^[A-Za-z0-9_-]{1,40}$', name):
        return jsonify({'error': '名称不合法'}), 400
    skillFile = os.path.join(_skillsDir(), name, 'SKILL.md')
    if not os.path.isfile(skillFile):
        return jsonify({'error': '技能不存在', 'available':
                        [s for s in os.listdir(_skillsDir())
                         if os.path.isfile(os.path.join(_skillsDir(), s, 'SKILL.md'))]
                        if os.path.isdir(_skillsDir()) else []}), 404
    with open(skillFile, 'r', encoding='utf-8') as f:
        body = f.read()
    resp = jsonify({'name': name, 'content': body})
    resp.headers['X-Skill-Name'] = name
    return resp
