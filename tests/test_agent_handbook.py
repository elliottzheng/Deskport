# -*- coding: utf-8 -*-
"""公开接入手册：无令牌可读、占位符替换、JSON 形状、开关与内容安全。"""
import re
from types import SimpleNamespace

import pytest

import config as configMod
from modules import meta


@pytest.fixture
def env(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client())


def test_public_without_token(env):
    """令牌之前就要能读：不带任何头也是 200。"""
    resp = env.client.get('/api/agent/handbook')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'text/markdown; charset=utf-8'


def test_placeholders_replaced(env):
    body = env.client.get('/api/agent/handbook').get_data(as_text=True)
    assert not re.search(r'\{\{[A-Z]+\}\}', body)
    assert '{{BASE}}' not in body and '{{VERSION}}' not in body
    assert 'http://localhost' in body
    assert meta.BUILD in body


def test_json_shape(env):
    data = env.client.get('/api/agent/handbook?format=json').get_json()
    assert data['base'] == 'http://localhost'
    assert data['version'] == meta.BUILD
    assert data['markdown'].startswith('# ')
    assert '{{BASE}}' not in data['markdown']
    assert '{{VERSION}}' not in data['markdown']


def test_disabled_returns_404(env, monkeypatch):
    monkeypatch.setattr(configMod, 'isHandbookEnabled', lambda: False)
    assert env.client.get('/api/agent/handbook').status_code == 404
    assert env.client.get('/api/agent/handbook?format=json').status_code == 404


def test_no_secrets_or_local_paths(env):
    """公开端点：正文不得出现令牌指纹字段、密码字段或本机绝对路径。"""
    body = env.client.get('/api/agent/handbook').get_data(as_text=True)
    assert not re.search(r'agentTokens|tokenHash|password|C:\\|/mnt/', body)
    assert 'X-Agent-Token' in body


def test_tasks_still_rejects_legacy_engine(env):
    """回归：对外只认 node/cwd，旧字段仍是 400。"""
    with env.client.session_transaction() as sess:
        sess['authenticated'] = True
    resp = env.client.post('/api/tasks', json={'prompt': 'x', 'engine': 'local'})
    assert resp.status_code == 400
    assert 'node' in resp.get_json()['error']