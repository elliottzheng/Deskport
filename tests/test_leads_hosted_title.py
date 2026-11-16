# -*- coding: utf-8 -*-
"""批次 T 返工：托管主管建出来的任务，标题为「主管 · <主管名>」（given）。

规格 §3 的验收遗漏项：modules/leads_hosted.py 的 createHostedLead 建卡时显式
给标题——角色说明首行推出的默认标题在指挥台上指代不清。夹具照
test_leads_hosted.py（全套桩化，会话不真起）。
"""
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import leads_hosted
from modules import tasks_api
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    audit = []
    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log',
                        lambda *a, **k: audit.append((a, k)))
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from modules import leads_inbox
    monkeypatch.setattr(leads_inbox, 'sweepOnce', lambda: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           repo=repo, tmp=tmp_path, app=app, audit=audit)


def hosted(env, name='验收主管', duty='把课题做出来'):
    assert env.client.post('/login',
                           data={'password': PASSWORD}).status_code == 302
    resp = env.client.post('/api/settings/leads/hosted',
                           data=json.dumps({'name': name, 'duty': duty}),
                           content_type='application/json')
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def test_hosted_lead_task_title_is_lead_named(env):
    """建托管主管 → 任务标题「主管 · <主管名>」，titleSource=given。"""
    out = hosted(env, '课题主管')
    task = env.repo.get(out['taskId'])
    assert task['title'] == '主管 · 课题主管'
    assert task['titleSource'] == 'given'


def test_hosted_lead_title_output_as_is(env):
    """given 标题原样输出：列表/详情都不按提示词重新推导。"""
    out = hosted(env, '甲主管')
    row = env.client.get('/api/tasks?full=1').get_json()['tasks'][0]
    assert row['title'] == '主管 · 甲主管'
    detail = env.client.get('/api/tasks/' + out['taskId']).get_json()['task']
    assert detail['title'] == '主管 · 甲主管'


def test_hosted_lead_title_max_name_length(env):
    """主管名取满 24 字：标题 5+24=29 字，仍在上限内、不被截断。"""
    name = '主' * 24
    out = hosted(env, name)
    task = env.repo.get(out['taskId'])
    assert task['title'] == '主管 · ' + name
    assert len(task['title']) == 29
