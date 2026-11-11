# -*- coding: utf-8 -*-
"""Kimi Code ACP 客户端（极小实现）— JSON-RPC over stdio, newline-delimited。

只覆盖平台需要的子集：initialize / session/new / session/set_mode /
session/prompt / session/update / session/request_permission / session/cancel。
不依赖 ACP SDK，避免生产 venv 额外安装依赖。
"""
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import threading

log = logging.getLogger('kimi-acp')

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
SESSION_ID_RE = re.compile(r'session_[0-9a-f-]{36}')


def _kimiBin():
    for cand in ('kimi.exe', 'kimi'):
        path = shutil.which(cand)
        if path:
            return path
    raise RuntimeError('找不到 kimi 命令')


def forkSession(sessionId, timeout=90):
    """kimi fork：把已有会话复制为新会话并返回新 id（反馈第二轮 §3）。

    默认 fork 而非直接接管——避免与用户正开着的终端写同一个会话
    （kimi 有会话锁，占用中的会话 fork 会直接报错，原文透传给调用方）。
    """
    proc = subprocess.run(
        [_kimiBin(), 'fork', '-y', sessionId], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=timeout,
        creationflags=CREATE_NO_WINDOW)
    out = (proc.stdout or '') + (proc.stderr or '')
    if proc.returncode != 0:
        raise RuntimeError('fork 失败: %s' % (out.strip()[:300] or proc.returncode))
    m = SESSION_ID_RE.search(out)
    if not m:
        raise RuntimeError('fork 输出里没有新会话 id: %s' % out.strip()[:200])
    return m.group(0)


def listSessions(limit=30, cwd=None):
    """kimi session list --all --json → 智能体可挑的接手候选（按最近更新排序）。

    cwd 给定时做前缀过滤（大小写不敏感）；kimi 不可用抛 RuntimeError。
    """
    proc = subprocess.run(
        [_kimiBin(), 'session', 'list', '--all', '--json'], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=60,
        creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError('session list 失败: %s' % (proc.stderr or '').strip()[:300])
    try:
        raw = json.loads(proc.stdout or '[]')
    except ValueError:
        raise RuntimeError('session list 输出不是 JSON')
    wanted = str(cwd or '').strip()
    if wanted:
        wanted = wanted.replace('/', '\\').rstrip('\\').lower()
    items = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not entry.get('id'):
            continue
        workDir = str(entry.get('workDir') or '')
        if wanted:
            norm = workDir.replace('/', '\\').rstrip('\\').lower()
            if norm != wanted and not norm.startswith(wanted + '\\'):
                continue
        items.append({'id': str(entry['id']), 'title': str(entry.get('title') or '')[:120],
                      'lastPrompt': str(entry.get('lastPrompt') or '')[:160],
                      'workDir': workDir,
                      'updatedAt': entry.get('updatedAt') or 0})
    items.sort(key=lambda x: -(x['updatedAt'] or 0))
    return items[:max(1, min(int(limit), 100))]


class AcpError(Exception):
    pass


class KimiAcp:
    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=1800, model=''):
        self.cwd = cwd
        self.on_update = on_update or (lambda update: None)
        self.on_permission = on_permission or self._defaultPermission
        self.mode = mode
        self.model = str(model or '').strip()
        self.prompt_timeout = prompt_timeout
        self.proc = None
        self.session_id = None
        self.agent_info = {}
        self.capabilities = {}
        self.stderr_lines = []
        self._id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._reader = None
        self._errReader = None
        self._closed = False
        self._loading = False      # session/load 进行中（回放窗口，R4-5a）
        self.replayCount = 0       # 窗口内被门禁挡下的回放 update 条数
        self.replayPerms = 0       # 窗口内被直接取消的回放审批条数

    # ---- transport ----
    def _send(self, obj):
        with self._lock:
            if not self.proc or not self.proc.stdin:
                raise AcpError('ACP 进程未启动')
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def request(self, method, params, timeout=120):
        fut = concurrent.futures.Future()
        with self._lock:
            self._id += 1
            reqId = self._id
            self._pending[reqId] = fut
        self._send({'jsonrpc': '2.0', 'id': reqId, 'method': method, 'params': params})
        try:
            # timeout=0 表示不限时（常驻会话等待用户/模型）
            return fut.result(timeout=(timeout or None))
        except concurrent.futures.TimeoutError:
            raise AcpError('ACP 请求超时: %s' % method)
        finally:
            with self._lock:
                self._pending.pop(reqId, None)

    def _readLoop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    log.warning('ACP 非 JSON 输出: %s', line[:200])
                    continue
                self._handleMessage(obj)
        except Exception as e:
            log.warning('ACP reader stopped: %s', e)
        finally:
            with self._lock:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(AcpError('ACP 进程已退出'))
                self._pending.clear()

    def _handleMessage(self, obj):
        """分发一条 ACP 消息；session/load 回放窗口内的 update 与旧审批
        不外发（R4-5a）：update 计数不转发，旧审批直接回 cancelled。"""
        if obj.get('id') is not None and 'method' not in obj:
            with self._lock:
                fut = self._pending.get(obj.get('id'))
            if fut and not fut.done():
                if obj.get('error'):
                    fut.set_exception(AcpError(str(obj['error'].get('message') or obj['error'])))
                else:
                    fut.set_result(obj.get('result') or {})
            return
        method = obj.get('method')
        if method == 'session/update':
            update = (obj.get('params') or {}).get('update') or {}
            if self._loading:
                self.replayCount += 1
                return
            try:
                self.on_update(update)
            except Exception as e:
                log.warning('ACP update handler error: %s', e)
        elif method == 'session/request_permission':
            params = obj.get('params') or {}
            if self._loading:
                self.replayPerms += 1
                self._send({'jsonrpc': '2.0', 'id': obj.get('id'),
                            'result': {'outcome': {'outcome': 'cancelled'}}})
                return
            optionId = None
            try:
                optionId = self.on_permission(params, obj.get('id'))
            except TypeError:
                optionId = self.on_permission(params)
            except Exception as e:
                log.warning('ACP permission handler error: %s', e)
            if optionId:
                self._send({'jsonrpc': '2.0', 'id': obj.get('id'),
                            'result': {'outcome': {'outcome': 'selected',
                                                   'optionId': optionId}}})
            else:
                self._send({'jsonrpc': '2.0', 'id': obj.get('id'),
                            'result': {'outcome': {'outcome': 'cancelled'}}})
        elif method:
            log.info('ACP 未处理的 agent 请求: %s', method)
            self._send({'jsonrpc': '2.0', 'id': obj.get('id'),
                        'error': {'code': -32601, 'message': 'method not found'}})

    def _readStderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip())
                if len(self.stderr_lines) > 200:
                    del self.stderr_lines[:100]
        except Exception:
            pass

    @staticmethod
    def _defaultPermission(params):
        options = params.get('options') or []
        for kind in ('allow_always', 'allow_once'):
            for opt in options:
                if opt.get('kind') == kind:
                    return opt.get('optionId')
        return None

    # ---- lifecycle ----
    def start(self, session_id=None):
        try:
            kimi = _kimiBin()
        except RuntimeError:
            raise AcpError('找不到 kimi 命令')
        cmd = [kimi]
        if self.model:
            cmd += ['--model', self.model]
        cmd.append('acp')
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', bufsize=1, cwd=self.cwd,
            creationflags=CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._readLoop, daemon=True)
        self._reader.start()
        self._errReader = threading.Thread(target=self._readStderr, daemon=True)
        self._errReader.start()
        init = self.request('initialize', {
            'protocolVersion': 1,
            'clientCapabilities': {},
            'clientInfo': {'name': 'wp-platform', 'version': '1.0'},
        }, timeout=20)
        self.agent_info = init.get('agentInfo') or {}
        self.capabilities = init.get('agentCapabilities') or {}
        if session_id:
            # 回放窗口 = session/load 请求在途（R4-5a #4：load 返回即结束）
            self._loading = True
            try:
                loaded = self.request('session/load', {
                    'sessionId': session_id, 'cwd': self.cwd,
                    'mcpServers': [], 'additionalDirectories': [],
                }, timeout=60)
            finally:
                self._loading = False
            self.session_id = session_id
            self.modes = loaded.get('modes') or {}
            if self.mode:
                try:
                    self.set_mode(self.mode)
                except AcpError as e:
                    log.info('ACP set_mode %s 失败(忽略): %s', self.mode, e)
        else:
            session = self.request('session/new', {
                'cwd': self.cwd, 'mcpServers': [], 'additionalDirectories': [],
            }, timeout=30)
            self.session_id = session.get('sessionId')
            if not self.session_id:
                raise AcpError('ACP session/new 未返回 sessionId')
            self.modes = session.get('modes') or {}
            if self.mode:
                try:
                    self.set_mode(self.mode)
                except AcpError as e:
                    log.info('ACP set_mode %s 失败(忽略): %s', self.mode, e)
        return self.session_id

    def set_mode(self, mode):
        if not self.session_id:
            raise AcpError('会话未建立')
        self.mode = mode
        return self.request('session/set_mode',
                            {'sessionId': self.session_id, 'modeId': mode},
                            timeout=10)

    def prompt(self, text, timeout=None):
        return self.request('session/prompt', {
            'sessionId': self.session_id,
            'prompt': [{'type': 'text', 'text': text}],
        }, timeout=timeout or self.prompt_timeout)

    def cancel(self):
        if not self.session_id:
            return
        try:
            self._send({'jsonrpc': '2.0', 'method': 'session/cancel',
                        'params': {'sessionId': self.session_id}})
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.cancel()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

    def shutdown(self):
        """stdio 版进程本就在 close 里 terminate；接口对齐 RemoteAcpClient。"""
        self.close()

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def poll(self):
        return self.proc.poll() if self.proc else 1


def extractText(content):
    """ACP content block(s) → 纯文本。"""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get('type') == 'text':
            return content.get('text') or ''
        return json.dumps(content, ensure_ascii=False)[:2000]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                inner = item.get('content')
                if isinstance(inner, dict) and inner.get('type') == 'text':
                    parts.append(inner.get('text') or '')
                elif inner is not None:
                    parts.append(extractText(inner))
        return ''.join(parts)
    return str(content)