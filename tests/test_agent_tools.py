# -*- coding: utf-8 -*-
"""公开工具箱 /api/agent/tools：无令牌可读、清单与原文一致、白名单外 404、
共用手册开关、公开内容无凭据；并锁手册「工具箱」一节的形状。"""
import hashlib
import re
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_tools

CREDENTIAL_PATTERNS = [r'token\s*=\s*[\'"][A-Za-z0-9_-]{16,}',
                       r'password', r'agentTokens']


@pytest.fixture
def env(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client())


def test_list_public_without_token(env):
    """令牌之前就要能取：清单列出三个工具，各带 bytes/sha256。"""
    resp = env.client.get('/api/agent/tools')
    assert resp.status_code == 200
    tools = resp.get_json()['tools']
    assert [t['name'] for t in tools] == list(agent_tools.TOOLS)
    for t in tools:
        assert t['bytes'] > 0
        assert re.fullmatch(r'[0-9a-f]{64}', t['sha256'])


@pytest.mark.parametrize('name', agent_tools.TOOLS)
def test_file_matches_manifest(env, name):
    """原文 200：sha256/bytes 与清单一致，头带 X-Tools-Sha256；.md→markdown，.py→plain。"""
    listed = {t['name']: t for t in env.client.get('/api/agent/tools').get_json()['tools']}
    resp = env.client.get('/api/agent/tools/%s' % name)
    assert resp.status_code == 200
    blob = resp.get_data()
    assert hashlib.sha256(blob).hexdigest() == listed[name]['sha256']
    assert len(blob) == listed[name]['bytes']
    assert resp.headers['X-Tools-Sha256'] == listed[name]['sha256']
    want = 'text/markdown' if name.endswith('.md') else 'text/plain'
    assert resp.headers['Content-Type'] == want + '; charset=utf-8'


def test_outside_whitelist_404(env):
    """白名单外一律 404：SKILL.md、目录穿越、子目录、不存在的名字。"""
    for path in ('/api/agent/tools/SKILL.md',
                 '/api/agent/tools/../config.json',
                 '/api/agent/tools/__pycache__/x.pyc',
                 '/api/agent/tools/nope.py'):
        assert env.client.get(path).status_code == 404, path


def test_no_credentials_in_public_content(env):
    """公开内容不得含凭据：三个文件逐个扫，命中任一正则即失败。"""
    for name in agent_tools.TOOLS:
        blob = env.client.get('/api/agent/tools/%s' % name).get_data(as_text=True)
        for pat in CREDENTIAL_PATTERNS:
            assert not re.search(pat, blob), '%s 命中 %s' % (name, pat)


def test_disabled_returns_404(env, monkeypatch):
    """与手册同开关：handbookEnabled=false 时两个端点都 404。"""
    monkeypatch.setattr(configMod, 'isHandbookEnabled', lambda: False)
    assert env.client.get('/api/agent/tools').status_code == 404
    assert env.client.get('/api/agent/tools/deskport.py').status_code == 404


def test_handbook_toolbox_section(env):
    """手册「工具箱」一节：无占位符残留、无 engine、有可直接粘的下载命令。"""
    body = env.client.get('/api/agent/handbook').get_data(as_text=True)
    assert not re.search(r'\{\{[A-Z]+\}\}', body)
    assert '{{BASE}}' not in body and '{{VERSION}}' not in body
    assert 'engine' not in body
    assert 'curl -s $D/api/agent/tools/deskport.py -o deskport.py' in body
    assert '任选其一，不必全用' in body
    toolbox = body.index('## 工具箱')
    assert toolbox < body.index('## 端点') < len(body)
