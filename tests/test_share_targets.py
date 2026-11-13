# -*- coding: utf-8 -*-
"""分享「只上传」的机器维度：目标列表 + 跨机上传（批次 E）。

覆盖: /api/share/targets（本机/远端/离线/路径不出网）、submit way=upload
的 node/target 分支（本机不变、远端投递、非法 node/target 400、成员端
缺目标 id 400 且文案明确）、upload_remote 授权门（按 stage 令牌）、
成员端 /api/agent/upload[/targets] 接受联邦身份、错误文本抹掉绝对路径。
全部桩化，不碰真实机器与真实配置。
"""
import hashlib
import io
import json
import threading
from types import SimpleNamespace

import pytest

import config as configMod
from modules import events as eventsMod
from modules import share_targets
from modules import share_web
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

TOKEN_SHARE = 'tok-share-' + 'd' * 16          # 只有 share
TOKEN_REMOTE = 'tok-remote-' + 'r' * 16        # share + upload_remote
FED_TOKEN = 'fed-token-' + 'f' * 16

REMOTE = {'id': 'i7', 'name': 'win4710', 'base': 'http://127.0.0.1:1',
          'token': 'fed-token-' + 'f' * 16, 'terminal': False, 'apps': []}
NODE = {'name': 'ai-xmyz', 'url': 'http://127.0.0.1:2', 'token': 'node-token',
        'via': '', 'remote': ''}


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []

    def fakeStatePath(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 'share', 'name': '手机分享', 'tokenHash': _hash(TOKEN_SHARE),
         'scopes': ['share']},
        {'id': 'remote', 'name': '远端分享', 'tokenHash': _hash(TOKEN_REMOTE),
         'scopes': ['share', 'upload_remote']},
    ])
    monkeypatch.setattr(configMod, 'statePath', fakeStatePath)
    monkeypatch.setattr(configMod, 'getUploadTargets', lambda: [
        {'id': 'phone', 'name': '手机上传', 'path': str(tmp_path / 'phone')},
        {'id': 'jyzl', 'name': '教研资料', 'path': str(tmp_path / 'jyzl')},
    ])
    monkeypatch.setattr(configMod, 'getPhoneUploadMaxMB', lambda: 20)
    monkeypatch.setattr(configMod, 'getFederationToken', lambda: FED_TOKEN)
    monkeypatch.setattr(configMod, 'getRemotes', lambda: [dict(REMOTE)])
    monkeypatch.setattr(configMod, 'getAgentNodes', lambda: [dict(NODE)])
    monkeypatch.setattr(eventsMod, 'log',
                        lambda action, target='', result='ok', detail='', ip='':
                        audit.append({'action': action, 'target': target,
                                      'result': result, 'detail': detail}))
    share_targets._cache.clear()
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    return SimpleNamespace(client=client, audit=audit, repo=repo, tmp=tmp_path,
                           app=app)


def stage(env, files=(), token=TOKEN_REMOTE):
    data = {'file': [(io.BytesIO(raw), name) for name, raw in files]} if files else {}
    return env.client.post('/api/agent/share/stage', data=data,
                           headers={'X-Agent-Token': token},
                           content_type='multipart/form-data')


def staged(env, files=(), token=TOKEN_REMOTE):
    resp = stage(env, files=files, token=token)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def submit(env, ticket, **body):
    body['ticket'] = ticket
    return env.client.post('/api/share/submit', json=body)


def batches(env):
    root = env.tmp / 'taskroot' / '_uploads'
    return list(root.iterdir()) if root.is_dir() else []


# ---------------------------------------------------------------- 目标列表

def test_targets_require_login(env):
    anon = env.app.test_client()
    assert anon.get('/api/share/targets').status_code == 401


def test_targets_merge_local_and_remote_without_paths(env, monkeypatch):
    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传',
                                       'path': 'C:\\secret'}])
    body = env.client.get('/api/share/targets').get_json()
    local = [t for t in body['targets'] if t['node'] == 'local']
    assert [t['id'] for t in local] == ['phone', 'jyzl']
    assert all(t['status'] == 'online' for t in local)
    remote = [t for t in body['targets'] if t['node'] == 'win4710']
    assert remote == [{'node': 'win4710', 'id': 'phone',
                       'name': '手机上传', 'status': 'online'}]
    assert {'name': 'win4710', 'label': 'win4710',
            'status': 'online'} in body['nodes']
    assert body['default'] == {'node': 'local', 'id': 'phone'}
    text = json.dumps(body, ensure_ascii=False)
    assert str(env.tmp) not in text and 'C:\\\\secret' not in text
    assert '"path"' not in text


def test_offline_machine_stays_visible_and_keeps_cached_targets(env, monkeypatch):
    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传'}])
    first = env.client.get('/api/share/targets').get_json()
    assert any(t['node'] == 'win4710' and t['status'] == 'online'
               for t in first['targets'])

    def dead(name):
        raise share_targets.TargetError('成员机不可达')

    monkeypatch.setattr(share_targets, '_fetchNode', dead)
    monkeypatch.setattr(share_targets, 'CACHE_TTL', 0)
    second = env.client.get('/api/share/targets').get_json()
    rows = [t for t in second['targets'] if t['node'] == 'win4710']
    assert rows and all(t['status'] == 'offline' for t in rows)
    assert {'name': 'win4710', 'label': 'win4710',
            'status': 'offline'} in second['nodes']
    assert any(n['name'] == 'ai-xmyz' and n['status'] == 'offline'
               for n in second['nodes'])


def test_agent_node_connection_error_marks_offline(env, monkeypatch):
    from modules import agent_nodes

    def boom(*_args, **_kwargs):
        raise OSError('tunnel down')

    monkeypatch.setattr(agent_nodes, 'nodeRequest', boom)
    body = env.client.get('/api/share/targets').get_json()
    node = [n for n in body['nodes'] if n['name'] == 'ai-xmyz'][0]
    assert node['status'] == 'offline'


# ---------------------------------------------------------------- 只上传：本机

def test_local_upload_explicit_target(env):
    body = staged(env, files=[('卷一.pdf', b'one')])
    resp = submit(env, body['ticket'], way='upload', node='local',
                  target='jyzl', subdir='周测卷')
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out['node'] == 'local' and out['target'] == 'jyzl'
    assert out['uploaded'][0]['path'] == '教研资料/周测卷/卷一.pdf'
    assert (env.tmp / 'jyzl' / '周测卷' / '卷一.pdf').is_file()
    assert batches(env) == []


def test_local_upload_unknown_target_400_ticket_retryable(env):
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='local', target='nope')
    assert resp.status_code == 400
    out = resp.get_json()
    assert out['error'] == '上传目标不存在'
    assert [t['id'] for t in out['targets']] == ['phone', 'jyzl']
    assert 'path' not in json.dumps(out)
    assert submit(env, body['ticket'], way='upload').status_code == 200


# ---------------------------------------------------------------- 只上传：远端

def test_remote_upload_requires_upload_remote_scope(env):
    body = staged(env, files=[('a.txt', b'x')], token=TOKEN_SHARE)
    resp = submit(env, body['ticket'], way='upload', node='win4710',
                  target='phone')
    assert resp.status_code == 403
    out = resp.get_json()
    assert out['need'] == 'upload_remote' and '权限' in out['error']
    # 票据没被烧掉：换成本机还能提交
    assert submit(env, body['ticket'], way='upload').status_code == 200


def test_remote_upload_delivers_through_member(env, monkeypatch):
    seen = {}

    def fakeSend(node, target, subdir, files):
        seen.update(node=node, target=target, subdir=subdir, files=list(files))
        return {'ok': True, 'saved': [{'path': '手机上传/周测卷/a.txt',
                                       'size': 1}], 'failed': []}

    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传'}])
    monkeypatch.setattr(share_targets, 'sendFiles', fakeSend)
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='win4710',
                  target='phone', subdir='周测卷')
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out['ok'] is True and out['node'] == 'win4710'
    assert out['uploaded'] == [{'path': '手机上传/周测卷/a.txt', 'size': 1}]
    assert seen['node'] == 'win4710' and seen['target'] == 'phone'
    assert seen['subdir'] == '周测卷' and len(seen['files']) == 1
    assert batches(env) == []


def test_remote_upload_unknown_machine_400(env):
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='ghost',
                  target='phone')
    assert resp.status_code == 400
    assert '机器不存在' in resp.get_json()['error']


def test_remote_upload_missing_target_400(env, monkeypatch):
    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传'}])
    called = []
    monkeypatch.setattr(share_targets, 'sendFiles',
                        lambda *a: called.append(a) or {})
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='win4710',
                  target='nope')
    assert resp.status_code == 400
    error = resp.get_json()['error']
    assert '没有这个上传目标' in error and 'win4710' in error
    assert called == []


def test_remote_upload_member_rejects_target_id_400(env, monkeypatch):
    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传'}])

    def reject(*_args):
        raise share_targets.TargetError(
            '成员机 win4710 上没有这个上传目标（phone）', 400)

    monkeypatch.setattr(share_targets, 'sendFiles', reject)
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='win4710',
                  target='phone')
    assert resp.status_code == 400
    assert '没有这个上传目标' in resp.get_json()['error']


def test_remote_upload_offline_machine_502(env, monkeypatch):
    monkeypatch.setattr(share_targets, 'CACHE_TTL', 0)

    def dead(name):
        raise share_targets.TargetError('成员机不可达')

    monkeypatch.setattr(share_targets, '_fetchNode', dead)
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='win4710',
                  target='phone')
    assert resp.status_code == 502
    assert '连不上' in resp.get_json()['error']


def test_remote_failed_text_never_leaks_paths(env, monkeypatch):
    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传'}])
    monkeypatch.setattr(share_targets, 'sendFiles', lambda *a: {
        'ok': False, 'saved': [],
        'failed': [{'name': 'a.txt',
                    'error': '写盘失败: D:\\secret\\a.txt'}]})
    body = staged(env, files=[('a.txt', b'x')])
    resp = submit(env, body['ticket'], way='upload', node='win4710',
                  target='phone')
    text = json.dumps(resp.get_json(), ensure_ascii=False)
    assert 'D:\\secret' not in text and 'secret' not in text
    assert resp.status_code == 502


# ---------------------------------------------------------------- 成员端

def test_member_targets_accept_federation_token(env):
    assert env.client.get('/api/agent/upload/targets').status_code == 401
    resp = env.client.get('/api/agent/upload/targets',
                          headers={'X-Deskport-Token': FED_TOKEN})
    assert resp.status_code == 200
    body = resp.get_json()
    assert [t['id'] for t in body['targets']] == ['phone', 'jyzl']
    assert 'path' not in json.dumps(body)


def test_member_upload_accepts_federation_and_maps_target(env):
    resp = env.client.post(
        '/api/agent/upload',
        data={'file': (io.BytesIO(b'hello'), 'a.txt'),
              'target': 'jyzl', 'subdir': '11月'},
        headers={'X-Deskport-Token': FED_TOKEN,
                 'X-Deskport-From': 'Deskport'},
        content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['saved'][0]['path'] == '教研资料/11月/a.txt'
    assert (env.tmp / 'jyzl' / '11月' / 'a.txt').read_bytes() == b'hello'
    text = json.dumps(body, ensure_ascii=False)
    assert str(env.tmp) not in text
    assert any('联邦' in a['target'] for a in env.audit
               if a['action'] == 'agent.upload')


def test_member_upload_unknown_target_400(env):
    resp = env.client.post(
        '/api/agent/upload',
        data={'file': (io.BytesIO(b'x'), 'a.txt'), 'target': 'nope'},
        headers={'X-Deskport-Token': FED_TOKEN},
        content_type='multipart/form-data')
    assert resp.status_code == 400
    assert resp.get_json()['error'] == '上传目标不存在'


def test_scrub_removes_absolute_paths():
    assert 'D:\\' not in share_targets._scrub('写盘失败: D:\\x\\a.txt')
    assert '\\\\nas\\share' not in share_targets._scrub('失败 \\\\nas\\share\\a.txt')
    assert '/var/log/a' not in share_targets._scrub('失败 /var/log/a')
    assert share_targets._scrub('手机上传/周测/a.txt') == '手机上传/周测/a.txt'


def test_page_has_machine_and_target_only_in_upload_panel(env):
    html = env.client.get('/m/share?ticket=abc').get_data(as_text=True)
    for marker in ('id="share-upload-node"', 'id="share-upload-target"',
                   'id="share-upload-target-empty"', 'id="share-subdir"'):
        assert marker in html, marker
    newPanel = html.split('id="panel-new"')[1].split('</section>')[0]
    assert 'share-upload-node' not in newPanel
    assert 'share-upload-target' not in newPanel


def test_remote_upload_audit_records_node_and_target(env, monkeypatch):
    monkeypatch.setattr(share_targets, '_fetchNode',
                        lambda name: [{'id': 'phone', 'name': '手机上传'}])
    monkeypatch.setattr(share_targets, 'sendFiles', lambda *a: {
        'ok': True, 'saved': [{'path': '手机上传/a.txt', 'size': 1}],
        'failed': []})
    body = staged(env, files=[('a.txt', b'x')])
    submit(env, body['ticket'], way='upload', node='win4710', target='phone')
    entry = [a for a in env.audit if a['action'] == 'share.submit'][-1]
    assert 'node=win4710' in entry['detail'] and 'target=phone' in entry['detail']


# ---------------------------------------------------------------- 端到端线络

def test_remote_upload_over_real_http_to_member(env, monkeypatch):
    """真的走 HTTP：中枢的 requests → 成员 Flask 的 /api/agent/upload。"""
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', 0, env.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = 'http://127.0.0.1:%d' % server.server_port
        monkeypatch.setattr(configMod, 'getRemotes',
                            lambda: [dict(REMOTE, base=base)])
        targets = env.client.get('/api/share/targets').get_json()
        rows = [t for t in targets['targets'] if t['node'] == 'win4710']
        assert rows and all(t['status'] == 'online' for t in rows)
        assert [t['id'] for t in rows] == ['phone', 'jyzl']
        assert str(env.tmp) not in json.dumps(targets, ensure_ascii=False)

        body = staged(env, files=[('卷一.pdf', b'one')])
        resp = submit(env, body['ticket'], way='upload', node='win4710',
                      target='jyzl', subdir='周测卷')
        assert resp.status_code == 200, resp.get_json()
        out = resp.get_json()
        assert out['uploaded'] == [{'path': '教研资料/周测卷/卷一.pdf',
                                    'size': 3}]
        assert str(env.tmp) not in json.dumps(out, ensure_ascii=False)
        assert (env.tmp / 'jyzl' / '周测卷' / '卷一.pdf').read_bytes() == b'one'
    finally:
        server.shutdown()
        thread.join(timeout=5)