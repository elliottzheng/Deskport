# -*- coding: utf-8 -*-
"""会话关闭端点：返回值形态一致性。

terminalKill 有两条分支: 联邦会话走 _proxyJsonData(原始 dict) 后 jsonify,
本机会话走 _proxyJson —— 而后者已经返回 (Response, status)。本地分支若再
jsonify 一层, 就是把 Response 当数据序列化, 直接 500。这个坑踩过两次:
先是路由占位符大小写不匹配 (test_route_params.py 覆盖), 修完暴露双重 jsonify。
"""
import pytest

from modules import sessionproxy


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sessionproxy, '_proxyJsonData',
                        lambda *a, **k: ({'ok': True, 'via': 'data'}, 200))
    monkeypatch.setattr(sessionproxy, '_proxyJson',
                        lambda *a, **k: ({'ok': True, 'via': 'json'}, 200))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['authenticated'] = True
    return c


def test_terminal_kill_local_branch_returns_json(client):
    """本机会话分支: 不得把 Response 再序列化一次。"""
    r = client.delete('/api/terminal/sessions/deadbeef')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {'ok': True, 'via': 'json'}


def test_terminal_kill_requires_login(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    with app.test_client() as c:
        assert c.delete('/api/terminal/sessions/x').status_code == 401


def test_ssh_kill_returns_json(client):
    r = client.delete('/api/ssh/sessions/deadbeef')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {'ok': True, 'via': 'json'}


def test_terminal_sessions_endpoint_still_works(client):
    """列表端点走 _proxyJsonData + 联邦合并, 与 kill 的形态本就不同。"""
    r = client.get('/api/terminal/sessions')
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'via': 'data'}
