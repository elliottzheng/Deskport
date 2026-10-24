# -*- coding: utf-8 -*-
"""P0 回归：拆分断点（aihandoff 复制 / control 审计）与路由级验证。"""
import os

import pytest

import config
from modules import aihandoff, control_base


@pytest.fixture()
def client():
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


@pytest.fixture()
def workdirs(tmp_path, monkeypatch):
    root = tmp_path / 'work'
    fallback = tmp_path / 'fallback'
    root.mkdir()
    fallback.mkdir()
    monkeypatch.setattr(config, 'getWorkDirs', lambda: [str(root)])
    monkeypatch.setattr(config, 'getFallbackDir', lambda: str(fallback))
    return root, fallback


def test_audit_is_side_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(control_base.events, 'log',
                        lambda *args, **kwargs: calls.append(args) or {'ok': True})
    assert control_base._audit('probe') == {'ok': True}
    assert calls and calls[0][-1] == ''      # 无请求上下文时 ip 为空, 不抛异常


def test_audit_inside_request_context(monkeypatch):
    from app import app
    calls = []
    monkeypatch.setattr(control_base.events, 'log',
                        lambda *args, **kwargs: calls.append(args))
    with app.test_request_context('/'):
        control_base._audit('probe', 'target')
    assert calls and calls[0][-1] in ('127.0.0.1', None, '')


def test_invalid_process_close_is_400(client):
    resp = client.post('/api/control/process/close', json={'pid': 'not-a-pid'})
    assert resp.status_code == 400


def test_power_dry_run_does_not_audit(client, monkeypatch):
    calls = []
    monkeypatch.setattr(control_base.events, 'log',
                        lambda *args, **kwargs: calls.append(args))
    resp = client.post('/api/control/power', json={'action': 'shutdown', 'dryRun': True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['dryRun'] is True
    assert data['cmd'] == ['shutdown', '/s', '/t', '0']
    assert calls == []


def test_keys_dry_run_returns_action(client):
    resp = client.post('/api/control/keys', json={'action': 'mute', 'dryRun': True})
    assert resp.status_code == 200
    assert resp.get_json()['action'] == 'mute'


def test_task_route_404_uses_decorator(client):
    resp = client.get('/api/tasks/not-exist')
    assert resp.status_code == 404
    assert resp.get_json()['error'] == '任务不存在'


def test_ai_case_copy_skip_and_rename(client, workdirs, tmp_path):
    root, _fallback = workdirs
    src = tmp_path / '报告.txt'
    src.write_text('v1', encoding='utf-8')

    resp = client.post('/api/ai/case', json={
        'task': '整理材料', 'files': [str(src)], 'mode': 'new', 'dest': str(root)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data['copied'] == 1 and data['skipped'] == 0
    caseDir = data['dir']
    assert os.path.isfile(os.path.join(caseDir, '报告.txt'))

    # 同名同内容: 跳过 (覆盖 _sameContent/HASH_LIMIT)
    resp = client.post('/api/ai/case', json={
        'task': '整理材料', 'files': [str(src)], 'mode': 'existing', 'dest': caseDir})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['skipped'] == 1

    # 同名不同内容: 加序号复制 (覆盖 stat/chmod)
    src.write_text('v2', encoding='utf-8')
    resp = client.post('/api/ai/case', json={
        'task': '整理材料', 'files': [str(src)], 'mode': 'existing', 'dest': caseDir})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['copied'] == 1
    assert os.path.isfile(os.path.join(caseDir, '报告 (1).txt'))


def test_ai_case_rejects_dest_outside_workdirs(client, workdirs, tmp_path):
    _root, _fallback = workdirs
    src = tmp_path / 'a.txt'
    src.write_text('x', encoding='utf-8')
    outside = tmp_path / 'outside'
    outside.mkdir()
    resp = client.post('/api/ai/case', json={
        'task': 't', 'files': [str(src)], 'mode': 'new', 'dest': str(outside)})
    assert resp.status_code == 403


def test_copy_file_direct(tmp_path):
    src = tmp_path / 'a.txt'
    src.write_text('hello', encoding='utf-8')
    target = tmp_path / 'a.txt.bak'
    assert aihandoff._copyFile(str(src), str(target)) is True
    assert target.read_text(encoding='utf-8') == 'hello'
    assert aihandoff._copyFile(str(src), str(target)) is False
