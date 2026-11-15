# -*- coding: utf-8 -*-
"""批次 L L3：重启打断（pausedBy='restart'）的任务启动 60s 后自动续接。

覆盖：只有启动恢复路径打标记（用户暂停/线程猝停/无会话中断都不打）、60s
内不续、每个任务只续一次、续接给主管留一条 notice、配置可关。续接本身
打桩（runner._resumeWithMessage 记录调用）——起不起真会话由 R4/R5 的
回归集覆盖，这里只验编排。

L 返工（主控追加·必须修）：重启落在「唤醒中」（resuming=True 落盘）时，
启动恢复必须清掉标记——否则任何 message 都被 409「会话正在唤醒，请稍候」
挡死（线上实测 5 个任务）；清掉之后 message 正常续接。

DR→L3 接缝（主控追件）：部署排空超时把仍在跑的任务标 pausedBy='restart'
（task_drain._markRestart），正是 L3 的消费方——重启后自动续接不能断。
"""
import json
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import events as eventsMod
from modules import leads_inbox
from modules import task_drain, task_health
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
TOKEN = 'tok-' + 'p' * 24
H = {'X-Agent-Token': TOKEN}


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
    import hashlib
    monkeypatch.setattr(configMod, 'getAgentTokens',
                        lambda: [{'id': 't1', 'name': '测试智能体',
                                  'tokenHash': hashlib.sha256(
                                      TOKEN.encode()).hexdigest()}])
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'root'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    monkeypatch.setattr(nodesMod, '_healthModels',
                        {'at': time.monotonic(), 'data': {}})
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    client = app.test_client()
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302
    return SimpleNamespace(client=client, fresh=app.test_client(), repo=repo,
                           tmp=tmp_path, monkeypatch=monkeypatch)


def _create(env):
    resp = env.client.post('/api/tasks', headers=H,
                           data=json.dumps({'prompt': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 202
    return resp.get_json()['task']['id']


def _restart(env):
    """同路径新 repo 模拟重启（首次访问触发 load：状态机 + pausedBy 标记）。"""
    repo2 = TaskRepository(str(env.tmp / 'tasks.json'), str(env.tmp / 'logs'))
    env.monkeypatch.setattr(store, 'repo', repo2)
    repo2.load()
    return repo2


def _makeLeadTask(env):
    """建一个带主管的下属任务（令牌绑主管 → task.lead 自动记）。

    注意：env 夹具把 getAgentTokens 打了桩（返回每次新建的字面量），往它的
    返回值里塞 lead 是无效的——这里重新打桩，把带上 lead 的条目给鉴权读。
    """
    import hashlib
    resp = env.client.post('/api/settings/leads',
                           data=json.dumps({'name': '甲'}),
                           content_type='application/json')
    assert resp.status_code == 201
    leadId = resp.get_json()['lead']['id']
    entry = {'id': 't1', 'name': '测试智能体',
             'tokenHash': hashlib.sha256(TOKEN.encode()).hexdigest(),
             'lead': leadId}
    env.monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [dict(entry)])
    configMod.setAgentTokens([dict(entry)])
    # 令牌通道必须走无 Cookie 的 fresh client（否则被网页会话态短路，
    # task.lead 记不上——指挥链的既有约定）
    resp = env.fresh.post('/api/tasks', headers=H,
                          data=json.dumps({'prompt': 'x'}),
                          content_type='application/json')
    assert resp.status_code == 202
    return leadId, resp.get_json()['task']['id']


def test_restart_marks_only_recovery_path(env):
    """启动恢复把在飞任务打 paused 时记 pausedBy='restart'；无会话的中断、
    已经 paused 的老任务都不记（用户主动暂停的不续，构造上保证）。"""
    running = _create(env)
    with env.repo.lock:
        task = env.repo.get(running)
        task['sessionId'] = 'session_' + 'a' * 36
        task['status'] = 'running'
        env.repo.save()
    noSession = _create(env)
    with env.repo.lock:
        env.repo.get(noSession)['status'] = 'running'
        env.repo.save()
    alreadyPaused = _create(env)
    with env.repo.lock:
        env.repo.get(alreadyPaused)['status'] = 'paused'
        env.repo.save()

    repo2 = _restart(env)
    assert repo2.get(running)['pausedBy'] == 'restart'
    assert repo2.get(running)['status'] == 'paused'
    assert 'pausedBy' not in repo2.get(noSession)      # interrupted：不能续
    assert 'pausedBy' not in repo2.get(alreadyPaused)  # 本来就被暂停：不续


def test_auto_resume_after_startup_delay_once(env):
    """启动 60s 后自动续一条（文案见模块常量），每个任务只续一次；
    给主管留一条 notice。"""
    leadId, tid = _makeLeadTask(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'session_' + 'b' * 36
        task['status'] = 'running'
        env.repo.save()
    repo2 = _restart(env)
    resumed = []
    env.monkeypatch.setattr(runner, '_resumeWithMessage',
                            lambda t, text: resumed.append((t['id'], text)))
    env.monkeypatch.setattr(task_health, '_startedAt', time.time() - 61)

    task_health.tick(time.time())                      # 过 60s：续
    assert resumed == [(tid, task_health.AUTO_RESUME_TEXT)]
    assert repo2.get(tid)['pausedBy'] == ''            # 只续一次：标记已清
    entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
    assert len(entries) == 1 and entries[0]['kind'] == 'notice'
    assert tid[:8] in entries[0]['text'] and '自动续接' in entries[0]['text']

    task_health.tick(time.time() + 10)                 # 再扫：不重复续
    assert len(resumed) == 1


def test_no_resume_within_startup_delay(env):
    """启动 60s 内不续（给手动续接留窗口）。"""
    _leadId, tid = _makeLeadTask(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'session_' + 'c' * 36
        task['status'] = 'running'
        env.repo.save()
    repo2 = _restart(env)
    resumed = []
    env.monkeypatch.setattr(runner, '_resumeWithMessage',
                            lambda t, text: resumed.append(t['id']))
    env.monkeypatch.setattr(task_health, '_startedAt', time.time() - 30)

    task_health.tick(time.time())
    assert resumed == [] and repo2.get(tid)['pausedBy'] == 'restart'


def test_user_paused_task_not_resumed(env):
    """没有 pausedBy 标记的 paused（用户主动暂停/线程猝停）不续。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'session_' + 'd' * 36
        task['status'] = 'paused'
        env.repo.save()
    resumed = []
    env.monkeypatch.setattr(runner, '_resumeWithMessage',
                            lambda t, text: resumed.append(t['id']))
    env.monkeypatch.setattr(task_health, '_startedAt', time.time() - 600)

    task_health.tick(time.time())
    assert resumed == []


def test_auto_resume_disabled_by_config(env):
    """autoResumeAfterRestart=false：不续（标记留着，用户自己续）。"""
    _leadId, tid = _makeLeadTask(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'session_' + 'e' * 36
        task['status'] = 'running'
        env.repo.save()
    repo2 = _restart(env)
    resumed = []
    env.monkeypatch.setattr(runner, '_resumeWithMessage',
                            lambda t, text: resumed.append(t['id']))
    env.monkeypatch.setattr(task_health, '_startedAt', time.time() - 600)
    env.monkeypatch.setattr(configMod, 'getAutoResumeAfterRestart',
                            lambda: False)

    task_health.tick(time.time())
    assert resumed == [] and repo2.get(tid)['pausedBy'] == 'restart'


def test_restart_clears_resuming_then_message_resumes(env):
    """L 返工（必须修）：重启落在「唤醒中」——resuming 落盘活过重启，
    之后任何 message 都被 409「会话正在唤醒」挡死（线上实测 5 个任务）。
    启动恢复清掉标记后，message 正常续接（202 + 真起 resume 会话线程）。
    """
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'session_' + 'f' * 36
        task['status'] = 'queued'          # _resumeWithMessage 把任务打 queued
        task['resuming'] = True            # 重启正好落在唤醒中：标记落盘
        env.repo.save()

    repo2 = _restart(env)
    assert repo2.get(tid)['resuming'] is False       # 启动恢复清掉唤醒标记
    assert repo2.get(tid)['pausedBy'] == 'restart'   # 照常走 L3 恢复路径

    started = []
    env.monkeypatch.setattr(
        runner, '_sessionThread',
        lambda task, manifest, resume=False, firstMessage=None, **kw:
        started.append((task['id'], resume, firstMessage)))
    resp = env.client.post('/api/tasks/%s/message' % tid, headers=H,
                           data=json.dumps({'text': '重启后续上'}),
                           content_type='application/json')
    assert resp.status_code == 202, resp.get_json()
    # _resumeWithMessage 起的是后台线程：202 先返回、线程可能还没跑——有上限
    # 轮询等它真起来再断言（并行负载下不等会偶发 []，主控授权修的同类问题）
    deadline = time.time() + 5
    while time.time() < deadline and not started:
        time.sleep(0.05)
    assert started == [(tid, True, '重启后续上')]


def test_drain_timeout_mark_is_resumed_by_l3(env):
    """DR→L3 接缝（主控追件）：部署排空超时把仍在跑的任务标
    pausedBy='restart'（task_drain._markRestart，DR 的原话就是「交批次 L3
    重启后续接」）——重启后 load 据此打 paused，L3 60s 后自动续接。排空
    超时不白标，L3 也不白接。"""
    tid = _create(env)
    with env.repo.lock:
        task = env.repo.get(tid)
        task['sessionId'] = 'session_' + 'd' * 36
        task['status'] = 'running'          # 在飞回合：排空等的就是它
        env.repo.save()
    env.repo.setAgent(tid, {})              # _runningRounds 判「有活客户端」

    result = task_drain.runDrain(1)         # 1s 超时：等不到回合结束 → 标记
    assert result.get('timedOut') is True
    assert env.repo.get(tid)['pausedBy'] == 'restart'
    assert any('pausedBy=restart' in (e.get('text') or '')
               for e in env.repo.get(tid)['events'])

    repo2 = _restart(env)                   # 重启：running → paused（标记已在）
    assert repo2.get(tid)['status'] == 'paused'
    assert repo2.get(tid)['pausedBy'] == 'restart'
    resumed = []
    env.monkeypatch.setattr(runner, '_resumeWithMessage',
                            lambda t, text: resumed.append(t['id']))
    env.monkeypatch.setattr(task_health, '_startedAt', time.time() - 61)

    task_health.tick(time.time())           # L3 接上 DR 的标记
    assert resumed == [tid]
