# -*- coding: utf-8 -*-
"""反馈 3fa7b644e7b8：CLI task --wait 的 None/0/N 三态回归。

旧 _waitTask 的 `end = time.time() + (seconds or 900)` 把 None 与 0 都吞成
900——不传 --wait 或传 0 都会阻塞到执行方回合结束（最多 900s）。现在
None=缺省等 900s、0/--no-wait=建完即退、正数=等 N 秒（封顶 900）。

进程内：假时钟 + 假 Task 测 _waitTask 全部分支（None→900、0→立即返回、N→N、
>900 封顶、负数按 0）；桩服务器：真跑 CLI 冒烟 --wait 0 / --no-wait 秒退、
缺省仍等、--wait N 超时退 3、wait 子命令 --timeout 0 同口径。不建真任务、
不碰线上（令牌只进请求头）。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, '.agents', 'skills', 'deskport', 'deskport.py')
TOKEN = 'tok-wait-synthetic-0123456789abcdef'   # 合成令牌，只进请求头


def _loadClient():
    """把 deskport.py 当模块加载（单文件客户端，导入无副作用）。

    必须先登记 sys.modules：文件里的 @dataclass 在类创建时要按 __module__
    找回模块命名空间，不登记会炸在 dataclasses 内部。
    """
    spec = importlib.util.spec_from_file_location('deskport_wait_under_test', CLIENT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Clock:
    """假时钟：冻结在 t0，由假 Task 的轮询推进——不真睡，毫秒级跑完。"""

    def __init__(self, t0=1000.0):
        self.t = t0

    def time(self):
        return self.t


class _FakeTask:
    """假 Task：wait() 按脚本回应；每次调用把时钟推进 timeout（模拟一轮轮询耗尽）。"""

    def __init__(self, clock, script):
        self.clock, self.script = clock, list(script)
        self.id = 't-fake-1'
        self.timeouts = []

    def wait(self, poll=2.5, timeout=1800, on_event=None, on_permission=None):
        self.timeouts.append(timeout)
        self.clock.t += timeout
        step = self.script.pop(0) if self.script else 'timeout'
        if step == 'timeout':
            raise TimeoutError('fake: 一轮轮询耗尽')
        return {'status': step, 'finalText': '最终回复' if step == 'waiting' else ''}

    def permission(self, *a, **kw):
        raise AssertionError('本用例不该走到审批')


def test_waitTask_none_means_900(monkeypatch, capsys):
    """None（不传 --wait）= 缺省等 900s：带 900s 预算进轮询，拿到 waiting 退 0。"""
    dp = _loadClient()
    clock = _Clock()
    monkeypatch.setattr(_time, 'time', clock.time)
    task = _FakeTask(clock, ['waiting'])
    assert dp._waitTask(task, None, False, False) == 0
    assert task.timeouts == [900]
    assert '最终回复' in capsys.readouterr().out


def test_waitTask_zero_returns_immediately(capsys):
    """0（--wait 0 / --no-wait）= 建完即退：一次轮询都不发起，退 0、无输出。"""
    dp = _loadClient()
    task = _FakeTask(_Clock(), ['waiting'])
    t0 = _time.time()
    assert dp._waitTask(task, 0, False, False) == 0
    assert _time.time() - t0 < 1
    assert task.timeouts == []
    assert capsys.readouterr().out == ''


def test_waitTask_n_waits_n_seconds(monkeypatch, capsys):
    """正数 N = 等 N 秒（不是 900）：首轮轮询预算就是 N，超时打 TIMEOUT 退 3。"""
    dp = _loadClient()
    clock = _Clock()
    monkeypatch.setattr(_time, 'time', clock.time)
    task = _FakeTask(clock, [])          # 一直没结束
    assert dp._waitTask(task, 30, False, False) == 3
    assert task.timeouts[0] == 30
    err = capsys.readouterr().err
    assert 'TIMEOUT' in err and '30s' in err


def test_waitTask_caps_at_900(monkeypatch, capsys):
    """>900 封顶（与 wait 端点契约一致）：要等 5000s 也只给 900s 预算。"""
    dp = _loadClient()
    clock = _Clock()
    monkeypatch.setattr(_time, 'time', clock.time)
    task = _FakeTask(clock, [])
    assert dp._waitTask(task, 5000, False, False) == 3
    assert task.timeouts[0] == 900
    assert '900s' in capsys.readouterr().err


def test_waitTask_negative_treated_as_zero(capsys):
    """负数（argparse type=int 拦不住）按 0 处理：即退，不傻等。"""
    dp = _loadClient()
    task = _FakeTask(_Clock(), ['waiting'])
    assert dp._waitTask(task, -5, False, False) == 0
    assert task.timeouts == []
    assert capsys.readouterr().out == ''


class _Handler(BaseHTTPRequestHandler):
    """假 Deskport：建任务 + 事件/详情；任务状态由用例切（running=执行方还没结束）。"""

    status = 'waiting'

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/api/tasks/') and self.path.endswith('/events'):
            self._json({'events': []})
        elif self.path.startswith('/api/tasks/'):
            self._json({'task': {'id': 't-wait-1', 'status': _Handler.status,
                                 'finalText': '最终回复'}})
        else:
            self._json({'error': 'not found'})

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/api/tasks':
            self._json({'task': {'id': 't-wait-1', 'status': 'queued'}}, 202)
        else:
            self._json({'error': 'not found'})

    def log_message(self, *a):    # 别把请求日志泼进测试输出
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d' % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _runCli(tmp_path, server, *args):
    """在干净目录里真跑 CLI（.deskport.json 指向假服务器，令牌在 .agent-token）。"""
    d = tmp_path / ('cli-%d' % len(args))
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLIENT, d / 'deskport.py')
    (d / '.agent-token').write_text(TOKEN, encoding='utf-8')
    (d / '.deskport.json').write_text(
        json.dumps({'url': server, 'node': 'local'}, ensure_ascii=False),
        encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.endswith('_PROXY')}
    for k in ('DESKPORT_TOKEN', 'DESKPORT_URL', 'DESKPORT_NODE'):
        env.pop(k, None)
    t0 = _time.time()
    p = subprocess.run([sys.executable, 'deskport.py', *args], cwd=str(d), env=env,
                       capture_output=True, encoding='utf-8', errors='replace',
                       timeout=90)
    return p.returncode, p.stdout, p.stderr, _time.time() - t0


def test_cli_task_wait_zero_exits_immediately(server, tmp_path):
    """--wait 0：建完打印 id 即退（旧代码会傻等到回合结束/900s）。"""
    _Handler.status = 'running'          # 执行方还没结束
    code, out, err, secs = _runCli(tmp_path, server, 'task', '立即结束本轮', '--wait', '0')
    assert code == 0, err
    assert out.splitlines() == ['t-wait-1']   # 只有 id，没有最终回复
    assert secs < 10                        # 秒退，不是 900s


def test_cli_task_no_wait_is_alias_of_zero(server, tmp_path):
    """--no-wait = --wait 0 的别名（CLI 惯例）。"""
    _Handler.status = 'running'
    code, out, err, secs = _runCli(tmp_path, server, 'task', '立即结束本轮', '--no-wait')
    assert code == 0, err
    assert out.splitlines() == ['t-wait-1']
    assert secs < 10


def test_cli_task_without_wait_still_waits(server, tmp_path):
    """不传 --wait：缺省仍等它结束并打印最终回复（None=900 的行为没变）。"""
    _Handler.status = 'waiting'
    code, out, err, _secs = _runCli(tmp_path, server, 'task', '普通派活')
    assert code == 0, err
    assert out.splitlines() == ['t-wait-1', '最终回复']


def test_cli_task_wait_n_times_out(server, tmp_path):
    """--wait 1：等 1 秒没结束 → TIMEOUT 退出 3（N→N 的 CLI 侧证据）。"""
    _Handler.status = 'running'
    code, _out, err, _secs = _runCli(tmp_path, server, 'task', '长活', '--wait', '1')
    assert code == 3
    assert 'TIMEOUT' in err and '1s' in err


def test_cli_wait_subcommand_timeout_zero(server, tmp_path):
    """wait 子命令同口径：--timeout 0 也即退（共享 _waitTask 的修复）。"""
    _Handler.status = 'running'
    code, _out, err, secs = _runCli(tmp_path, server, 'wait', 't-wait-1', '--timeout', '0')
    assert code == 0, err
    assert secs < 10
