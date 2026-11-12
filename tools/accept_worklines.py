# -*- coding: utf-8 -*-
"""工作线（R5-1a）黑盒验收：对着一台真实运行的 Deskport 跑一遍。

    python tools/accept_worklines.py --base http://127.0.0.1:6789 \\
        --token <智能体令牌> [--model glm-5.3-flash] [--wait 600] [--strict]

只走 HTTP（/api/worklines 与 /api/tasks），不 import 项目代码、不读状态目录。
硬检查失败 → 退出码 1；写回（执行方是否真的改 WORKLINE.md）默认只 WARN，
`--strict` 时升级为失败——模型是否照做是产品指标，不该让脚本误报挂掉。

用例顺序（一条真实工作线走完全程）：
    auth → create → 派活(注入) → 续聊(自动路由) → 轮换(旧关新开) → 清理
"""
import argparse
import os
import sys
import time
import uuid

import requests

MARK = 'WL' + uuid.uuid4().hex[:6].upper()
FAILS = []
WARNS = []


def say(kind, name, detail=''):
    print('%-4s [%s] %s' % (kind, name, detail))
    if kind == 'FAIL':
        FAILS.append(name)
    elif kind == 'WARN':
        WARNS.append(name)


class Api:
    def __init__(self, base, token):
        self.base = base.rstrip('/')
        self.s = requests.Session()
        self.s.headers.update({'X-Agent-Token': token,
                               'Content-Type': 'application/json'})
        self.s.trust_env = False       # 本机地址不走系统代理

    def call(self, method, path, body=None, timeout=60):
        url = self.base + path
        resp = self.s.request(method, url, json=body, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {'raw': resp.text[:500]}
        return resp.status_code, data

    def get(self, path, timeout=60):
        return self.call('GET', path, timeout=timeout)

    def post(self, path, body=None, timeout=60):
        return self.call('POST', path, body or {}, timeout=timeout)

    def put(self, path, body=None, timeout=60):
        return self.call('PUT', path, body or {}, timeout=timeout)

    def delete(self, path, timeout=60):
        return self.call('DELETE', path, timeout=timeout)


def approvePending(api, taskId, pending):
    """yolo 模式下通常用不到；真出现审批就按 allow_once 放行。"""
    options = pending.get('options') or []
    pick = next((o['optionId'] for o in options
                 if o.get('kind') in ('allow_once', 'allow_always')), '')
    if not pick:
        say('WARN', 'permission', '无允许选项，拒绝 %s' % pending.get('name'))
        api.post('/api/tasks/%s/permission' % taskId,
                 {'requestId': pending.get('requestId'), 'deny': True})
        return
    api.post('/api/tasks/%s/permission' % taskId,
             {'requestId': pending.get('requestId'), 'optionId': pick})
    say('INFO', 'permission', '自动放行 %s' % pending.get('name'))


def waitTask(api, taskId, seconds):
    """阻塞到回合落定（waiting/failed/closed/cancelled）；期间替主控批审批。"""
    deadline = time.time() + seconds
    after = 0
    while time.time() < deadline:
        left = max(5, min(120, int(deadline - time.time())))
        code, data = api.get('/api/tasks/%s/wait?after=%d&timeout=%d'
                             % (taskId, after, left), timeout=left + 30)
        if code != 200:
            return 'http-%s' % code, data
        if data.get('pending'):
            approvePending(api, taskId, data['pending'])
            continue
        after = data.get('lastSeq') or after
        status = data.get('status') or ''
        if status in ('waiting', 'failed', 'closed', 'cancelled', 'paused',
                      'interrupted'):
            return status, data
    return 'timeout', {}


def userEvents(api, taskId):
    code, data = api.get('/api/tasks/%s/events?after=0&types=user' % taskId)
    if code != 200:
        return []
    return [e.get('text') or '' for e in data.get('events') or []
            if e.get('type') == 'user']


def closeTask(api, taskId):
    if not taskId:
        return
    try:
        api.post('/api/tasks/%s/close' % taskId, timeout=20)
    except requests.RequestException:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=os.environ.get('DESKPORT_BASE',
                                                    'http://127.0.0.1:6789'))
    ap.add_argument('--token', default=os.environ.get('DESKPORT_TOKEN', ''))
    ap.add_argument('--model', default='')
    ap.add_argument('--mode', default='yolo', choices=['auto', 'default', 'yolo'])
    ap.add_argument('--wait', type=int, default=600)
    ap.add_argument('--strict', action='store_true',
                    help='写回检查失败也算失败（默认只 WARN）')
    ap.add_argument('--keep', action='store_true', help='保留工作线，便于人工查看')
    args = ap.parse_args()
    if not args.token:
        print('需要智能体令牌：--token 或环境变量 DESKPORT_TOKEN')
        return 2
    api = Api(args.base, args.token)
    wid = 'acc-%s' % MARK.lower()
    doc = ('# 验收接力棒 %s\n\n'
           '| 项 | 状态 |\n|---|---|\n| 脚本创建 | ✅ |\n' % MARK)
    taskIds = []
    print('== 工作线验收 %s @ %s' % (MARK, args.base))

    try:
        code, data = api.get('/api/worklines')
    except requests.RequestException as e:
        print('无法连接 %s：%s' % (args.base, e))
        return 2
    if code == 200:
        say('PASS', 'auth', 'GET /api/worklines 200')
    else:
        say('FAIL', 'auth', 'HTTP %s %s' % (code, data))
        return 1

    code, data = api.post('/api/worklines', {
        'id': wid, 'title': 'R5-1a 验收 %s' % MARK, 'doc': doc,
        'policy': {}, 'defaults': {'mode': args.mode},
        'repo': 'acceptance', 'branch': 'workline'})
    if code != 201:
        say('FAIL', 'create', 'HTTP %s %s' % (code, data))
        return 1
    wl = data['workline']
    rev0 = wl['rev']
    say('PASS', 'create', '%s rev=%s' % (wid, rev0[:8]))

    body = {'prompt': ('这是一次工作线验收，请只做三件事：\n'
                       '1) 先读当前目录的 WORKLINE.md；\n'
                       '2) 在文件末尾追加一行 "- 验收回合完成 %s"（保留原有内容）；\n'
                       '3) 最后只回复一句：WL-ACK-%s。\n'
                       '不要新建别的文件，不要做别的事。' % (MARK, MARK)),
            'node': 'local', 'mode': args.mode}
    if args.model:
        body['model'] = args.model
    code, data = api.post('/api/worklines/%s/tasks' % wid, body, timeout=120)
    if code not in (200, 202):
        say('FAIL', 'dispatch', 'HTTP %s %s' % (code, data))
        return cleanup(api, wid, taskIds, args)
    taskId = (data.get('task') or {}).get('id') or ''
    taskIds.append(taskId)
    say('PASS', 'dispatch', 'routed=%s task=%s' % (data.get('routed'), taskId))

    status, snap = waitTask(api, taskId, args.wait)
    if status in ('waiting',):
        say('PASS', 'turn1', '任务落定 status=%s' % status)
    else:
        say('FAIL', 'turn1', 'status=%s err=%s' % (status, str(snap.get('error'))[:200]))

    users = userEvents(api, taskId)
    first = users[0] if users else ''
    for name, needle in (('inject-baton', MARK),
                         ('inject-rules', '不要使用后台任务'),
                         ('inject-contract', 'WORKLINE.md')):
        if needle in first:
            say('PASS', name, '首条消息含 %r' % needle)
        else:
            say('FAIL', name, '首条消息不含 %r（长度 %d）' % (needle, len(first)))

    code, data = api.get('/api/tasks/%s' % taskId)
    wlRef = ((data.get('task') or {}).get('workline') or {})
    if wlRef.get('id') == wid:
        say('PASS', 'task-link', '任务带 workline=%s rev=%s'
            % (wlRef.get('id'), str(wlRef.get('rev'))[:8]))
    else:
        say('FAIL', 'task-link', '任务详情 workline=%r' % (wlRef,))
    ctx = (data.get('task') or {}).get('context')
    if ctx is not None:
        say('PASS', 'context', 'context=%s' % (ctx,))
    else:
        say('WARN', 'context', '任务详情无 context（本轮未上报 usage）')

    code, data = api.get('/api/worklines/%s' % wid)
    wl = (data.get('workline') or {}) if code == 200 else {}
    rev1 = wl.get('rev') or ''
    if rev1 and rev1 != rev0:
        say('PASS', 'writeback', 'rev %s → %s' % (rev0[:8], rev1[:8]))
    else:
        say('FAIL' if args.strict else 'WARN', 'writeback',
            'rev 未变（执行方没改 WORKLINE.md）')
    if wl.get('currentTask') == taskId:
        say('PASS', 'current-task', wl.get('currentTask') or '')
    else:
        say('FAIL', 'current-task', 'currentTask=%r 期望 %r'
            % (wl.get('currentTask'), taskId))
    if wl.get('nextAction') in ('', 'rotate'):
        say('PASS', 'next-action', repr(wl.get('nextAction')))
    else:
        say('FAIL', 'next-action', repr(wl.get('nextAction')))

    code, data = api.post('/api/worklines/%s/tasks' % wid,
                          {'prompt': '只回复一句：MSG-OK-%s。不要改文件。' % MARK},
                          timeout=120)
    routed = data.get('routed')
    if code in (200, 202) and routed in ('message', 'queued'):
        say('PASS', 'route-message', 'routed=%s' % routed)
    else:
        say('FAIL', 'route-message', 'HTTP %s %s' % (code, data))
    if code in (200, 202):
        status, _snap = waitTask(api, taskId, args.wait)
        if status in ('waiting',):
            say('PASS', 'turn2', '续聊落定')
        else:
            say('FAIL', 'turn2', 'status=%s' % status)

    code, data = api.post('/api/worklines/%s/rotate' % wid,
                          {'prompt': '只回复一句：ROTATE-OK-%s。不要改文件。' % MARK,
                           'mode': args.mode}, timeout=120)
    if code not in (200, 202):
        say('FAIL', 'rotate', 'HTTP %s %s' % (code, data))
        return cleanup(api, wid, taskIds, args)
    newId = (data.get('task') or {}).get('id') or ''
    taskIds.append(newId)
    if newId and newId != taskId:
        say('PASS', 'rotate-new', '新任务 %s（旧 %s）' % (newId, taskId))
    else:
        say('FAIL', 'rotate-new', 'data=%s' % str(data)[:200])
    if data.get('closedTaskId') == taskId:
        say('PASS', 'rotate-close', '旧任务已 close')
    else:
        say('FAIL', 'rotate-close', 'closedTaskId=%r' % data.get('closedTaskId'))
    _status, _snap = waitTask(api, newId, args.wait)
    users = userEvents(api, newId)
    if users and MARK in users[0]:
        say('PASS', 'rotate-baton', '新任务首条消息带接力棒')
    else:
        say('FAIL', 'rotate-baton', '新任务首条消息缺接力棒（%d 字）'
            % (len(users[0]) if users else 0))

    return cleanup(api, wid, taskIds, args)


def cleanup(api, wid, taskIds, args):
    for tid in taskIds:
        closeTask(api, tid)
    if args.keep:
        print('保留工作线 %s（--keep）' % wid)
    else:
        code, _data = api.delete('/api/worklines/%s' % wid)
        say('PASS' if code == 200 else 'WARN', 'cleanup',
            'delete workline HTTP %s' % code)
    print('\n结果：%d 硬失败，%d 警告' % (len(FAILS), len(WARNS)))
    if FAILS:
        print('失败项：' + ', '.join(FAILS))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())