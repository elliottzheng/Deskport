# -*- coding: utf-8 -*-
"""公开整包 /api/agent/skill.zip：无令牌可下、五条目、占位符按请求替换、
包内无凭据、白名单外 404、共用手册开关。"""
import hashlib
import io
import json
import os
import re
import zipfile
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_tools, meta

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         '.agents', 'skills', 'deskport')
PACK = '/api/agent/skill.zip'
ENTRIES = ['deskport/.deskport.json', 'deskport/COLLABORATION.md',
           'deskport/SKILL.md', 'deskport/deskport.py', 'deskport/driver.py']
# 公开内容里出现这些模式即失败（tests/test_agent_tools.py 的三个 + 路径泄露两个）
CREDENTIAL_PATTERNS = [r'token\s*=\s*[\'"][A-Za-z0-9_-]{16,}',
                       r'password', r'agentTokens', r'C:\\', r'/mnt/']


@pytest.fixture
def env(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client())


def openPack(resp):
    return zipfile.ZipFile(io.BytesIO(resp.get_data()))


def test_zip_public_without_token(env):
    """无令牌 200：application/zip，五个条目，全在 deskport/ 前缀下。"""
    resp = env.client.get(PACK)
    assert resp.status_code == 200
    assert resp.mimetype == 'application/zip'
    assert 'deskport-skill.zip' in resp.headers['Content-Disposition']
    assert int(resp.headers['X-Skill-Files']) == 5
    z = openPack(resp)
    assert sorted(z.namelist()) == ENTRIES
    for name in z.namelist():
        assert name.startswith('deskport/')


def test_manual_placeholders_rendered(env):
    """SKILL.md 的 {{BASE}} 换成本次请求地址、{{VERSION}} 无残留，解开即可照做。"""
    z = openPack(env.client.get(PACK))
    text = z.read('deskport/SKILL.md').decode('utf-8')
    assert '{{BASE}}' not in text and '{{VERSION}}' not in text
    assert 'http://localhost' in text          # 测试客户端的请求地址


def test_no_credentials_in_pack(env):
    """包内任一文件命中凭据/路径模式即失败（含生成的 .deskport.json）。"""
    z = openPack(env.client.get(PACK))
    for name in z.namelist():
        text = z.read(name).decode('utf-8', 'replace')
        for pat in CREDENTIAL_PATTERNS:
            assert not re.search(pat, text), '%s 命中 %s' % (name, pat)


def test_config_ready_but_tokenless(env):
    """.deskport.json 带本次地址、默认 local；除说明外只有这两项，令牌绝不进包。"""
    z = openPack(env.client.get(PACK))
    cfg = json.loads(z.read('deskport/.deskport.json').decode('utf-8'))
    assert cfg['url'] == 'http://localhost' and cfg['host'] == 'local'
    assert sorted(cfg) == ['_说明', 'host', 'url']
    assert '不要写进这个文件' in cfg['_说明']


def test_attachments_are_byte_identical(env):
    """三个工具就是仓库里的原文件；SKILL.md 只多占位符替换。"""
    z = openPack(env.client.get(PACK))
    for name in ('deskport.py', 'driver.py', 'COLLABORATION.md'):
        with open(os.path.join(SKILL_DIR, name), 'rb') as f:
            assert z.read('deskport/' + name) == f.read(), name
    with open(os.path.join(SKILL_DIR, 'SKILL.md'), encoding='utf-8') as f:
        raw = f.read()
    packed = z.read('deskport/SKILL.md').decode('utf-8')
    assert packed == raw.replace('{{BASE}}', 'http://localhost') \
                        .replace('{{VERSION}}', meta.BUILD)


def test_outside_names_404(env):
    """别的技能名、目录穿越、子路径、隐藏文件一律 404（不解释、不列目录）。"""
    for path in ('/api/agent/xxx.zip', '/api/agent/deskport.zip',
                 '/api/agent/skill.zip/../config.json',
                 '/api/agent/skill.zip/__pycache__',
                 '/api/agent/skill.zip/__pycache__/x.pyc',
                 '/api/agent/skill.zip/deskport.py',
                 '/api/agent/skill.zip/.deskport.json'):
        assert env.client.get(path).status_code == 404, path


def test_disabled_returns_404(env, monkeypatch):
    """与手册/工具箱同开关：handbookEnabled=false 时 404。"""
    monkeypatch.setattr(configMod, 'isHandbookEnabled', lambda: False)
    assert env.client.get(PACK).status_code == 404


def test_pack_url_follows_forwarded_proto(env, tmp_path, monkeypatch):
    """批次 I：trustProxyProto 打开 + X-Forwarded-Proto: https → 包内 url 是 https；
    不带头部仍是 http；开关缺省时伪造头部不生效。"""
    def _cfg(**data):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
        configMod._cache.update(mtime=None, data=None)

    _cfg(trustProxyProto=True)
    z = openPack(env.client.get(PACK, headers={'X-Forwarded-Proto': 'https'}))
    cfg = json.loads(z.read('deskport/.deskport.json').decode('utf-8'))
    assert cfg['url'] == 'https://localhost'
    assert '{{' not in z.read('deskport/SKILL.md').decode('utf-8')

    z = openPack(env.client.get(PACK))
    assert json.loads(z.read('deskport/.deskport.json').decode('utf-8'))['url'] \
        == 'http://localhost'

    _cfg()   # 缺省关：伪造 https 头不得生效
    z = openPack(env.client.get(PACK, headers={'X-Forwarded-Proto': 'https'}))
    assert json.loads(z.read('deskport/.deskport.json').decode('utf-8'))['url'] \
        == 'http://localhost'
    configMod._cache.update(mtime=None, data=None)


def test_tools_endpoints_unaffected(env):
    """同一开关下单文件工具箱照旧：清单三个、原文 200、sha256 一致。"""
    listed = {t['name']: t for t in env.client.get('/api/agent/tools').get_json()['tools']}
    assert list(listed) == list(agent_tools.TOOLS)
    resp = env.client.get('/api/agent/tools/deskport.py')
    assert resp.status_code == 200
    assert hashlib.sha256(resp.get_data()).hexdigest() == listed['deskport.py']['sha256']
