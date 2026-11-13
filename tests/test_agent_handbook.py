# -*- coding: utf-8 -*-
"""公开接入手册：无令牌可读、占位符替换、JSON 形状、开关与内容安全。

批次 I：公网入口（TLS 在隧道端终结）下 base 不能印成 http——trustProxyProto
打开时按 X-Forwarded-Proto 修正 scheme；Host 类头部一律不采信；缺省关时
伪造头部不得生效（安全验收）。手册新增「局域网直连」行与「证书是自签的」小节。
"""
import json
import re
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_handbook, meta


@pytest.fixture
def env(tmp_path):
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client())


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    """把 CONFIG_PATH 指到一份临时 config.json（真实读配置链路，不桩函数）。"""
    def _write(**data):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        monkeypatch.setattr(configMod, 'CONFIG_PATH', str(path))
        configMod._cache.update(mtime=None, data=None)
        return path
    yield _write
    configMod._cache.update(mtime=None, data=None)


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


# ------------------------------------------------ 公网入口的 scheme（批次 I）

def _baseLine(body):
    return next(line for line in body.split('\n') if line.startswith('- API base'))


def test_forwarded_proto_honored_when_enabled(env, cfgfile):
    """trustProxyProto=true + X-Forwarded-Proto: https → 手册与 JSON 的 base 都是 https。"""
    cfgfile(trustProxyProto=True)
    resp = env.client.get('/api/agent/handbook',
                          headers={'X-Forwarded-Proto': 'https'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert _baseLine(body) == '- API base（本次请求的入口）：https://localhost'
    data = env.client.get('/api/agent/handbook?format=json',
                          headers={'X-Forwarded-Proto': 'https'}).get_json()
    assert data['base'] == 'https://localhost'


def test_no_header_stays_http_when_enabled(env, cfgfile):
    """开关打开但不带头部：仍是进程真实看到的 http（不猜）。"""
    cfgfile(trustProxyProto=True)
    resp = env.client.get('/api/agent/handbook')
    assert _baseLine(resp.get_data(as_text=True)) == \
        '- API base（本次请求的入口）：http://localhost'
    assert env.client.get('/api/agent/handbook?format=json').get_json()['base'] \
        == 'http://localhost'


def test_forwarded_proto_ignored_by_default(env, cfgfile):
    """安全验收：缺省（不配 trustProxyProto）时伪造该头不得生效。"""
    cfgfile()
    body = env.client.get('/api/agent/handbook',
                          headers={'X-Forwarded-Proto': 'https'}).get_data(as_text=True)
    assert _baseLine(body) == '- API base（本次请求的入口）：http://localhost'
    assert env.client.get('/api/agent/handbook?format=json',
                          headers={'X-Forwarded-Proto': 'https'}).get_json()['base'] \
        == 'http://localhost'


def test_forwarded_proto_invalid_values_ignored(env, cfgfile):
    """开关打开也只认 http/https；多值/别的协议一律忽略，回退真实 scheme。"""
    cfgfile(trustProxyProto=True)
    for proto in ('ftp', 'https, http', 'HTTPSX', ' wss ', ''):
        resp = env.client.get('/api/agent/handbook',
                              headers={'X-Forwarded-Proto': proto})
        assert _baseLine(resp.get_data(as_text=True)) == \
            '- API base（本次请求的入口）：http://localhost', proto


def test_forwarded_host_never_trusted(env, cfgfile):
    """只采信 scheme：X-Forwarded-Host 再像也不改 host（防调用方决定印什么地址）。"""
    cfgfile(trustProxyProto=True)
    resp = env.client.get('/api/agent/handbook', headers={
        'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'evil.example.com',
        'X-Forwarded-For': '1.2.3.4', 'X-Forwarded-Port': '443'})
    assert _baseLine(resp.get_data(as_text=True)) == \
        '- API base（本次请求的入口）：https://localhost'


# ------------------------------------------------ 手册新增内容（批次 I）

def test_lan_line_present_when_configured(env, cfgfile):
    """配了 lanAddress：markdown 与 ?format=json 都带「局域网直连」行。"""
    cfgfile(port=6789, lanAddress='192.168.31.108')
    body = env.client.get('/api/agent/handbook').get_data(as_text=True)
    assert '- 局域网直连：http://192.168.31.108:6789（同网段更快，不需要 -k）' in body
    assert '{{' not in body
    data = env.client.get('/api/agent/handbook?format=json').get_json()
    assert '- 局域网直连：http://192.168.31.108:6789（同网段更快，不需要 -k）' \
        in data['markdown']
    assert data['base'] == 'http://localhost'


def test_lan_line_absent_without_config(env, cfgfile):
    """没配 lanAddress：整行消失（不留空 bullet），占位符不残留。"""
    cfgfile()
    body = env.client.get('/api/agent/handbook').get_data(as_text=True)
    assert '- 局域网直连：' not in body
    assert '{{' not in body
    assert '- API base（本次请求的入口）：http://localhost' in body
    # API base 行与手册版本行仍然相邻（删行不留下空行堆）
    lines = body.split('\n')
    assert lines[lines.index('- API base（本次请求的入口）：http://localhost') + 1] \
        == '- 手册版本：%s' % meta.BUILD


def test_lan_direct_validation(cfgfile):
    """lanDirect()：容忍 http:// 前缀与显式端口；回环/通配/坏值一律视为没配。"""
    for text, want in (('192.168.1.23', 'http://192.168.1.23:6789'),
                       ('http://10.0.0.9:8080', 'http://10.0.0.9:8080'),
                       ('nas.local', 'http://nas.local:6789'),
                       ('', ''), ('127.0.0.1', ''), ('localhost:6789', ''),
                       ('0.0.0.0', ''), ('::1', ''), ('http://', ''),
                       ('1.2.3.4:99999', ''), ('1.2.3.4:0', ''),
                       ('host with space', ''), ('http://x:abc', '')):
        cfgfile(port=6789, lanAddress=text)
        assert agent_handbook.lanDirect() == want, text


def test_cert_section_in_both_formats(env, cfgfile):
    """「证书是自签的」小节：curl -k / deskport.py insecure|cafile / 一句提醒。"""
    body = env.client.get('/api/agent/handbook').get_data(as_text=True)
    assert '## 证书是自签的' in body
    assert '`-k`' in body and '退出码 60' in body
    assert '"insecure": true' in body and 'cafile' in body
    assert '局域网直连是纯 HTTP' in body
    data = env.client.get('/api/agent/handbook?format=json').get_json()
    assert '## 证书是自签的' in data['markdown']
    assert '"insecure": true' in data['markdown']
