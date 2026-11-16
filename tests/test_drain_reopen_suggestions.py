# -*- coding: utf-8 -*-
"""批次 DR 建议修回归（SW 清扫，2026-11-16）：两项竞态/善后角落。

    DR-S1 排空闸门退出竞态：drain 结束与 close 撞毫秒窗口时，闸门 while
          退出后补查关闭标志，不再对已关闭任务开一轮；
    DR-S2 接回重试仍 in use：错误点名占用者（closed/cancelled 也算，
          节点侧进程可能没退），不再只抛节点原文。

test_drain_reopen.py 已顶 .py 400 行预算（存量不可再长），本批两项回归
单立此文件；fixture 只搭这两例需要的部分（不建 app、不打令牌）。
"""
import threading
import time as _time
from types import SimpleNamespace

import pytest

from modules import events as eventsMod
from modules import session_adopt, task_drain
from modules import tasks_store as store
from modules.tasks_store import TaskRepository


@pytest.fixture(autouse=True)
def cleanDrain():
    assert not task_drain.draining()
    yield
    assert not task_drain.draining()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **k: None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    import modules.agent_nodes as nodesMod
    return SimpleNamespace(repo=repo, nodes=nodesMod, tmp=tmp_path)


def _sid(n='a'):
    return 'session_' + n * 8 + '-' + n * 4 + '-' + n * 4 + '-' + n * 4 + '-' + n * 12


def test_dr_s1_gate_rechecks_close_after_drain_ends():
    """DR-S1：排空结束与 close 撞毫秒级窗口——闸门 while 退出后补查一次
    关闭标志再返回，已关闭任务不再开这一轮（旧实现循环外直接返回 False，
    终态会由 cancelled 被翻成 failed）。

    编排：闸门线程进 _cond.wait 后，先 close 任务、再按 runDrain finally
    的同一组写法结束排空并唤醒——补查只在循环退出后发生，旧实现必红。
    """
    task = {'id': 't-gate', 'status': 'waiting', 'closedByUser': False}
    got = {}
    with task_drain._cond:
        task_drain._state['active'] = True
        task_drain._cond.notify_all()

    def gate():
        got['value'] = task_drain.waitWhileDraining(task)
    th = threading.Thread(target=gate, daemon=True)
    th.start()
    _time.sleep(0.2)                     # 闸门已进 _cond.wait(0.5)
    task['closedByUser'] = True          # close 撞进来（drain 即将结束的同一窗口）
    with task_drain._cond:               # 排空结束：唤醒闸门
        task_drain._state['active'] = False
        task_drain._cond.notify_all()
    th.join(5)
    assert got['value'] is True          # 补查命中：不开这一轮


def test_dr_s2_retry_failure_names_holders_including_closed(env, monkeypatch):
    """DR-S2：清场后重试仍 in use 且无未关闭占用者时，错误点名占用者——
    已关闭/已取消的任务也算（节点侧进程可能没退），不再只抛节点原文；
    主路径（有未关闭占用者 → ensureSessionFree 直接抛）行为不变。"""
    monkeypatch.setattr(session_adopt, 'KILL_WAIT_SEC', 0)
    killed = []
    monkeypatch.setattr(env.nodes, 'shutdownNodeSession',
                        lambda node, sid: killed.append((node, sid)) or True)
    monkeypatch.setattr(env.nodes, 'findNode',
                        lambda name: {'name': name, 'url': 'http://stub'})
    monkeypatch.setattr(env.nodes, 'findFederationMember', lambda name: None)
    sid = _sid('d')
    with env.repo.lock:
        env.repo.put({'id': 'closed-holder', 'status': 'closed',
                      'title': '关掉的卡', 'sessionId': sid})
        env.repo.save()

    def fakeRequest(node, method, path, payload=None, timeout=30):
        if method == 'POST' and path == '/api/sessions':
            return 400, {'error': 'This session is in use by another '
                                  'Kimi Code CLI instance'}
        return 200, {}
    monkeypatch.setattr(env.nodes, 'nodeRequest', fakeRequest)

    client = env.nodes.RemoteAcpClient(cwd='/mnt/x', node='ai-xmyz',
                                       kimi_session_id=sid, fork=False)
    client._closed = True              # 别让事件流线程空转
    task = {'id': 't-adopt2', 'node': 'ai-xmyz', 'sessionId': '',
            'kimiSessionId': sid}
    with pytest.raises(RuntimeError) as excInfo:
        session_adopt.startSession(client, task)
    assert 'closed-holder' in str(excInfo.value)   # 点名占用者（含已关闭）
    assert killed == [('ai-xmyz', sid)]           # 无未关闭占用者：清场照做
