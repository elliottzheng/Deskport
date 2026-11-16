# -*- coding: utf-8 -*-
"""批次 PM 测试：平级消息与登记薄（agent_peer）。

双令牌 e2e（规格 §5）：两个令牌各自自助注册为 external 主管（批次 SR
的真实入口），互发平级消息、查登记薄。唤醒旁路另走进程内直测
（_wakeHosted + 桩 deliverMessage）——把 peer 从 URGENT_KINDS 删掉必须红
（改坏即红自证）。
"""
import json
import time

import pytest

import config as configMod
from modules import events as eventsMod
from modules import agent_peer, leads_inbox, leads_store, tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pm-test-pw'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    configMod._cache.update(mtime=None, data=None)
    monkeypatch.setattr(store, 'repo', TaskRepository(
        str(tmp_path / 'tasks.json'), str(tmp_path / 'logs')))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302

    def newToken(name):
        resp = client.post('/api/settings/agent-tokens',
                           data=json.dumps({'name': name}),
                           content_type='application/json')
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()['token']

    def register(name, token):
        resp = app.test_client().post(
            '/api/agent/lead', data=json.dumps({'name': name}),
            content_type='application/json',
            headers={'X-Agent-Token': token})
        assert resp.status_code in (200, 201), resp.get_json()
        return resp.get_json()['lead']

    tokA, tokB = newToken('甲'), newToken('乙')
    leadA = register('甲主管', tokA)
    leadB = register('乙主管', tokB)
    return {'app': app, 'client': client, 'tokA': tokA, 'tokB': tokB,
            'leadA': leadA, 'leadB': leadB, 'tmp': tmp_path}


def _post(app, path, body, token):
    return app.test_client().post(
        path, data=json.dumps(body), content_type='application/json',
        headers={'X-Agent-Token': token})


def _get(app, path, token):
    return app.test_client().get(path, headers={'X-Agent-Token': token})


# ---------------------------------------------------------------- 登记薄

def test_registry_lists_registered_leads(env):
    resp = _get(env['app'], '/api/agent/registry', env['tokA'])
    assert resp.status_code == 200
    rows = {r['name']: r for r in resp.get_json()['leads']}
    assert set(rows) == {'甲主管', '乙主管'}
    assert rows['甲主管']['kind'] == 'external'
    assert rows['甲主管']['id'] == env['leadA']['id']
    assert rows['甲主管']['taskId'] == ''     # 自助注册不起托管任务


def test_registry_requires_token(env):
    assert env['app'].test_client().get(
        '/api/agent/registry').status_code == 401


# ---------------------------------------------------------------- 平级消息

def test_peer_message_end_to_end(env):
    resp = _post(env['app'], '/api/agent/message',
                 {'to': '乙主管', 'text': '9222 端口是你的吗？要多久？',
                  'context': {'host': 'ai-xmyz', 'port': 9222}}, env['tokA'])
    assert resp.status_code == 202, resp.get_json()
    body = resp.get_json()
    assert body['to'] == '乙主管' and body['seq'] >= 1
    entries = leads_inbox.readAll(env['leadB']['id'])
    assert len(entries) == 1
    e = entries[0]
    assert e['kind'] == 'peer' and e['dir'] == 'up'
    assert '9222 端口' in e['text'] and e['text'].startswith('【甲】')
    assert e['ref']['context'] == {'host': 'ai-xmyz', 'port': 9222}
    assert e['ref']['from']['token'] == '甲'
    # 收方隔离：甲的箱空
    assert leads_inbox.readAll(env['leadA']['id']) == []


def test_peer_message_by_id(env):
    resp = _post(env['app'], '/api/agent/message',
                 {'to': env['leadB']['id'], 'text': '按 id 寻址'}, env['tokA'])
    assert resp.status_code == 202
    assert leads_inbox.readAll(env['leadB']['id'])[0]['kind'] == 'peer'


def test_peer_message_unknown_target(env):
    resp = _post(env['app'], '/api/agent/message',
                 {'to': '不存在的人', 'text': 'x'}, env['tokA'])
    assert resp.status_code == 400
    assert 'availableLeads' in resp.get_json()


def test_peer_message_missing_fields(env):
    assert _post(env['app'], '/api/agent/message',
                 {'to': '乙主管', 'text': ''}, env['tokA']).status_code == 400
    assert _post(env['app'], '/api/agent/message',
                 {'to': '', 'text': 'x'}, env['tokA']).status_code == 400


def test_peer_message_to_self_rejected(env):
    resp = _post(env['app'], '/api/agent/message',
                 {'to': env['leadA']['id'], 'text': '自言自语'}, env['tokA'])
    assert resp.status_code == 400


def test_peer_message_context_not_dict(env):
    resp = _post(env['app'], '/api/agent/message',
                 {'to': '乙主管', 'text': 'x', 'context': 'str'}, env['tokA'])
    assert resp.status_code == 400


def test_peer_message_requires_token(env):
    resp = env['app'].test_client().post(
        '/api/agent/message', data=json.dumps({'to': '乙主管', 'text': 'x'}),
        content_type='application/json')
    assert resp.status_code == 401


def test_peer_message_rate_limit(env):
    for i in range(agent_peer.RATE_PER_MIN):
        resp = _post(env['app'], '/api/agent/message',
                     {'to': '乙主管', 'text': 'x%d' % i}, env['tokA'])
        assert resp.status_code == 202, resp.get_json()
    resp = _post(env['app'], '/api/agent/message',
                 {'to': '乙主管', 'text': '超额'}, env['tokA'])
    assert resp.status_code == 429
    assert resp.get_json()['retryAfter'] >= 1


def test_peer_message_context_over_long_clipped(env, monkeypatch):
    """验收 FB 必修：超长 context 不得 500，落箱 ≤MAX_CONTEXT。"""
    _post(env['app'], '/api/agent/message',
          {'to': '乙主管', 'text': 'x',
           'context': {'note': 'x' * 600, 'host': 'ai-xmyz'}}, env['tokA'])
    entries = leads_inbox.readAll(env['leadB']['id'])
    assert len(entries) == 1
    ref = entries[0]['ref']
    assert len(json.dumps(ref, ensure_ascii=False)) <= \
        len(json.dumps({'from': {'token': '甲', 'lead': env['leadA']['id']},
                        'ref': ''}, ensure_ascii=False)) + \
        agent_peer.MAX_CONTEXT + 8
    stored = ref.get('context') or {}
    assert len(json.dumps(stored, ensure_ascii=False)) <= agent_peer.MAX_CONTEXT
    # 前面的键优先（note 超长整体进不去，host 小键进）
    assert 'host' in stored or stored == {}


def test_peer_message_retired_lead_receives(env):
    """停用 lead 按规格未禁：照旧可寻址、条目落箱（规格未定义，固化现状）。"""
    lead = leads_store.get(env['leadB']['id'])
    leads_store.update(env['leadB']['id'], {'status': 'retired'})
    resp = _post(env['app'], '/api/agent/message',
                 {'to': env['leadB']['id'], 'text': '给停用的'}, env['tokA'])
    assert resp.status_code == 202
    assert leads_inbox.readAll(env['leadB']['id'])[0]['kind'] == 'peer'


def test_registry_hosted_node_aggregated(env, monkeypatch):
    """hosted lead 的 node 从其托管任务取（聚合口径回归）。"""
    lead = leads_store.get(env['leadB']['id'])
    lead['taskId'] = 'tk-hosted'
    leads_store._save({lead['id']: lead})

    class Repo:
        def load(self):
            pass

        def get(self, taskId):
            return {'id': taskId, 'node': 'ai-xmyz', 'status': 'running'}
    monkeypatch.setattr(leads_inbox.store, 'repo', Repo())
    rows = agent_peer._registryRows()
    row = [r for r in rows if r['id'] == env['leadB']['id']][0]
    assert row['node'] == 'ai-xmyz' and row['taskId'] == 'tk-hosted'


def test_message_ambiguous_name_400(env):
    """重名寻址：两个同名 lead 时按名发 → 400（不得歧义投递）。"""
    data = leads_store._load()
    data['ld_dup'] = {'id': 'ld_dup', 'name': '重名', 'kind': 'external',
                      'status': 'active', 'taskId': ''}
    data['ld_dup2'] = {'id': 'ld_dup2', 'name': '重名', 'kind': 'external',
                       'status': 'active', 'taskId': ''}
    leads_store._save(data)
    resp = _post(env['app'], '/api/agent/message',
                 {'to': '重名', 'text': 'x'}, env['tokA'])
    assert resp.status_code == 400


# ------------------------------------------- 唤醒旁路（进程内直测）

def _wakeEnv(monkeypatch, leadB, kind='peer', text='【甲】在吗'):
    lead = leads_store.get(leadB['id'])
    lead['taskId'] = 'tk-wake'
    leads_store._save({lead['id']: lead})
    leads_inbox.append(leadB['id'], 'up', kind, text)

    class Repo:
        def load(self):
            pass

        def get(self, taskId):
            return {'id': taskId, 'status': 'running'}
    monkeypatch.setattr(leads_inbox.store, 'repo', Repo())
    monkeypatch.setattr(leads_inbox.config, 'getLeadWakeMergeSec',
                        lambda: 600)
    delivered = []
    import modules.tasks_api as tasks_api
    monkeypatch.setattr(tasks_api, 'deliverMessage',
                        lambda task, text, mode, by='': (
                            delivered.append(text) or ({'ok': True}, 200)))
    return delivered


def test_peer_wakes_without_merge_window(env, monkeypatch):
    delivered = _wakeEnv(monkeypatch, env['leadB'])
    leads_inbox._wakeHosted(leads_store.get(env['leadB']['id']),
                            time.time() + 1)
    assert len(delivered) == 1 and '在吗' in delivered[0]


def test_peer_removed_from_urgent_breaks_wake(env, monkeypatch):
    """改坏即红自证：peer 不在 URGENT_KINDS → 窗口内不投递。"""
    delivered = _wakeEnv(monkeypatch, env['leadB'])
    monkeypatch.setattr(leads_inbox, 'URGENT_KINDS',
                        tuple(k for k in leads_inbox.URGENT_KINDS
                              if k != 'peer'))
    leads_inbox._wakeHosted(leads_store.get(env['leadB']['id']),
                            time.time() + 1)
    assert delivered == []


def test_non_urgent_still_merges(env, monkeypatch):
    """对照组：普通 kind 在合并窗口内不投（旁路只对 urgent 生效）。"""
    delivered = _wakeEnv(monkeypatch, env['leadB'], kind='notice',
                         text='普通条目')
    leads_inbox._wakeHosted(leads_store.get(env['leadB']['id']),
                            time.time() + 1)
    assert delivered == []
