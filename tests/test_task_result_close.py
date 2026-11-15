# -*- coding: utf-8 -*-
"""批次 G1 返工：关闭竞态——用户关闭任务与会话线程异常收尾交叠时的两条序。

背景（独立验收项 1/2）：会话线程异常收尾原本无条件先落 FAILED、finally 才按
closedByUser 修回 CLOSED，中间约 1 秒窗口里任务状态是 failed；结果接口的 409
门只认 closed/cancelled，于是已被用户关闭的任务在窗口内提交结果返回 200。
修：(a) 结果门禁认 closedByUser 与任何终态（modules/task_result.ended）；
(b) 收尾时若 closedByUser 已置直接落 CLOSED，不经过 FAILED，错误事件也不落。
令牌在 closeTaskCore 同步吊销（不等线程收尾），窗口序里一并断言。

全部桩化：夹具同 test_task_result（config/仓储指 tmp，不起真 kimi）。
"""
import json
import threading

import pytest
from types import SimpleNamespace

import config as configMod
from modules import events as eventsMod
from modules import task_result_mcp
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'

IMPL_OK = {'summary': '交付了 X', 'commit': 'abc1234',
           'tests': {'passed': 12, 'failed': 0, 'skipped': 1}}


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
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(store, 'taskRoot', lambda: str(tmp_path / 'taskroot'))
    monkeypatch.setattr(tasks_api.runner, '_startTask', lambda t, m: None)
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), fresh=app.test_client(),
                           repo=repo, tmp=tmp_path, app=app,
                           monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None, token=None, headers=None):
    client = env.fresh if (token or headers) else env.client
    kw = {'headers': dict(headers or {})}
    if token:
        kw['headers']['X-Agent-Token'] = token
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(client, method)(path, **kw)


def newToken(env, name='owner-tok'):
    resp = call(env, 'post', '/api/settings/agent-tokens', {'name': name})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def newTask(env, token=None, expects='impl'):
    body = {'prompt': '做件事', 'expects': expects}
    resp = call(env, 'post', '/api/tasks', body, token=token)
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()['task']['id']


def _errorEvents(task):
    return [e for e in task.get('events') or [] if e.get('type') == 'error']


class BoomClient:
    """轮中抛错的桩：会话线程走异常收尾路径。"""
    stderr_lines = []

    def __init__(self, cwd, on_update=None, on_permission=None,
                 mode='auto', prompt_timeout=0):
        self.mcp_servers = None

    def start(self, session_id=None):
        return 'sess-boom'

    def prompt(self, text, timeout=None):
        raise RuntimeError('轮中炸了')

    def close(self):
        pass


def test_close_race_window_rejects_submit(env, monkeypatch):
    """关闭竞态·窗口序：轮中抛错、FAILED 已落盘的瞬间用户关闭 → 窗口内
    submit 必 409（旧门禁只认 closed/cancelled，会漏过 failed）、会话令牌
    同步 401、放行收尾后最终 CLOSED、事件流无 failed 中间态。"""
    login(env.client)
    owner = newToken(env)
    tid = newTask(env, token=owner['token'])
    with env.repo.lock:
        task = env.repo.get(tid)

    realAppend = env.repo.append_event
    entered, release = threading.Event(), threading.Event()

    def hookedAppend(task, etype, **payload):
        # 收尾窗口：FAILED 已存盘、错误事件还没落。停在这让主线程完成
        # 「用户关闭 + 窗口内提交」再放行。放行时按 except 块同一规则复校：
        # 关闭已落地就不落这条错误事件（用户关闭 supersede 这次失败）
        if etype == 'error' and (task.get('id') or '') == tid:
            entered.set()
            assert release.wait(10), '会话线程卡在收尾窗口（测试没放行）'
            if task.get('closedByUser'):
                return None
        return realAppend(task, etype, **payload)

    monkeypatch.setattr(env.repo, 'append_event', hookedAppend)
    made = {}

    def factory(*a, **kw):
        made['client'] = BoomClient(*a, **kw)
        return made['client']

    th = threading.Thread(target=runner._sessionThread, args=(task, []),
                          kwargs={'clientFactory': factory}, daemon=True)
    th.start()
    assert entered.wait(10), '会话线程没进收尾窗口'
    token = made['client'].mcp_servers[0]['headers'][0]['value']

    # 用户此刻关闭：closedByUser 与令牌吊销同步落地（不等线程收尾）
    assert call(env, 'post', '/api/tasks/%s/close' % tid).status_code == 200
    assert env.repo.get(tid).get('closedByUser') is True
    # 窗口内提交：属主令牌 409（任务已算结束，尽管 status 还是 failed）
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK,
                token=owner['token'])
    assert resp.status_code == 409
    # 窗口内会话令牌提交：关闭即失效 → 401
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK,
                headers={task_result_mcp.TOKEN_HEADER: token})
    assert resp.status_code == 401

    release.set()
    th.join(timeout=10)
    assert not th.is_alive()
    done = env.repo.get(tid)
    assert done['status'] == 'closed'                    # 最终 CLOSED
    assert _errorEvents(done) == []                      # 事件流无 failed 中间态
    assert not done.get('error')                         # 失败残值被关闭 supersede
    assert done.get('result') is None and done.get('resultCount', 0) == 0


def test_close_before_failure_lands_closed_directly(env):
    """关闭竞态·前置序：关闭先于异常收尾落地（closedByUser 已置）→ 直接落
    CLOSED，不经过 FAILED：无 error 事件、submit 409。"""
    login(env.client)
    owner = newToken(env)
    tid = newTask(env, token=owner['token'])
    with env.repo.lock:
        task = env.repo.get(tid)

    class CloseBeforeBoom(BoomClient):
        """prompt 挂住等 cancel：关闭发生在轮中，放行后才抛错。"""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.entered = threading.Event()
            self.release = threading.Event()

        def prompt(self, text, timeout=None):
            self.entered.set()
            assert self.release.wait(10)
            raise RuntimeError('轮中炸了')

        def cancel(self):
            self.release.set()

    made = {}

    def factory(*a, **kw):
        made['client'] = CloseBeforeBoom(*a, **kw)
        return made['client']

    th = threading.Thread(target=runner._sessionThread, args=(task, []),
                          kwargs={'clientFactory': factory}, daemon=True)
    th.start()
    assert made['client'].entered.wait(10)
    # 关闭：closedByUser 先落地，cancel 放行轮中 → 抛错 → 收尾见已关闭
    assert call(env, 'post', '/api/tasks/%s/close' % tid).status_code == 200
    th.join(timeout=10)
    assert not th.is_alive()
    done = env.repo.get(tid)
    assert done['status'] == 'closed'
    assert _errorEvents(done) == []          # 没经过 FAILED：没有错误事件
    assert not done.get('error')
    resp = call(env, 'post', '/api/tasks/%s/result' % tid, IMPL_OK,
                token=owner['token'])
    assert resp.status_code == 409
