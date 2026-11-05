# -*- coding: utf-8 -*-
"""路由占位符必须与视图函数签名一致。

Flask 按规则里的 `<name>` 名传关键字参数。占位符写成 `sessionId` 而函数参数是
`sessionID` 时, 调用即 TypeError 500 —— 而且前端门禁抓不到: JS 里是字符串拼接
`'/api/terminal/sessions/' + id`, 字面量只是路由前缀。

这个 bug 在 prod 真实存在过: 侧栏"关闭会话"按钮 (term-client.js) 一直 500,
因为 prod 接线走 sessionproxy 的代理路由, 而 terminal.py 自己那份是对的。
"""
import inspect
import re

import pytest

from app import create_app


@pytest.fixture(scope='module')
def app():
    app, _sock = create_app(pluginRoots=[], pluginNavDir='/tmp/wp-nav-test')
    return app


@pytest.fixture(scope='module')
def urlMap(app):
    return app.url_map


def test_route_placeholders_match_view_signature(app, urlMap):
    bad = []
    for rule in urlMap.iter_rules():
        placeholders = re.findall(r'<(?:(?:[^:>]+:)?)([^>]+)>', rule.rule)
        if not placeholders:
            continue
        if rule.endpoint == 'static':
            continue        # Flask 内置, 签名是 send_static_file(**kwargs)
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            bad.append('%s -> endpoint %s 无视图函数' % (rule.rule, rule.endpoint))
            continue
        try:
            params = list(inspect.signature(view).parameters)
        except (TypeError, ValueError):
            continue        # 内置/partial 等无法内省的, 跳过
        for name in placeholders:
            if name not in params:
                bad.append('%s 的占位符 <%s> 不在 %s 的参数 %s 里'
                           % (rule.rule, name, rule.endpoint, params))
    assert not bad, '路由占位符与视图签名不匹配:\n  ' + '\n  '.join(bad)


def test_terminal_and_ssh_kill_routes_registered(urlMap):
    """两个 kill 路由都要在 (prod 走 sessionproxy, 无 sessiond 时走模块自己)。"""
    rules = {r.rule: sorted(r.methods) for r in urlMap.iter_rules()
             if r.rule.endswith('sessions/<sessionID>')}
    assert '/api/terminal/sessions/<sessionID>' in rules, sorted(rules)
    assert '/api/ssh/sessions/<sessionID>' in rules, sorted(rules)
    for rule, methods in rules.items():
        assert 'DELETE' in methods, '%s 缺 DELETE: %s' % (rule, methods)
