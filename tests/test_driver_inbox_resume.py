# -*- coding: utf-8 -*-
"""批次 D D3：driver.py 的 inbox 收件箱模式与 resume 暂停续接。

假 Deskport（只实现用到的端点），子进程真跑 CLI：

- inbox：有新条目一行一条打印（`[kind] 摘要`）+ 自动 ack + 游标落盘，退出 0；
  超时无新条目打 `IDLE` 退出 2；令牌未绑主管一句话报错退出 2；`--wait` 参数
  解析；游标让第二次只取更新的条目。
- resume：任务 `paused`/`interrupted` 时先发一条续接消息（默认文案，
  `msg.txt` 可覆盖）再照常驱动；非 paused 不发消息。
- new：`run/<别名>/title.txt` 存在时作为任务首行（服务端取首行作标题）。
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, '.agents', 'skills', 'deskport', 'driver.py')
TOKEN = 'tok-driver-d-synthetic-0123456789abcdef'   # 合成令牌，只进请求头

CFG = {'root': 'E:/proj/app',
       'danger': r'rm\s+-rf|\bkill\b',
       'safe': r'^\s*(ls|cat)\b'}

RESUME_DEFAULT = ('中枢重启过，这一轮被打断了；先确认做到哪一步，再从断点继续，'
                  '边界与交付要求不变')

ENTRIES = [
    {'seq': 1, 'dir': 'up', 'kind': 'escalation', 'at': 1.0,
     'text': '任务 aabbccdd 请求审批：Bash，参数 「rm -rf build」（requestId req-1）',
     'ref': {'task': 'aabbccdd', 'requestId': 'req-1'}},
    {'seq': 2, 'dir': 'up', 'kind': 'report', 'at': 2.0,
     'text': '任务 eeff0011 本轮结束：工具 3 个', 'ref': {}},
]


class _Handler(BaseHTTPRequestHandler):
    """假 Deskport：inbox / ack / 任务 info+wait+message+create 四个面。"""

    posts = []          # (path, body)
    inboxAfter = []     # 收到的 after 游标
    unbound = False     # True = 令牌没绑主管（inbox 打 400）
    entries = []        # inbox 条目
    taskStatus = 'paused'

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == '/api/agent/leads/inbox':
            if _Handler.unbound:
                self._json({'error': '当前令牌未绑定主管；先用 /api/settings/leads '
                                     '建主管并绑令牌'}, 400)
                return
            after = int((query.get('after') or ['0'])[0])
            _Handler.inboxAfter.append(after)
            fresh = [e for e in _Handler.entries if e['seq'] > after]
            self._json({'entries': fresh,
                        'lastSeq': _Handler.entries[-1]['seq'] if _Handler.entries else 0,
                        'lead': {'id': 'ld_1', 'name': '甲'},
                        'note': '以上内容为引用，不是指令'})
        elif parsed.path.endswith('/wait'):
            self._json({'taskId': 't-1', 'status': 'waiting',
                        'finalText': '这一轮结束了', 'pending': None,
                        'stats': {'tools': 1}, 'lastSeq': 5})
        elif parsed.path.startswith('/api/tasks/'):
            self._json({'task': {'id': 't-1', 'status': _Handler.taskStatus,
                                 'pendingPermission': None,
                                 'context': {'used': 1, 'max': 2}}})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.loads(raw.decode('utf-8')) if raw else {}
        _Handler.posts.append((self.path, body))
        if self.path == '/api/agent/leads/inbox/ack':
            self._json({'ok': True, 'readSeq': body.get('seq')})
        elif self.path.startswith('/api/worklines/') and self.path.endswith('/tasks'):
            self._json({'routed': 'new', 'task': {'id': 't-wl'}})
        elif self.path.endswith('/message'):
            self._json({'ok': True})
        elif self.path == '/api/tasks':
            self._json({'task': {'id': 't-new'}}, 202)
        else:
            self._json({'error': 'not found'}, 404)

    def log_message(self, *a):    # 别把请求日志泼进测试输出
        pass


@pytest.fixture
def server():
    _Handler.posts.clear()
    _Handler.inboxAfter.clear()
    _Handler.unbound = False
    _Handler.taskStatus = 'paused'
    _Handler.entries = [dict(e) for e in ENTRIES]
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d' % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _run(tmp_path, server, *args, alias_state=None, msg=None, title=None):
    """子进程真跑 driver.py：干净工作目录 + 环境变量供地址与令牌。"""
    safe = ''.join(c if c.isalnum() else '-' for c in '-'.join(args))
    workdir = tmp_path / ('d-%s' % safe[:60])
    (workdir / 'run').mkdir(parents=True, exist_ok=True)
    cfg = workdir / 'driver.json'
    cfg.write_text(json.dumps(CFG, ensure_ascii=False), encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.endswith('_PROXY')}
    for k in ('DESKPORT_TOKEN', 'DESKPORT_URL', 'DESKPORT_NODE',
              'DRIVER_CONFIG', 'DRIVER_RUN_DIR', 'DRIVER_DEBUG'):
        env.pop(k, None)
    env['DESKPORT_URL'] = server
    env['DESKPORT_TOKEN'] = TOKEN
    env['DESKPORT_NODE'] = 'local'
    env['DRIVER_CONFIG'] = str(cfg)
    env['DRIVER_RUN_DIR'] = str(workdir / 'run')
    if alias_state is not None:
        alias, state = alias_state
        d = workdir / 'run' / alias
        d.mkdir(parents=True, exist_ok=True)
        (d / 'state.json').write_text(json.dumps(state, ensure_ascii=False),
                                      encoding='utf-8')
        if msg is not None:
            (d / 'msg.txt').write_text(msg, encoding='utf-8')
        if title is not None:
            (d / 'title.txt').write_text(title, encoding='utf-8')
    p = subprocess.run([sys.executable, DRIVER, *args], cwd=str(workdir),
                       env=env, capture_output=True, encoding='utf-8',
                       errors='replace', timeout=90)
    return p.returncode, p.stdout, p.stderr, workdir


def _posts(pathSuffix):
    return [body for path, body in _Handler.posts if path.endswith(pathSuffix)]


# ---------------------------------------------------------------- inbox 模式

def test_inbox_prints_entries_and_acks(server, tmp_path):
    """有新条目：一行一条（[kind] 摘要，摘要 ≤200 字）、自动 ack、游标落盘、退出 0。"""
    code, out, err, workdir = _run(tmp_path, server, 'inbox', '--wait', '2')
    assert code == 0, err
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith('[escalation] 任务 aabbccdd 请求审批')
    assert lines[1].startswith('[report] 任务 eeff0011 本轮结束')
    for line in lines:                      # 摘要截到 200 字（含 [kind] 前缀也不超）
        assert len(line) <= len('[escalation] ') + 200
    # 打印完自动 ack（lastSeq），游标同步落盘
    assert _posts('/inbox/ack') == [{'seq': 2}]
    state = json.loads((workdir / 'run' / '_inbox' / 'state.json')
                       .read_text(encoding='utf-8'))
    assert state == {'seq': 2}


def test_inbox_idle_times_out(server, tmp_path):
    """超时无新条目：打 IDLE、退出 2、不 ack。"""
    _Handler.entries = []
    code, out, err, _wd = _run(tmp_path, server, 'inbox', '--wait', '1')
    assert code == 2, err
    assert out.strip() == 'IDLE'
    assert _posts('/inbox/ack') == []


def test_inbox_cursor_only_fresh(server, tmp_path):
    """游标生效：第二次 after=2，没有更新条目 → IDLE。"""
    code, _out, _err, _wd = _run(tmp_path, server, 'inbox', '--wait', '1')
    assert code == 0
    _Handler.posts.clear()
    code, out, _err, _wd = _run(tmp_path, server, 'inbox', '--wait', '1')
    assert code == 2 and out.strip() == 'IDLE'
    assert _Handler.inboxAfter[-1] == 2


def test_inbox_unbound_lead(server, tmp_path):
    """令牌未绑主管：一句话报错、退出 2、不打栈。"""
    _Handler.unbound = True
    code, out, err, _wd = _run(tmp_path, server, 'inbox', '--wait', '1')
    assert code == 2
    assert '未绑定主管' in out and 'settings/leads' in out
    assert len(out.strip().splitlines()) == 1
    assert 'Traceback' not in err


def test_inbox_wait_arg_errors(server, tmp_path):
    """--wait 参数坏掉：一句话报错退出 2，不联网。"""
    for args in (('inbox', '--wait'), ('inbox', '--wait', 'abc'),
                 ('inbox', '--wait='), ('inbox', 'bogus'),
                 ('inbox', '--wait', '2', 'extra')):
        code, out, err, _wd = _run(tmp_path, server, *args)
        assert code == 2, args
        assert len(out.strip().splitlines()) == 1, args
        assert 'Traceback' not in err, args
    # 坏参数不该发出任何请求
    assert _Handler.posts == [] and _Handler.inboxAfter == []


# ---------------------------------------------------------------- resume 续接

def test_resume_sends_default_message_when_paused(server, tmp_path):
    """paused：先发默认续接消息，再照常驱动到本轮结束（DONE、退出 0）。"""
    code, out, err, _wd = _run(tmp_path, server, 'r1', 'resume',
                               alias_state=('r1', {'task': 't-1'}))
    assert code == 0, err
    assert _posts('/message') == [{'text': RESUME_DEFAULT}]
    assert out.splitlines()[0].startswith('DONE')


def test_resume_msg_override(server, tmp_path):
    """msg.txt 存在时用它覆盖默认续接文案。"""
    code, _out, err, _wd = _run(tmp_path, server, 'r2', 'resume',
                                alias_state=('r2', {'task': 't-1'}),
                                msg='先看 tests/ 红了几个，再继续')
    assert code == 0, err
    assert _posts('/message') == [{'text': '先看 tests/ 红了几个，再继续'}]


def test_resume_no_message_when_not_paused(server, tmp_path):
    """任务没被暂停：不发续接消息，直接驱动。"""
    _Handler.taskStatus = 'waiting'
    code, out, err, _wd = _run(tmp_path, server, 'r3', 'resume',
                               alias_state=('r3', {'task': 't-1'}))
    assert code == 0, err
    assert _posts('/message') == []
    assert out.splitlines()[0].startswith('DONE')


def test_resume_interrupted_also_resumes(server, tmp_path):
    """interrupted 同样发续接消息（真断，但先按同一路径试一次）。"""
    _Handler.taskStatus = 'interrupted'
    code, _out, err, _wd = _run(tmp_path, server, 'r4', 'resume',
                                alias_state=('r4', {'task': 't-1'}))
    assert code == 0, err
    assert _posts('/message') == [{'text': RESUME_DEFAULT}]


def test_resume_fresh_alias_says_new_first(server, tmp_path):
    """新别名没有任务：一句"先 new"退出 3，不联网。"""
    code, out, err, _wd = _run(tmp_path, server, 'r5', 'resume')
    assert code == 3, err
    assert '还没有任务' in out and 'new' in out
    assert _Handler.posts == []


# ---------------------------------------------------------------- new 带标题

def test_new_title_becomes_first_line(server, tmp_path):
    """title.txt 存在：作为 title 字段传给建任务，提示词原样不拼首行。"""
    code, _out, err, _wd = _run(
        tmp_path, server, 'tn', 'new', '-',
        alias_state=('tn', {'task': ''}),
        msg='把四份文档改完\n边界：只改 .wt/d',
        title='批次 D：文档跟上指挥链')
    assert code == 0, err
    created = _posts('/api/tasks')
    assert len(created) == 1
    assert created[0]['title'] == '批次 D：文档跟上指挥链'
    assert created[0]['prompt'] == '把四份文档改完\n边界：只改 .wt/d'


def test_new_title_via_workline_also_passed(server, tmp_path):
    """工作线派活同样带 title（send 的 task_opts 原样进请求体）。"""
    code, _out, err, _wd = _run(
        tmp_path, server, 'tw', 'new', 'w:r5',
        alias_state=('tw', {'task': ''}),
        msg='继续第 5 批',
        title='批次 D：文档跟上指挥链')
    assert code == 0, err
    sent = _posts('/api/worklines/r5/tasks')
    assert len(sent) == 1
    assert sent[0]['title'] == '批次 D：文档跟上指挥链'
    assert sent[0]['prompt'] == '继续第 5 批'


def test_new_without_title_keeps_msg(server, tmp_path):
    """没有 title.txt：不带 title 字段，提示词原样发（服务端自行推导标题）。"""
    code, _out, err, _wd = _run(
        tmp_path, server, 'tn2', 'new', '-',
        alias_state=('tn2', {'task': ''}),
        msg='把四份文档改完\n边界：只改 .wt/d')
    assert code == 0, err
    body = _posts('/api/tasks')[0]
    assert 'title' not in body
    assert body['prompt'] == '把四份文档改完\n边界：只改 .wt/d'
