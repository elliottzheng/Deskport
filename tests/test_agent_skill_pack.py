# -*- coding: utf-8 -*-
"""技能包整包下载（/api/agent/skills/<技能>/pack.zip）：解开就能用。"""
import io
import json
import zipfile

import pytest

from tests.test_agent_skill_files import HEADERS, SKILL_DIR, env, readPack  # noqa: F401

PACK = '/api/agent/skills/deskport/pack.zip'


def openPack(resp):
    return zipfile.ZipFile(io.BytesIO(resp.get_data()))


def test_pack_requires_token(env):
    assert env.client.get(PACK).status_code == 401


def test_pack_contains_whole_skill(env):
    """手册 + 四个附件 + 现成配置，都在 <技能>/ 前缀下（解压即得技能目录）。"""
    resp = env.client.get(PACK, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.mimetype == 'application/zip'
    assert 'deskport-skill.zip' in resp.headers['Content-Disposition']
    names = sorted(openPack(resp).namelist())
    assert names == ['deskport/.deskport.json', 'deskport/COLLABORATION.md',
                     'deskport/SKILL.md', 'deskport/deskport.py',
                     'deskport/driver.example.json', 'deskport/driver.py']
    assert int(resp.headers['X-Skill-Files']) == len(names)


def test_pack_files_are_byte_identical(env):
    """包里就是仓库里的原文件，不是副本。"""
    z = openPack(env.client.get(PACK, headers=HEADERS))
    for name in ('SKILL.md', 'COLLABORATION.md', 'deskport.py', 'driver.py',
                 'driver.example.json'):
        assert z.read('deskport/' + name) == readPack(name), name


def test_pack_config_is_ready_but_tokenless(env):
    """url 取本次请求地址、node/host 默认 local（同值，新旧客户端都读）；配置里不含令牌。"""
    z = openPack(env.client.get(PACK, headers=HEADERS))
    cfg = json.loads(z.read('deskport/.deskport.json').decode('utf-8'))
    assert cfg['url'].startswith('http://')
    assert cfg['node'] == 'local' and cfg['host'] == 'local'
    assert sorted(cfg) == ['_说明', 'host', 'node', 'url']
    assert '不要写进这个文件' in cfg['_说明']


def test_pack_route_not_shadowed(env):
    """pack.zip 不能被 /<技能>/<文件> 抢走（那不是合法附件名）。"""
    assert env.client.get(PACK, headers=HEADERS).mimetype == 'application/zip'
    assert env.client.get('/api/agent/skills/deskport/pack.zip?raw=1',
                          headers=HEADERS).mimetype == 'application/zip'


def test_pack_unknown_skill(env):
    resp = env.client.get('/api/agent/skills/nope/pack.zip', headers=HEADERS)
    assert resp.status_code == 404
    assert 'deskport' in resp.get_json()['available']


def test_skills_list_advertises_pack(env):
    skills = {s['name']: s for s in env.client.get('/api/agent/skills',
                                                   headers=HEADERS).get_json()['skills']}
    assert skills['deskport']['pack'] == '/api/agent/skills/deskport/pack.zip'