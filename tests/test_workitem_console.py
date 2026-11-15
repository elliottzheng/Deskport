# -*- coding: utf-8 -*-
"""批次 G2 指挥台：needs_judgment 的工作项进「等你处理」。

夹具同 test_command_view（statePath/仓储指 tmp、扫掠线程空操作）；只覆盖
G2 新增的那一类 pending 行——终态/排队中的工作项不进（排队只是在等别的
工作项，不需要人）。行 shape 与其它 pending 行一致，前端按 kind='workitem'
出决策按钮。
"""
import json
from types import SimpleNamespace

import pytest

import config as configMod
from modules import command_view, events as eventsMod, leads_inbox, leads_store
from modules import tasks_store as store
from modules import workitem_store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
NOW = 1_800_000_000.0


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
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **kw: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(workitem_store, 'repo',
                        workitem_store.WorkitemRepository(
                            str(tmp_path / 'workitems.json')))
    monkeypatch.setattr(leads_inbox, 'sweepOnce', lambda: None)
    monkeypatch.setattr(command_view, 'time',
                        SimpleNamespace(time=lambda: NOW))
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), tmp=tmp_path, app=app)


def putWorkItem(env, wid, state, reason='实施方有问题要拍板', lead='',
                attempt=1, maxAttempts=3):
    """直接落一张指定状态的工作项（pending 判定只看 state，不走迁移表）。"""
    wi = workitem_store.repo.create({
        'id': wid, 'projectId': 'p1', 'title': '工作项 %s' % wid, 'lead': lead,
        'specText': 'x', 'implModel': 'm1', 'verifyModel': 'm2',
        'footprint': ['*.py'], 'attempt': attempt, 'maxAttempts': maxAttempts,
        'judgmentReason': reason, 'updatedAt': NOW - 60})
    wi['state'] = state
    workitem_store.repo.save()
    return wi


def test_judgment_rows_enter_pending(env):
    assert env.client.post('/login',
                           data={'password': PASSWORD}).status_code == 302
    putWorkItem(env, 'wi-judg01', 'needs_judgment', reason='用哪个数据库？',
                lead='lead-1', attempt=2, maxAttempts=3)
    resp = env.client.get('/api/command/overview')
    assert resp.status_code == 200
    rows = [p for p in resp.get_json()['pending'] if p['kind'] == 'workitem']
    assert [r['workItemId'] for r in rows] == ['wi-judg01']
    assert rows[0]['detail'] == '用哪个数据库？'
    assert rows[0]['attempt'] == 2 and rows[0]['maxAttempts'] == 3
    assert rows[0]['title'] == '工作项 wi-judg01'
    assert rows[0]['leadId'] == 'lead-1'


def test_terminal_and_queued_not_in_pending(env):
    putWorkItem(env, 'wi-impl01', 'implementing')
    putWorkItem(env, 'wi-done01', 'integrated')
    putWorkItem(env, 'wi-queue0', 'queued')
    putWorkItem(env, 'wi-fail01', 'failed')
    data = command_view.overview()
    assert not [p for p in data['pending'] if p['kind'] == 'workitem']
