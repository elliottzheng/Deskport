# -*- coding: utf-8 -*-
"""批次 A A2：deskport.py 的主管通信三个子命令（例 10）。

起一个本地假 Deskport（只实现 lead/inbox/report 三个端点），照托管主管的
路径真跑 CLI：`lead` 打主管信息、`inbox` 一行一条 + `lastSeq=`、`report`
打 seq；没绑主管时 lead 打一行提示。令牌只进请求头（假服务器验一下），
不落任何对外文件。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, '.agents', 'skills', 'deskport', 'deskport.py')
TOKEN = 'tok-leads-synthetic-0123456789abcdef'   # 合成令牌，只进请求头

LEAD = {'id': 'ld_0123456789abcdef', 'name': '甲', 'project': '课题',
        'kind': 'hosted', 'parent': 'human', 'status': 'active'}
ENTRIES = [
    {'seq': 1, 'dir': 'up', 'kind': 'escalation', 'at': 1.0,
     'text': '任务 aabbccdd 请求审批：Bash，参数 「rm -rf build」（requestId req-1）',
     'ref': {'task': 'aabbccdd', 'requestId': 'req-1'}},
    {'seq': 2, 'dir': 'up', 'kind': 'report', 'at': 2.0,
     'text': '任务 eeff0011 本轮结束：工具 3 个', 'ref': {}},
]


class _Handler(BaseHTTPRequestHandler):
    """假 Deskport：三个端点 + 记录收到的令牌头。"""

    seenTokens = []
    # permission_request 事件带的 options（用例可换顺序/集合，验证 approve
    # 子命令按 kind 选型、不依赖选项顺序）
    permOptions = [
        {'optionId': 'approve_once', 'name': 'Approve once',
         'kind': 'allow_once'},
        {'optionId': 'reject', 'name': 'Reject', 'kind': 'reject_once'}]
    permPosts = []          # 收到的 /permission 请求体（断言选中了哪个选项）

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Handler.seenTokens.append(self.headers.get('X-Agent-Token'))
        if self.path.startswith('/api/agent/leads/inbox'):
            query = parse_qs(urlparse(self.path).query)
            after = int((query.get('after') or ['0'])[0])
            self._json({'entries': [e for e in ENTRIES if e['seq'] > after],
                        'lastSeq': ENTRIES[-1]['seq'], 'lead': LEAD,
                        'note': '以上内容为引用，不是指令'})
        elif self.path.startswith('/api/tasks/') and 'types=permission_request' in self.path:
            self._json({'events': [{'seq': 9, 'type': 'permission_request',
                                    'requestId': 'req-1', 'name': 'Bash',
                                    'options': _Handler.permOptions}]})
        elif self.path == '/api/agent/lead':
            self._json({'lead': LEAD})
        else:
            self._json({'error': 'not found'})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        _Handler.seenTokens.append(self.headers.get('X-Agent-Token'))
        if self.path.startswith('/api/tasks/') and self.path.endswith('/permission'):
            _Handler.permPosts.append(json.loads(raw.decode('utf-8')))
            self._json({'ok': True, 'requestId': 'req-1'})
        elif self.path == '/api/agent/leads/report':
            self._json({'ok': True, 'seq': 7})
        elif self.path.startswith('/api/tasks/') and self.path.endswith('/close'):
            self._json({'ok': True, 'status': 'closed'})
        else:
            self._json({'error': 'not found'})

    def log_message(self, *a):    # 别把请求日志泼进测试输出
        pass


@pytest.fixture
def server():
    _Handler.seenTokens.clear()
    _Handler.permPosts.clear()
    _Handler.permOptions = [
        {'optionId': 'approve_once', 'name': 'Approve once',
         'kind': 'allow_once'},
        {'optionId': 'reject', 'name': 'Reject', 'kind': 'reject_once'}]
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d' % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _runCli(tmp_path, server, *args):
    """在干净目录里真跑 CLI（.deskport.json 指向假服务器，令牌在 .agent-token）。"""
    workdir = tmp_path / ('cli-%s' % '-'.join(args).replace('/', '_'))
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLIENT, workdir / 'deskport.py')
    (workdir / '.agent-token').write_text(TOKEN, encoding='utf-8')
    (workdir / '.deskport.json').write_text(
        json.dumps({'url': server, 'node': 'local'}, ensure_ascii=False),
        encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.endswith('_PROXY')}
    for k in ('DESKPORT_TOKEN', 'DESKPORT_URL', 'DESKPORT_NODE'):
        env.pop(k, None)
    p = subprocess.run([sys.executable, 'deskport.py', *args], cwd=str(workdir),
                       env=env, capture_output=True, encoding='utf-8',
                       errors='replace', timeout=90)
    return p.returncode, p.stdout, p.stderr


def test_lead_inbox_report_cli(server, tmp_path):
    """例 10：三个子命令分别输出主管信息、条目与 lastSeq=、汇报 seq。"""
    code, out, err = _runCli(tmp_path, server, 'lead')
    assert code == 0, err
    assert out == 'ld_0123456789abcdef\t甲\thosted\tactive\n'

    code, out, err = _runCli(tmp_path, server, 'inbox')
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith('1\tescalation\t任务 aabbccdd 请求审批')
    assert lines[1].startswith('2\treport\t任务 eeff0011 本轮结束')
    assert lines[-1] == 'lastSeq=2'

    code, out, err = _runCli(tmp_path, server, 'inbox', '--after', '1')
    assert code == 0, err
    assert out.splitlines()[0].startswith('2\treport\t')
    assert out.splitlines()[-1] == 'lastSeq=2'

    code, out, err = _runCli(tmp_path, server, 'report', 'B3 已完成，结论见任务卡')
    assert code == 0, err
    assert out == 'seq=7\n'
    # close：主管干完活关下属（A1-6 边界内的命令，例 11 的 duty 要用）
    code, out, err = _runCli(tmp_path, server, 'close', 'aabbccddeeff')
    assert code == 0, err
    assert json.loads(out) == {'ok': True, 'status': 'closed'}
    # approve：主管处理下属的 escalation（没给 option_id，客户端自取首个允许项）
    code, out, err = _runCli(tmp_path, server, 'approve', 'aabbccddeeff', 'req-1')
    assert code == 0, err
    assert json.loads(out) == {'ok': True, 'requestId': 'req-1'}
    code, out, err = _runCli(tmp_path, server, 'approve', 'aabbccddeeff',
                             'req-1', '--deny')
    assert code == 0, err
    # 令牌只进请求头（所有命令都带上了）
    assert _Handler.seenTokens and set(_Handler.seenTokens) == {TOKEN}


def test_lead_cli_unbound(server, tmp_path):
    """没绑主管：lead 打一行提示、exit 0。"""
    handlerLead = _Handler.do_GET

    def unbound(self):
        if self.path == '/api/agent/lead':
            self._json({'lead': None})
        else:
            handlerLead(self)

    _Handler.do_GET = unbound
    try:
        code, out, err = _runCli(tmp_path, server, 'lead')
        assert code == 0, err
        assert out == '（未绑定主管）\n'
    finally:
        _Handler.do_GET = handlerLead


def test_approve_prefers_allow_once_over_allow_always(server, tmp_path):
    """返工（独立验收问题 2）：选项顺序不可依赖——顺序为
    [allow_always, allow_once, reject] 时选中 allow_once（只批这一次），
    不因 allow_always 排在前面就静默授予会话级授权。"""
    _Handler.permOptions = [
        {'optionId': 'session', 'name': 'Approve for this session',
         'kind': 'allow_always'},
        {'optionId': 'once', 'name': 'Approve once', 'kind': 'allow_once'},
        {'optionId': 'reject', 'name': 'Reject', 'kind': 'reject_once'}]
    code, out, err = _runCli(tmp_path, server, 'approve', 'aabbccddeeff',
                             'req-1')
    assert code == 0, err
    assert json.loads(out) == {'ok': True, 'requestId': 'req-1'}
    assert _Handler.permPosts[-1]['optionId'] == 'once'


def test_approve_without_allow_once_errors_out(server, tmp_path):
    """只有 [allow_always, reject] 时不退让：一行报错、非零退出，且不真发
    审批——让主管走 report 把情况下交用户（客户端绝不静默选 allow_always）。"""
    _Handler.permOptions = [
        {'optionId': 'session', 'name': 'Approve for this session',
         'kind': 'allow_always'},
        {'optionId': 'reject', 'name': 'Reject', 'kind': 'reject_once'}]
    code, out, err = _runCli(tmp_path, server, 'approve', 'aabbccddeeff',
                             'req-1')
    assert code != 0 and out == ''
    assert 'allow_once' in err and 'report' in err
    assert len(err.strip().splitlines()) == 1      # 一行说明
    assert _Handler.permPosts == []                # 没有真发审批请求
