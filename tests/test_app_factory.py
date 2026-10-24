# -*- coding: utf-8 -*-
"""create_app 工厂：多次装配互不影响，核心路由齐全。"""
from app import create_app


def test_factory_builds_independent_apps():
    first, _sock1 = create_app()
    second, _sock2 = create_app()
    assert first is not second
    assert len(list(first.url_map.iter_rules())) == len(list(second.url_map.iter_rules()))


def test_factory_registers_core_routes():
    app, _sock = create_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    for path in ('/login', '/app', '/files', '/api/files', '/api/tasks',
                 '/api/control/summary', '/api/terminal/sessions', '/api/ssh/sessions'):
        assert path in rules


def test_factory_login_page_renders():
    app, _sock = create_app()
    resp = app.test_client().get('/login')
    assert resp.status_code == 200
    assert 'text/html' in resp.headers['Content-Type']
