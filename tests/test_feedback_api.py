# -*- coding: utf-8 -*-
"""用户反馈接口：信封校验、幂等、附件、频控、状态机、检索、删除（v2 契约）。

批次 F1 后的契约变化（对照 _claude/调研-Deskport反馈现状-2026-11-15.md）：
    - submitter 由服务端写（信封自报 user 一概无效）；
    - 幂等作用域 = (submitter.id, idempotency_key)，feedback_id 撞号 409；
    - 频控持久化且系统所有者豁免（智能体 5/分钟、20/天，超限 429 + retry_after）；
    - 状态机按规格 §5 白名单：非法流转 409、必填 reason 缺失 400；
    - history 条目为 {from, to, actor, reason, at}。
闭环（评论/未读/确认/我的反馈/处理端）的验收在 tests/test_feedback_loop.py。
"""
import base64
import hashlib
import io
import json
import pathlib
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events, feedback_rate as rate
from modules import feedback_views as views

AGENT_TOKEN = 'fb-agent-' + 'b' * 20
AGENT_HASH = hashlib.sha256(AGENT_TOKEN.encode()).hexdigest()


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'stateDir', lambda: str(tmp_path / 'state'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    rate.resetRate()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    return SimpleNamespace(client=client)


def envelope(**over):
    # feedback_id 默认由幂等键派生：同一文件里多次提交互不撞号（真实客户端是随机 UUID）
    key = over.get('idempotency_key') or 'key-1'
    base = {
        'schema_version': '2',
        'feedback_id': 'fb-' + key,
        'idempotency_key': key,
        'submitted_at': '2026-02-06T10:30:00+08:00',
        'user': {'id': 'spoofed', 'role': 'teacher', 'anonymous': False},
        'client': {'platform': 'web', 'app_version': '2.3.1', 'user_agent': 'test'},
        'context': {'route': '/settings/notifications', 'page_id': 'SettingsPage',
                    'component_ref': 'NotifyToggle#email',
                    'viewport': {'width': 1920, 'height': 1080}, 'locale': 'zh-CN'},
        'trace': [{'t': 1250, 'action': 'click', 'target': 'button#save', 'ok': True},
                  {'t': 1380, 'action': 'http', 'target': 'POST /api/settings',
                   'status': 500, 'duration_ms': 3200}],
        'errors': {'console': ['TypeError: x'], 'failed_requests': []},
        'attachments': [],
        'message': '点保存没反应，转圈之后又弹回来了',
        'contact': '',
        'status': 'submitted',
    }
    base.update(over)
    return base


def upload(env, blob=PNG, name='shot.png'):
    return env.client.post('/api/feedback/attachments',
                           data={'file': (io.BytesIO(blob), name)},
                           content_type='multipart/form-data')


def submit(env, **over):
    return env.client.post('/api/feedback', json=envelope(**over))


def test_submit_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'stateDir', lambda: str(tmp_path / 'state'))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    assert app.test_client().post('/api/feedback', json=envelope()).status_code == 401


def test_submit_writes_submitter_and_forces_identity(env):
    resp = submit(env)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'submitted' and body['feedback_id']
    item = env.client.get('/api/feedback/' + body['feedback_id']).get_json()['feedback']
    # 身份由服务端说了算：网页会话 = 机器主人；信封自报的 user 一概无效
    assert item['submitter'] == {'kind': 'human', 'id': 'owner', 'display_name': '主人',
                                 'on_behalf_of': '', 'group': ''}
    assert item['message'] == '点保存没反应，转圈之后又弹回来了'
    assert item['route'] == '/settings/notifications'
    assert [t['action'] for t in item['trace']] == ['click', 'http']
    # history 条目按规格 §5：{from, to, actor, reason, at}
    first = item['history'][0]
    assert first['to'] == 'submitted' and first['actor'] == 'submitter' and first['from'] == ''


def test_v1_envelope_is_accepted_verbatim(env):
    """v1 信封照收（schema_version=1、带自报 user），submitter 仍由服务端写。"""
    resp = submit(env, schema_version='1', user={'id': 'someone', 'role': 'x', 'anonymous': True})
    assert resp.status_code == 200
    item = env.client.get('/api/feedback/' + resp.get_json()['feedback_id']).get_json()['feedback']
    assert item['submitter']['kind'] == 'human' and item['submitter']['id'] == 'owner'
    assert item['envelope']['schema_version'] == '1'


def test_required_fields_are_reported(env):
    cases = [
        ({'idempotency_key': ''}, 'idempotency_key'),
        ({'client': {'platform': 'web'}}, 'app_version'),
        ({'client': {'platform': 'tv', 'app_version': '1'}}, 'platform'),
        ({'context': {}}, 'context.route'),
        ({'message': 'x' * 501}, '500'),
        ({'message': ''}, 'message'),
        ({'contact': 'x' * 101}, 'contact'),
    ]
    for over, needle in cases:
        resp = submit(env, **over)
        assert resp.status_code == 400, over
        assert needle in resp.get_json()['error'], (over, resp.get_json())


def test_agent_platform_envelope(env):
    """v2 允许 client.platform=agent（规格 §2）。"""
    resp = submit(env, idempotency_key='k-agent-platform',
                  client={'platform': 'agent', 'app_version': 'skill-1.0', 'user_agent': 'curl'})
    assert resp.status_code == 200


@pytest.fixture
def bothEnv(tmp_path, monkeypatch):
    """会话 + 智能体令牌同一个 app、同一个状态目录：验跨身份语义用。"""
    monkeypatch.setattr(configMod, 'stateDir', lambda: str(tmp_path / 'state'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    rate.resetRate()
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 'a1', 'name': '验收智能体', 'tokenHash': AGENT_HASH}])
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    owner = app.test_client()
    with owner.session_transaction() as sess:
        sess['authenticated'] = True
    agent = app.test_client()          # 不带会话：只认令牌
    return SimpleNamespace(owner=owner, agent=agent, token=AGENT_TOKEN)


def test_idempotency_scoped_by_identity(bothEnv):
    """同一身份重复幂等键只落一条；不同身份用同一个键各落一条（规格 §4.2/§11.3）。"""
    first = bothEnv.owner.post('/api/feedback',
                               json=envelope(idempotency_key='same-key')).get_json()
    second = bothEnv.owner.post('/api/feedback',
                                json=envelope(idempotency_key='same-key',
                                              message='换了内容也算同一条')).get_json()
    assert second['feedback_id'] == first['feedback_id'] and second['duplicate'] is True
    # 智能体令牌用同一个键：独立落库，不再被静默吞掉（v1 全局键的缺陷）
    other = bothEnv.agent.post('/api/feedback', headers={'X-Agent-Token': AGENT_TOKEN},
                               json=envelope(idempotency_key='same-key',
                                             feedback_id='fb-agent-same'))
    assert other.status_code == 200
    assert other.get_json()['feedback_id'] != first['feedback_id']
    assert bothEnv.owner.get('/api/feedback').get_json()['total'] == 2


def test_feedback_id_collision_409(env):
    submit(env, feedback_id='fb-dup')
    clash = submit(env, idempotency_key='other-key', feedback_id='fb-dup')
    assert clash.status_code == 409 and 'feedback_id' in clash.get_json()['error']


def test_attachment_upload_and_reference(env):
    assert upload(env, b'not an image').status_code == 400
    assert upload(env, b'\x89PNG\r\n\x1a\n' + b'x' * (1024 * 1024)).status_code == 413
    ok = upload(env)
    assert ok.status_code == 200
    fileId = ok.get_json()['file_id']
    img = env.client.get('/api/feedback/attachments/' + fileId)
    assert img.status_code == 200 and img.mimetype == 'image/png'
    assert img.get_data() == PNG

    bad = submit(env, attachments=[{'file_id': 'att_0000000000000000', 'type': 'screenshot'}])
    assert bad.status_code == 400 and '附件不存在' in bad.get_json()['error']

    good = submit(env, idempotency_key='key-att',
                  attachments=[{'file_id': fileId, 'type': 'screenshot', 'has_annotations': True}])
    assert good.status_code == 200
    detail = env.client.get('/api/feedback/' + good.get_json()['feedback_id']).get_json()
    att = detail['feedback']['attachments'][0]
    assert att['file_id'] == fileId and att['has_annotations'] is True
    assert att['url'] == '/api/feedback/attachments/' + fileId


def test_attachment_cannot_be_reused_by_another_feedback(env):
    """附件被别的反馈引用后不能再引用（规格 §4.1：必须存在且未被占用）。"""
    fileId = upload(env).get_json()['file_id']
    assert submit(env, idempotency_key='att-1',
                  attachments=[{'file_id': fileId}]).status_code == 200
    again = submit(env, idempotency_key='att-2', attachments=[{'file_id': fileId}])
    assert again.status_code == 400 and '已被其他反馈引用' in again.get_json()['error']


def test_rate_limit_exempts_owner(env):
    """系统所有者（网页会话）不受频控（规格 §4.3）——单密码系统里限制主人只会堵死反馈。"""
    codes = [submit(env, idempotency_key='owner-%d' % i).status_code for i in range(8)]
    assert codes == [200] * 8


def test_status_machine(env):
    fid = submit(env).get_json()['feedback_id']
    assert env.client.patch('/api/feedback/%s/status' % fid,
                            json={'status': 'resolved'}).status_code == 409   # 跳过 acknowledged/processing
    r = env.client.patch('/api/feedback/%s/status' % fid,
                         json={'status': 'acknowledged', 'reason': '收到了'})
    assert r.status_code == 200 and r.get_json()['status'] == 'acknowledged'
    bad = env.client.patch('/api/feedback/%s/status' % fid, json={'status': 'rejected'})
    assert bad.status_code == 400 and '原因' in bad.get_json()['error']
    ok = env.client.patch('/api/feedback/%s/status' % fid,
                          json={'status': 'rejected', 'note': '不是 bug'})
    assert ok.status_code == 200                       # note 是旧名，照收
    item = env.client.get('/api/feedback/' + fid).get_json()['feedback']
    assert item['status'] == 'rejected' and item['status_note'] == '不是 bug'
    assert [h['to'] for h in item['history']] == ['submitted', 'acknowledged', 'rejected']
    assert item['history'][-1]['actor'] == 'handler' and item['history'][-1]['reason'] == '不是 bug'
    assert env.client.patch('/api/feedback/nope/status',
                            json={'status': 'processing'}).status_code == 404


def test_search_filters_and_paging(env):
    submit(env, idempotency_key='s1', context={'route': '/files/a'})
    submit(env, idempotency_key='s2', submitted_at='2026-02-07T09:00:00+08:00',
           context={'route': '/tasks'})
    assert env.client.get('/api/feedback?route=/files').get_json()['total'] == 1
    assert env.client.get('/api/feedback?date_from=2026-02-07').get_json()['total'] == 1
    page = env.client.get('/api/feedback?size=1&page=2').get_json()
    assert page['total'] == 2 and len(page['items']) == 1
    assert env.client.get('/api/feedback?status=resolved').get_json()['total'] == 0


def test_delete_removes_attachment(env):
    fileId = upload(env).get_json()['file_id']
    fid = submit(env, attachments=[{'file_id': fileId}]).get_json()['feedback_id']
    assert env.client.delete('/api/feedback/' + fid).status_code == 200
    assert env.client.get('/api/feedback/' + fid).status_code == 404
    assert env.client.get('/api/feedback/attachments/' + fileId).status_code == 404
    assert env.client.delete('/api/feedback/' + fid).status_code == 404


def test_stats(env):
    upload(env)
    submit(env)
    body = env.client.get('/api/feedback/stats').get_json()
    assert body['count'] == 1 and body['byStatus'] == {'submitted': 1}
    assert body['pending'] == 1 and 'overdue' in body
    assert body['attachmentCount'] == 1 and body['attachmentBytes'] > 0


def test_envelope_is_stored_verbatim_including_unknown_fields(env):
    env_raw = envelope(idempotency_key='verbatim', extra_field={'a': 1})
    assert env.client.post('/api/feedback', data=json.dumps(env_raw),
                           content_type='application/json').status_code == 200
    fid = env.client.get('/api/feedback?route=/settings').get_json()['items'][0]['feedback_id']
    raw = json.loads(pathlib.Path(configMod.statePath('feedback.json')).read_text(encoding='utf-8'))
    stored = next(i for i in raw['items'] if i['feedback_id'] == fid)
    assert stored['message'] == env_raw['message']            # 正文原样，不打码不改写
    assert stored['envelope']['extra_field'] == {'a': 1}      # 未知字段也不丢
    # 服务端补写 received_at 与 submitter（规格 §2）；客户端自报 user/status 不落信封
    assert stored['envelope']['submitter']['id'] == 'owner'
    assert 'user' not in stored['envelope'] and 'status' not in stored['envelope']


# ---------------------------------------------------------------- 智能体通道

@pytest.fixture
def agentEnv(tmp_path, monkeypatch):
    """令牌通道: 配好智能体令牌, 无会话登录。"""
    monkeypatch.setattr(configMod, 'stateDir', lambda: str(tmp_path / 'state'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    rate.resetRate()
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 'a1', 'name': '验收智能体', 'tokenHash': AGENT_HASH}])
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client())


def test_agent_token_can_submit_feedback(agentEnv):
    resp = agentEnv.client.post('/api/feedback', headers={'X-Agent-Token': AGENT_TOKEN},
                                json=envelope(idempotency_key='agent-1'))
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'submitted'
    # 读接口是处理者/本人权限（下一条测试与 test_feedback_loop.py）, 这里直接查存储确认身份与正文
    fid = resp.get_json()['feedback_id']
    stored = views.get(fid)
    assert stored['submitter'] == {'kind': 'agent', 'id': '验收智能体',
                                   'display_name': '验收智能体', 'on_behalf_of': '', 'group': ''}
    assert stored['message'] == '点保存没反应，转圈之后又弹回来了'
    raw = json.loads(pathlib.Path(configMod.statePath('feedback.json')).read_text(encoding='utf-8'))
    item = next(i for i in raw['items'] if i['feedback_id'] == fid)
    assert item['role'] == 'agent'                       # 服务端定身份，不用信封自报的


def test_agent_feedback_rejects_spoofed_identity(agentEnv):
    """信封里自报的 user.id / anonymous 都不采信, 一律按令牌名实名。"""
    resp = agentEnv.client.post('/api/feedback', headers={'X-Agent-Token': AGENT_TOKEN},
                                json=envelope(idempotency_key='agent-2',
                                              user={'id': 'owner', 'role': 'owner',
                                                    'anonymous': True}))
    assert resp.status_code == 200
    stored = views.get(resp.get_json()['feedback_id'])
    assert stored['submitter']['id'] == '验收智能体'
    assert stored['submitter']['kind'] == 'agent'


def test_agent_feedback_bad_token(agentEnv):
    for headers in ({}, {'X-Agent-Token': 'wrong'}):
        resp = agentEnv.client.post('/api/feedback', headers=headers,
                                    json=envelope(idempotency_key='agent-bad'))
        assert resp.status_code == 401, headers


def test_agent_token_cannot_reach_handler_endpoints(agentEnv):
    """检索/统计/待受理摘要/状态回写/删除仍是主人会话独有（feedback:triage 在 F3）。"""
    assert agentEnv.client.get('/api/feedback',
                               headers={'X-Agent-Token': AGENT_TOKEN}).status_code == 401
    assert agentEnv.client.get('/api/feedback/stats',
                               headers={'X-Agent-Token': AGENT_TOKEN}).status_code == 401
    assert agentEnv.client.get('/api/feedback/pending',
                               headers={'X-Agent-Token': AGENT_TOKEN}).status_code == 401
    assert agentEnv.client.patch('/api/feedback/x/status', headers={'X-Agent-Token': AGENT_TOKEN},
                                 json={'status': 'processing'}).status_code == 401
    assert agentEnv.client.delete('/api/feedback/x',
                                  headers={'X-Agent-Token': AGENT_TOKEN}).status_code == 401
