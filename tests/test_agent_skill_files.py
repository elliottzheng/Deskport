# -*- coding: utf-8 -*-
"""技能包附件（/api/agent/skills/<技能>/<文件>）：清单、JSON、raw、白名单。"""
import hashlib
import os
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events

TOKEN = 'tok-' + 'c' * 24
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
HEADERS = {'X-Agent-Token': TOKEN}
SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         '.agents', 'skills', 'deskport')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 't1', 'name': '测试智能体', 'tokenHash': TOKEN_HASH}])
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client())


def readPack(name):
    """按字节读：附件端点原样给字节（raw 与 sha256 都以字节为准）。
    deskport.py 出版前盖版本号（装配即版本，2026-11-14）：这里照服务端同一
    规则比对，否则磁盘原文永远对不上交付字节。"""
    from modules import agent_skill_files
    with open(os.path.join(SKILL_DIR, name), 'rb') as f:
        return agent_skill_files.stampedTool(name, f.read())


def readPackText(name):
    """SKILL.md 端点用文本模式打开（
 归一成 
），比对时也走文本模式。"""
    with open(os.path.join(SKILL_DIR, name), encoding='utf-8') as f:
        return f.read()


def test_requires_token(env):
    assert env.client.get('/api/agent/skills/deskport/deskport.py').status_code == 401
    assert env.client.get('/api/agent/skills/deskport').status_code == 401


def test_list_reports_bundle_files(env):
    """技能清单带 files：手册之外还有客户端、驱动、配置样例、协作原则。"""
    resp = env.client.get('/api/agent/skills', headers=HEADERS)
    assert resp.status_code == 200
    skill = next(s for s in resp.get_json()['skills'] if s['name'] == 'deskport')
    assert 'SKILL.md' not in skill['files']
    for name in ('deskport.py', 'driver.py', 'driver.example.json', 'COLLABORATION.md'):
        assert name in skill['files'], skill['files']


def test_skill_md_still_served_at_old_path(env):
    """原来的一步自举不变：/api/agent/skills/<技能> 仍是 SKILL.md。"""
    resp = env.client.get('/api/agent/skills/deskport', headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json()['content'] == readPackText('SKILL.md')


@pytest.mark.parametrize('name', ['deskport.py', 'driver.py', 'driver.example.json', 'COLLABORATION.md'])
def test_file_json_and_raw(env, name):
    """JSON 与 raw=1 都是原文；raw 用于 curl -o 直接落盘。"""
    resp = env.client.get('/api/agent/skills/deskport/%s' % name, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['file'] == name and body['name'] == 'deskport'
    assert body['content'] == readPack(name).decode('utf-8')
    assert body['sha256'] == hashlib.sha256(readPack(name)).hexdigest()

    raw = env.client.get('/api/agent/skills/deskport/%s?raw=1' % name, headers=HEADERS)
    assert raw.status_code == 200 and raw.mimetype == 'text/plain'
    assert raw.get_data() == readPack(name)
    assert raw.headers['X-Skill-Sha256'] == hashlib.sha256(readPack(name)).hexdigest()


def test_rejects_bad_names_and_unknown_files(env):
    """扩展名不在白名单 400；SKILL.md 走老路径（这里当附件找不到）；不存在 404 带可选清单。"""
    for name in ('deskport.exe', 'noext', 'x' * 60 + '.py', 'a.b.py'):
        assert env.client.get('/api/agent/skills/deskport/%s' % name,
                              headers=HEADERS).status_code == 400
    assert env.client.get('/api/agent/skills/deskport/SKILL.md',
                          headers=HEADERS).status_code == 404
    missing = env.client.get('/api/agent/skills/deskport/nope.py', headers=HEADERS)
    assert missing.status_code == 404
    assert 'deskport.py' in missing.get_json()['available']
    assert env.client.get('/api/agent/skills/no-such-skill/deskport.py',
                          headers=HEADERS).status_code == 404