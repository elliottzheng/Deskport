# -*- coding: utf-8 -*-
"""批次 F1 验收套件（二）：处理端视图与迁移（规格 §6.1 + 线上 9 条 v1 记录）。

    - 批量受理、待受理摘要、超时未受理（>24h）；
    - 检索筛选（状态/路由/提交者/归属/超时）与「待受理优先」排序；
    - submitter.group / on_behalf_of 服务端落库（调研 S5：归属不再丢失）；
    - 迁移：线上 9 条 v1 记录的结构副本（合成文本）读入、列表、详情正常。
闭环部分（状态机/未读/评论/智能体）在 tests/test_feedback_loop.py。
"""
import hashlib
import json
import pathlib
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events, feedback_rate as rate, feedback_store as store

AGENT_TOKEN = 'f1-agent-' + 'c' * 20
AGENT_HASH = hashlib.sha256(AGENT_TOKEN.encode()).hexdigest()
MIGRATION_FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'feedback_v1_migration.json'


@pytest.fixture
def both(tmp_path, monkeypatch):
    """owner（网页会话=主人/处理者）+ agent（无会话、只带令牌），同一状态目录。"""
    monkeypatch.setattr(configMod, 'stateDir', lambda: str(tmp_path / 'state'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    rate.resetRate()
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'a1', 'name': '验收智能体', 'tokenHash': AGENT_HASH,
         'onBehalfOf': '教务处', 'lead': 'talent'}])
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    owner = app.test_client()
    with owner.session_transaction() as sess:
        sess['authenticated'] = True
    agent = app.test_client()
    return SimpleNamespace(owner=owner, agent=agent, token=AGENT_TOKEN)


def envelope(**over):
    n = over.pop('_n', 'x')
    base = {
        'schema_version': '2',
        'feedback_id': 'fb-' + n,
        'idempotency_key': 'k-' + n,
        'submitted_at': '2026-11-15T10:00:00+08:00',
        'client': {'platform': 'web', 'app_version': 'f1-test', 'user_agent': 'pytest'},
        'context': {'route': '/settings', 'page_id': 'SettingsPage'},
        'message': '保存没反应', 'contact': '',
    }
    base.update(over)
    return base


def submitAgent(both, **over):
    return both.agent.post('/api/feedback', headers={'X-Agent-Token': AGENT_TOKEN},
                           json=envelope(**over))


def submitOwner(both, **over):
    return both.owner.post('/api/feedback', json=envelope(**over))


def patchOwner(both, fid, **body):
    return both.owner.patch('/api/feedback/%s/status' % fid, json=body)


def write_fixture():
    """把线上 9 条 v1 记录的结构副本（合成文本）写进状态目录当存量。"""
    data = json.loads(MIGRATION_FIXTURE.read_text(encoding='utf-8'))
    path = pathlib.Path(configMod.statePath('feedback.json'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


# ---------------------------------------------------------------- 批量受理（§6.1）

def test_batch_acknowledge(both):
    ids = [submitAgent(both, _n='b%d' % i).get_json()['feedback_id'] for i in range(3)]
    resp = both.owner.post('/api/feedback/batch', json={'ids': ids})
    assert resp.status_code == 200
    assert sorted(resp.get_json()['ok']) == sorted(ids)
    again = both.owner.post('/api/feedback/batch', json={'ids': ids}).get_json()
    assert again['ok'] == [] and len(again['skipped']) == 3
    mixed = both.owner.post('/api/feedback/batch',
                            json={'ids': ids + ['nope']}).get_json()
    assert mixed['ok'] == [] and mixed['skipped'][-1]['reason'] == '不存在'
    assert both.owner.post('/api/feedback/batch', json={'ids': []}).status_code == 400


def test_batch_marks_submitter_unread(both):
    fid = submitAgent(both, _n='bu').get_json()['feedback_id']
    both.owner.post('/api/feedback/batch', json={'ids': [fid]})
    mine = both.agent.get('/api/feedback/mine',
                          headers={'X-Agent-Token': AGENT_TOKEN}).get_json()
    assert mine['unread'] == 1


# ---------------------------------------------------------------- 待受理摘要（§6.1 首页数）

def test_pending_summary_shape(both):
    for i in range(3):
        submitAgent(both, _n='ps%d' % i)
    fid = submitAgent(both, _n='ps3').get_json()['feedback_id']
    patchOwner(both, fid, status='acknowledged')
    body = both.owner.get('/api/feedback/pending').get_json()
    assert body['pending'] == 3 and body['overdue'] == 0
    assert len(body['recent']) == 4
    assert body['recent'][0]['submitter']['id'] == '验收智能体'
    assert set(body['recent'][0]) >= {'feedback_id', 'status', 'route', 'submitter', 'message'}
    assert len(body['recent'][0]['message']) <= 50


def test_stats_carries_pending_and_overdue(both):
    submitAgent(both, _n='st1')
    submitOwner(both, _n='st2')
    body = both.owner.get('/api/feedback/stats').get_json()
    assert body['count'] == 2 and body['pending'] == 2
    assert body['byStatus'] == {'submitted': 2} and body['overdue'] == 0


# ---------------------------------------------------------------- 超时未受理（§5/§6.1）

def test_overdue_flag_and_filter(both):
    fid = submitAgent(both, _n='od').get_json()['feedback_id']
    stale = time.strftime('%Y-%m-%d %H:%M:%S',
                          time.localtime(store.nowSeconds() - 25 * 3600))
    with store.locked():
        data = store.load()
        for item in data['items']:
            if item['feedback_id'] == fid:
                item['received_at'] = stale
        store.save(data)
    listing = both.owner.get('/api/feedback?overdue=1').get_json()
    assert listing['total'] == 1 and listing['items'][0]['overdue'] is True
    assert both.owner.get('/api/feedback/pending').get_json()['overdue'] == 1
    assert both.owner.get('/api/feedback/pending').get_json()['pending'] == 1
    # 受理后不再算超时
    patchOwner(both, fid, status='acknowledged')
    assert both.owner.get('/api/feedback?overdue=1').get_json()['total'] == 0


# ---------------------------------------------------------------- 检索（§6.1）

def test_search_filters_submitter_and_group(both):
    submitAgent(both, _n='f1', context={'route': '/tasks'})
    submitOwner(both, _n='f2', context={'route': '/files'})
    assert both.owner.get('/api/feedback?route=/tasks').get_json()['total'] == 1
    assert both.owner.get('/api/feedback?submitter=验收').get_json()['total'] == 1
    assert both.owner.get('/api/feedback?submitter=主人').get_json()['total'] == 1
    assert both.owner.get('/api/feedback?group=talent').get_json()['total'] == 1
    assert both.owner.get('/api/feedback?group=nobody').get_json()['total'] == 0
    assert both.owner.get('/api/feedback?status=submitted').get_json()['total'] == 2


def test_search_orders_pending_first(both):
    old = submitAgent(both, _n='o1').get_json()['feedback_id']
    patchOwner(both, old, status='acknowledged')
    new = submitAgent(both, _n='o2').get_json()['feedback_id']
    items = both.owner.get('/api/feedback').get_json()['items']
    assert [i['feedback_id'] for i in items] == [new, old]


# ---------------------------------------------------------------- 归属（调研 S5）

def test_submitter_carries_group_and_on_behalf_of(both):
    submitAgent(both, _n='lead')
    item = both.owner.get('/api/feedback?submitter=验收').get_json()['items'][0]
    assert item['submitter']['group'] == 'talent'
    assert item['submitter']['on_behalf_of'] == '教务处'
    assert item['submitter']['kind'] == 'agent'


# ---------------------------------------------------------------- v1 迁移（线上 9 条）

def test_migration_v1_records_read_list_and_detail(both):
    write_fixture()
    listing = both.owner.get('/api/feedback?size=100').get_json()
    assert listing['total'] == 9
    assert all(i['status'] == 'submitted' for i in listing['items'])
    assert {i['submitter']['kind'] for i in listing['items']} == {'human', 'agent'}
    for item in listing['items']:
        detail = both.owner.get('/api/feedback/' + item['feedback_id'])
        assert detail.status_code == 200
        body = detail.get_json()['feedback']
        assert body['message'].startswith('合成迁移文本')
        assert body['envelope']['schema_version'] == '1.0'
        assert len(body['history']) == 1 and body['history'][0]['status'] == 'submitted'
        assert body['comments'] == [] and body['unread'] is False


def test_migration_v1_agent_mine_and_overdue(both, monkeypatch):
    write_fixture()
    # 夹具里没有「验收智能体」提的：mine 为空
    mine = both.agent.get('/api/feedback/mine',
                          headers={'X-Agent-Token': AGENT_TOKEN}).get_json()
    assert mine['total'] == 0
    # 线上 4 条是「外部智能体-拔尖人才」：换个同名令牌看得到自己的 4 条
    other = 'f1-agent-' + 'd' * 20
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'a2', 'name': '外部智能体-拔尖人才',
         'tokenHash': hashlib.sha256(other.encode()).hexdigest()}])
    mine = both.agent.get('/api/feedback/mine',
                          headers={'X-Agent-Token': other}).get_json()
    assert mine['total'] == 4
    assert all(i['submitter']['kind'] == 'agent' for i in mine['items'])
    assert all(i['submitter']['group'] == '' for i in mine['items'])   # v1 丢失的归属补不回来
    # 线上记录跨约 39 小时窗口：received_at 早于 24h 前的都算超时未受理
    cutoff = time.time() - 24 * 3600
    data = json.loads(MIGRATION_FIXTURE.read_text(encoding='utf-8'))
    expected = sum(1 for it in data['items']
                   if time.mktime(time.strptime(it['received_at'], '%Y-%m-%d %H:%M:%S')) < cutoff)
    assert expected >= 1
    assert both.owner.get('/api/feedback/pending').get_json()['overdue'] == expected
