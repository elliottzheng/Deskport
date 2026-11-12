# -*- coding: utf-8 -*-
"""用户反馈接口：信封校验、幂等、附件、频控、状态机、检索、删除。"""
import base64
import io
import json
import pathlib
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events, feedback_store as store

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(configMod, 'stateDir', lambda: str(tmp_path / 'state'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(events, 'log', lambda *a, **k: None)
    store.resetRate()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    return SimpleNamespace(client=client)


def envelope(**over):
    base = {
        'schema_version': '1.0',
        'feedback_id': 'fb_test_1',
        'idempotency_key': 'key-1',
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


def test_submit_stores_envelope_and_forces_identity(env):
    resp = submit(env)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'submitted' and body['feedback_id']
    item = env.client.get('/api/feedback/' + body['feedback_id']).get_json()['feedback']
    # 身份由服务端说了算，客户端自报的 user.id 不被采信
    assert item['user_id'] == 'owner'
    assert item['message'] == '点保存没反应，转圈之后又弹回来了'
    assert item['route'] == '/settings/notifications'
    assert [t['action'] for t in item['trace']] == ['click', 'http']
    assert item['history'][0]['status'] == 'submitted'


def test_required_fields_are_reported(env):
    cases = [
        ({'idempotency_key': ''}, 'idempotency_key'),
        ({'client': {'platform': 'web'}}, 'app_version'),
        ({'client': {'platform': 'tv', 'app_version': '1'}}, 'platform'),
        ({'context': {}}, 'context.route'),
        ({'message': 'x' * 501}, '500'),
    ]
    for over, needle in cases:
        resp = submit(env, **over)
        assert resp.status_code == 400, over
        assert needle in resp.get_json()['error'], (over, resp.get_json())


def test_idempotency_returns_first_result(env):
    first = submit(env).get_json()
    second = submit(env, message='换了内容也算同一条').get_json()
    assert second['feedback_id'] == first['feedback_id']
    assert second.get('duplicate') is True
    listing = env.client.get('/api/feedback').get_json()
    assert listing['total'] == 1


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


def test_rate_limit_per_minute(env):
    codes = [submit(env, idempotency_key='rl-%d' % i).status_code for i in range(6)]
    assert codes[:5] == [200] * 5
    assert codes[5] == 429
    assert '频繁' in env.client.post('/api/feedback', json=envelope(idempotency_key='rl-x')).get_json()['error']


def test_status_machine(env):
    fid = submit(env).get_json()['feedback_id']
    assert env.client.patch('/api/feedback/%s/status' % fid,
                            json={'status': 'resolved'}).status_code == 400   # 不能跳过加工
    r = env.client.patch('/api/feedback/%s/status' % fid,
                         json={'status': 'acknowledged', 'note': '收到了'})
    assert r.status_code == 200 and r.get_json()['status'] == 'acknowledged'
    bad = env.client.patch('/api/feedback/%s/status' % fid, json={'status': 'rejected'})
    assert bad.status_code == 400 and '原因' in bad.get_json()['error']
    ok = env.client.patch('/api/feedback/%s/status' % fid,
                          json={'status': 'rejected', 'note': '不是 bug'})
    assert ok.status_code == 200
    item = env.client.get('/api/feedback/' + fid).get_json()['feedback']
    assert item['status'] == 'rejected' and item['status_note'] == '不是 bug'
    assert [h['status'] for h in item['history']] == ['submitted', 'acknowledged', 'rejected']
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