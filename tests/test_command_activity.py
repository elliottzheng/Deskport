# -*- coding: utf-8 -*-
"""批次 U2 追加：指挥者的直接活动——审计日志倒序读、按令牌名归因、60 分钟计数、
24 小时上线、挂到指挥树上。全部桩化：config.statePath 与 getAgentTokens 指到 tmp，
不碰真实状态目录。后半是本模块挂进 overview 的集成测试（走 HTTP）。
"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config as configMod
from modules import command_activity as act
from modules import command_view, events as eventsMod, leads_inbox
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
NOW = 1_800_000_000.0            # 固定"现在"


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
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [])
    monkeypatch.setattr(act, 'CACHE_SEC', 0)      # 每条用例都重扫，不读缓存残影
    monkeypatch.setattr(eventsMod, 'log', lambda *a, **kw: None)
    configMod._cache.update(mtime=None, data=None)
    repo = TaskRepository(str(tmp_path / 'tasks.json'), str(tmp_path / 'logs'))
    monkeypatch.setattr(store, 'repo', repo)
    monkeypatch.setattr(leads_inbox, 'sweepOnce', lambda: None)
    monkeypatch.setattr(command_view, 'time', SimpleNamespace(time=lambda: NOW))
    act.resetCache()
    from app import create_app
    app, _sock = create_app(pluginRoots=[], pluginNavDir=str(tmp_path / 'nav'))
    return SimpleNamespace(client=app.test_client(), repo=repo, tmp=tmp_path)


def writeAudit(env, entries):
    path = configMod.statePath('control_audit.jsonl')
    with open(path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    act.resetCache()
    return path


def entry(action, at, target='', detail='', name=None):
    """造一条审计条目；给了 name 就按 auditAgentAction 的形状加 '[name] ' 前缀。"""
    return {'at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(at)),
            'action': action, 'target': target, 'result': 'ok',
            'detail': ('[%s] %s' % (name, detail)) if name else detail,
            'ip': '127.0.0.1', 'on_behalf_of': ''}


def ago(sec):
    return NOW - sec


# ---------------------------------------------------------------- 倒序读

def test_tail_lines_reads_from_the_end(env):
    path = env.tmp / 'big.jsonl'
    path.write_text(''.join('{"i": %d}\n' % i for i in range(6000)),
                    encoding='utf-8')
    lines = act.tailLines(str(path), 5000)
    assert len(lines) == 5000
    assert json.loads(lines[0])['i'] == 1000        # 旧的被丢掉
    assert json.loads(lines[-1])['i'] == 5999       # 新的在末尾
    # 少于 limit：全给，顺序不变
    assert len(act.tailLines(str(path), 9999)) == 6000


def test_tail_lines_handles_chunk_boundary_and_no_trailing_newline(env, monkeypatch):
    monkeypatch.setattr(act, 'TAIL_CHUNK', 7)       # 逼出多块 + 截断行拼接
    path = env.tmp / 'chunky.jsonl'
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3}', encoding='utf-8')
    lines = act.tailLines(str(path), 10)
    assert [json.loads(x)['a' if 'a' in x else 'b' if 'b' in x else 'c']
            for x in lines] == [1, 2, 3]
    assert act.tailLines(str(env.tmp / 'nope.jsonl'), 10) == []
    empty = env.tmp / 'empty.jsonl'
    empty.write_text('', encoding='utf-8')
    assert act.tailLines(str(empty), 10) == []


# ---------------------------------------------------------------- 归因

def test_name_attribution_two_shapes(env):
    assert act._nameOf(entry('agent.exec', ago(10), name='拔尖')) == '拔尖'
    assert act._nameOf(entry('agent.sftp.put', ago(10), name='拔尖')) == '拔尖'
    # agent.task.*：target 就是令牌名，detail 里没有 [name] 前缀
    assert act._nameOf(entry('agent.task.create', ago(10),
                             target='kimi-code', detail='prompt…')) == 'kimi-code'
    # 归不上因的：面板、联邦来源、非智能体动作、会话类（审计不带令牌名）
    assert act._nameOf(entry('agent.task.rename', ago(10),
                             target='面板', detail='x')) == ''
    assert act._nameOf(entry('agent.exec', ago(10),
                             detail='联邦:来源 cmd')) == ''
    assert act._nameOf(entry('agent.session.open', ago(10),
                             target='h1', detail='~')) == ''
    assert act._nameOf(entry('files.delete', ago(10), name='拔尖')) == ''
    assert act._nameOf({'action': 'agent.exec', 'detail': '[] x'}) == ''


# ---------------------------------------------------------------- 计数与明细

def test_counts_window_last_and_recent(env):
    writeAudit(env, [
        entry('agent.exec', ago(70 * 60), name='拔尖', detail='很久以前的命令'),
        entry('agent.exec', ago(10 * 60), name='拔尖', detail='ls -la /tmp'),
        entry('agent.sftp.put', ago(5 * 60), name='拔尖', target='/tmp/a.txt'),
        entry('agent.sftp.get', ago(4 * 60), name='拔尖', target='/tmp/b.txt'),
        entry('agent.task.create', ago(3 * 60), target='拔尖', detail='派个活'),
        entry('agent.session.open', ago(2 * 60), target='h1', detail='~'),
        entry('agent.token.created', ago(60), target='新令牌'),
    ])
    slot = act.activityIndex(NOW)['拔尖']
    # 60 分钟窗口内：exec 1、sftp 2（其中写文件 1）、task 1；session 归不上因
    assert (slot['exec'], slot['sftp'], slot['writes'], slot['task'],
            slot['session']) == (1, 2, 1, 1, 0)
    assert slot['lastAt'] == ago(3 * 60)          # 最后一次直接操作（task.create）
    assert slot['lastExec'] == 'ls -la /tmp'      # 仍是最后一次 exec 的命令
    # 明细不受 60 分钟窗口限制：老的 exec 也在，按时间正序
    assert [r['action'] for r in slot['recent']] == [
        'agent.exec', 'agent.exec', 'agent.sftp.put', 'agent.sftp.get',
        'agent.task.create']
    # exec 摘要是命令、sftp 是路径、task 是 detail
    assert slot['recent'][0]['summary'] == '很久以前的命令'
    assert slot['recent'][1]['summary'] == 'ls -la /tmp'
    assert slot['recent'][2]['summary'] == '/tmp/a.txt'
    assert slot['recent'][3]['summary'] == '/tmp/b.txt'
    assert slot['recent'][4]['summary'] == '派个活'
    assert slot['recent'][1]['at'] == ago(10 * 60)


def test_last_exec_kept_and_truncated(env):
    writeAudit(env, [
        entry('agent.exec', ago(20 * 60), name='拔尖', detail='x' * 200),
        entry('agent.sftp.put', ago(10 * 60), name='拔尖', target='/tmp/a'),
    ])
    slot = act.activityIndex(NOW)['拔尖']
    assert len(slot['lastExec']) == 60             # 命令前 60 字符
    assert slot['lastExec'] == 'x' * 60
    # 只有 exec 之外的更新活动时，lastExec 仍是最后一次 exec
    assert slot['lastAt'] == ago(10 * 60)


def test_recent_keeps_only_last_ten(env):
    writeAudit(env, [entry('agent.exec', ago(i * 60), name='拔尖',
                           detail='cmd%d' % i) for i in range(20, 0, -1)])
    slot = act.activityIndex(NOW)['拔尖']
    assert len(slot['recent']) == 10
    assert slot['recent'][0]['summary'] == 'cmd10'
    assert slot['recent'][-1]['summary'] == 'cmd1'


def test_bad_lines_do_not_break_the_scan(env):
    path = configMod.statePath('control_audit.jsonl')
    os.path.dirname(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        '{"at": "2026-11-15 10:00:00", "action": "agent.exec",'
        ' "target": "h", "detail": "[拔尖] ls", "result": "ok"}\n'
        'not json at all\n[]\n', encoding='utf-8')
    act.resetCache()
    index = act.activityIndex(NOW)
    assert list(index) == ['拔尖']
    assert index['拔尖']['exec'] == 0               # 老时间戳不在 60 分钟窗口内


def test_cache_ten_seconds(env, monkeypatch):
    writeAudit(env, [entry('agent.exec', ago(60), name='拔尖', detail='ls')])
    monkeypatch.setattr(act, 'CACHE_SEC', 10)
    first = act.activityIndex(NOW)
    # 追加一条但不重置缓存：10 秒内不该重扫
    with open(configMod.statePath('control_audit.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry('agent.exec', ago(20), name='别人',
                                 detail='pwd'), ensure_ascii=False) + '\n')
    second = act.activityIndex(NOW)
    assert second is first and '别人' not in second
    act.resetCache()
    assert '别人' in act.activityIndex(NOW)


# ---------------------------------------------------------------- 上线成节点

def lead(id_, name='甲', **kw):
    out = {'id': id_, 'name': name, 'kind': 'external', 'parent': 'human',
           'status': 'active', 'taskId': ''}
    out.update(kw)
    return out


def test_node_index_maps_tokens_to_nodes(env, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 't1', 'name': '拔尖', 'lead': 'ld_a'},
        {'id': 't2', 'name': '闲置', 'lead': 'ld_gone'},   # 绑到不在册的主管
        {'id': 't3', 'name': 'kimi-code', 'lead': ''},
    ])
    writeAudit(env, [entry('agent.exec', ago(60), name='拔尖', detail='ls'),
                     entry('agent.exec', ago(60), name='闲置', detail='pwd'),
                     entry('agent.exec', ago(60), name='kimi-code', detail='df')])
    index = act.nodeIndex([lead('ld_a')], NOW)
    # 拔尖归主管节点；闲置归它绑定的那个 key（主管不在册时树上同 key 是 orphan
    # 组，照样挂得上）；kimi-code 没绑主管 → owner 节点
    assert set(index) == {'lead:ld_a', 'lead:ld_gone', 'owner:kimi-code'}
    assert index['lead:ld_a']['exec'] == 1
    assert index['lead:ld_gone']['exec'] == 1
    assert all(slot['fresh'] for slot in index.values())


def test_decorate_attaches_and_adds_nodes(env, monkeypatch):
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 't1', 'name': '拔尖', 'lead': 'ld_a'},
        {'id': 't4', 'name': '小兵', 'lead': 'ld_sub'},      # 子主管的令牌
        {'id': 't2', 'name': 'kimi-code', 'lead': ''},
        {'id': 't3', 'name': '沉睡', 'lead': ''},
    ])
    writeAudit(env, [entry('agent.exec', ago(60), name='拔尖', detail='ls'),
                     entry('agent.exec', ago(50), name='小兵', detail='pwd'),
                     entry('agent.sftp.put', ago(30), name='kimi-code',
                           target='/tmp/x'),
                     entry('agent.exec', ago(30 * 3600), name='沉睡',
                           detail='很久以前')])
    tree = [
        {'type': 'lead', 'key': 'lead:ld_a', 'id': 'ld_a', 'name': '甲',
         'children': [{'type': 'lead', 'key': 'lead:ld_sub', 'id': 'ld_sub',
                       'name': '子', 'children': []}]},
        {'type': 'owner', 'key': 'owner:kimi-code', 'id': 'kimi-code',
         'name': 'kimi-code', 'children': []},
        {'type': 'direct', 'key': 'direct', 'id': '', 'name': '你直接派的',
         'children': []},
    ]
    index = act.nodeIndex([lead('ld_a'), lead('ld_sub')], NOW)
    out = act.decorate(tree, index)
    # 子主管（第二层）也挂上；沉睡超 24 小时不上线
    assert out[0]['children'][0]['activity']['exec'] == 1
    assert out[0]['activity']['exec'] == 1
    assert out[1]['activity']['writes'] == 1
    assert [n['name'] for n in out] == ['甲', 'kimi-code', '你直接派的']
    # 只有直接操作、没有任务的令牌：24 小时内的补成一级节点，插在 direct 前
    tree2 = [{'type': 'direct', 'key': 'direct', 'id': '', 'name': '你直接派的',
              'children': []}]
    out2 = act.decorate(tree2, act.nodeIndex([lead('ld_a')], NOW))
    assert [n['type'] for n in out2] == ['owner', 'direct']
    assert out2[0]['name'] == 'kimi-code' and out2[0]['activity']['writes'] == 1
    # 超 24 小时的「沉睡」不补节点
    assert all(n['name'] != '沉睡' for n in out2)
    # 已经有任务的同名分组不重复上线
    tree3 = [{'type': 'owner', 'key': 'owner:kimi-code', 'id': 'kimi-code',
              'name': 'kimi-code', 'children': []}]
    out3 = act.decorate(tree3, act.nodeIndex([lead('ld_a')], NOW))
    assert len(out3) == 1


# ------------------------------------------------ 挂进 overview 的集成测试

def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def newLead(client, name='甲'):
    resp = client.post('/api/settings/leads', json={'name': name})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()['lead']['id']


def putTask(repo, tid, **kw):
    task = {'id': tid, 'title': tid, 'titleSource': 'given', 'prompt': 'p',
            'status': 'waiting', 'events': [], 'seq': 1, 'lead': '',
            'owner': '面板', 'node': 'local', 'mode': 'default',
            'resultFiles': [], 'workspace': 'ws', 'createdAt': NOW - 3600,
            'lastActiveAt': NOW - 60, 'endedAt': 0}
    task.update(kw)
    repo.put(task)
    return task


def flat(nodes, depth=0, out=None):
    out = [] if out is None else out
    for node in nodes or []:
        out.append((node, depth))
        flat(node.get('children') or [], depth + 1, out)
    return out


def tree(client):
    return client.get('/api/command/overview').get_json()['tree']


def test_activity_only_commander_becomes_top_level(env, monkeypatch):
    """不派任务、只用自己的令牌直接操作的指挥者也上一级节点（线上实测形态）。"""
    login(env.client)
    a = newLead(env.client, '甲')
    putTask(env.repo, 't1', lead=a)
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 't1', 'name': '甲令牌', 'lead': a},
        {'id': 't2', 'name': 'kimi-code', 'lead': ''},
    ])
    writeAudit(env, [
        entry('agent.exec', ago(60), '拔尖主机', name='甲令牌', detail='ls -la /tmp'),
        entry('agent.sftp.put', ago(30), '/tmp/a.txt', name='kimi-code'),
        entry('agent.exec', ago(30 * 3600), '老主机', name='睡令牌', detail='pwd'),
    ])
    rows = flat(tree(env.client))
    groups = [(n['type'], n['name']) for n, d in rows if d == 0]
    # 甲（有任务）+ kimi-code（只有直接操作）上位；超 24 小时的睡令牌不上来
    assert groups == [('lead', '甲'), ('owner', 'kimi-code')]
    jia = [n for n, d in rows if d == 0 and n['name'] == '甲'][0]
    kimi = [n for n, d in rows if d == 0 and n['name'] == 'kimi-code'][0]
    assert jia['activity']['exec'] == 1
    assert jia['activity']['lastExec'] == 'ls -la /tmp'
    assert kimi['activity']['writes'] == 1 and kimi['activity']['sftp'] == 1
    assert kimi['activity']['lastAt'] == ago(30)
    assert [r['action'] for r in kimi['activity']['recent']] == ['agent.sftp.put']
    assert kimi['children'] == []                 # 没有任务，子项为空
    # 旧字段不受影响：无主任务里没有 kimi-code（它本来就没派过任务）
    data = env.client.get('/api/command/overview').get_json()
    assert [t['id'] for t in data['unassigned']['tasks']] == []


def test_activity_attached_to_lead_and_sub_lead(env, monkeypatch):
    """主管与子主管节点都带 activity（按令牌绑定归因）。"""
    login(env.client)
    a = newLead(env.client, '甲')
    a1 = newLead(env.client, '甲一')
    path = configMod.statePath('leads.json')
    table = json.load(open(path, encoding='utf-8'))
    table[a1]['parent'] = a
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(table, f)
    putTask(env.repo, 't1', lead=a)
    putTask(env.repo, 't2', lead=a1)
    monkeypatch.setattr(configMod, 'getAgentTokens', lambda: [
        {'id': 't1', 'name': '甲令牌', 'lead': a},
        {'id': 't2', 'name': '甲一令牌', 'lead': a1},
    ])
    writeAudit(env, [entry('agent.exec', ago(45), 'h1', name='甲一令牌', detail='pwd')])
    byId = {n['id']: n for n, _d in flat(tree(env.client))}
    assert 'activity' not in byId[a]              # 甲自己的令牌这一小时没动
    assert byId[a1]['activity']['exec'] == 1
    assert byId[a1]['activity']['lastExec'] == 'pwd'
    assert 'activity' not in byId['t1']           # activity 不逐级汇总，任务节点不带


def test_no_audit_log_means_no_activity(env):
    """没有审计日志（全新实例）时树照常，节点不带 activity 键。"""
    login(env.client)
    a = newLead(env.client, '甲')
    putTask(env.repo, 't1', lead=a)
    assert 'activity' not in tree(env.client)[0]
    assert [n['name'] for n, d in flat(tree(env.client)) if d == 0] == ['甲']
