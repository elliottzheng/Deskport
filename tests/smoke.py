# -*- coding: utf-8 -*-
"""发布前后冒烟。用法:

    python tests/smoke.py                       # dev, 密码读 config.json
    python tests/smoke.py --base http://127.0.0.1:6789

检查: 登录、页面、版本、控制台、收件箱、会话列表、两个守护进程健康。
"""
import argparse
import json
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:6788')
    parser.add_argument('--password', default='')
    args = parser.parse_args()
    base = args.base.rstrip('/')

    password = args.password
    if not password:
        try:
            password = json.load(open('config.json', encoding='utf-8'))['password']
        except (OSError, KeyError, ValueError):
            print('FAIL: no password and config.json unreadable')
            return 1

    failures = []

    def check(name, ok, detail=''):
        print('%-4s %-22s %s' % ('PASS' if ok else 'FAIL', name, detail))
        if not ok:
            failures.append(name)

    s = requests.Session()
    r = s.post(base + '/login', data={'password': password}, allow_redirects=False)
    check('login', r.status_code == 302, 'HTTP %s' % r.status_code)

    for path in ('/', '/tasks', '/terminal', '/ssh', '/files', '/inbox', '/settings'):
        r = s.get(base + path)
        check('page ' + path, r.status_code == 200, 'HTTP %s' % r.status_code)

    version = s.get(base + '/api/version').json()
    check('version', bool(version.get('build')), version.get('build', ''))
    check('proto', isinstance(version.get('proto'), int), str(version.get('proto')))

    summary = s.get(base + '/api/control/summary').json()
    services = {x['kind']: x for x in summary.get('services', [])}
    check('control summary', 'web' in services)
    for kind in ('local', 'ssh'):
        svc = services.get(kind, {})
        if svc.get('configured'):
            check('daemon ' + kind, bool(svc.get('ok')), 'pid %s' % svc.get('pid'))

    r = s.get(base + '/api/inbox/recent?limit=1')
    check('inbox', r.status_code == 200, 'HTTP %s' % r.status_code)
    r = s.get(base + '/api/tasks')
    check('tasks', r.status_code == 200 and 'tasks' in r.json(), 'HTTP %s' % r.status_code)
    for kind, path in (('terminal', '/api/terminal/sessions'),
                       ('ssh', '/api/ssh/sessions')):
        r = s.get(base + path)
        check(kind + ' sessions', r.status_code == 200 and 'sessions' in r.json(),
              'HTTP %s' % r.status_code)

    print('\n%s (%d failures)' % ('SMOKE FAILED' if failures else 'SMOKE OK',
                                  len(failures)))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())