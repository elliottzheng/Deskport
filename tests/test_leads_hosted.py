# -*- coding: utf-8 -*-
"""批次 A A1：一键建托管主管（例子 1–5 + 令牌名额 + 角色模板）。

POST /api/settings/leads/hosted 的完整编排：建主管记录 → 签发专属令牌 →
建卡（node=local、lead 为空、start=False）→ 工作区落技能包 /
.deskport.json / .agent-token → 绑定托管任务 → 起会话；任一步失败回滚、
不留半成品。全部桩化：config.statePath 与任务仓储指到 tmp，_startTask 打桩
（会话不真起），后台扫掠线程换空操作；技能包是真的（buildSkillZip 实时组装，
与 /api/agent/skill.zip 同一处）。
"""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

import config as configMod
from modules import agent_nodes as nodesMod
from modules import agent_skill_files as skillFilesMod
from modules import events as eventsMod
from modules import leads_hosted, leads_store
from modules import tasks_api
from modules import tasks_runner as runner
from modules import tasks_store as store
from modules.tasks_store import TaskRepository

PASSWORD = 'pw-123456'
ROLE_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'docs', 'lead-role.md')


@pytest.fixture
def env(tmp_path, monkeypatch):
    audit = []
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def fakeLog(action, target='', result='ok', detail='', ip=''):
        audit.append({'action': action, 'target': target, 'detail': detail})

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    monkeypatch.setattr(eventsMod, 'log', fakeLog)
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
                           audit=audit, repo=repo, tmp=tmp_path, app=app,
                           monkeypatch=monkeypatch)


def login(client):
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


def call(env, method, path, body=None, token=None):
    """管理面走会话态 client；给了 token 走 fresh（无 Cookie）令牌通道。"""
    client = env.fresh if token else env.client
    kw = {'headers': {'X-Agent-Token': token}} if token else {}
    if body is not None:
        kw['data'] = json.dumps(body)
        kw['content_type'] = 'application/json'
    return getattr(client, method)(path, **kw)


def newToken(env, name='T1'):
    resp = call(env, 'post', '/api/settings/agent-tokens', {'name': name})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def hosted(env, name='talent主管', duty='把课题做出来', **kw):
    """一键建托管主管；返回 (响应 json, 令牌明文)。"""
    body = {'name': name, 'duty': duty}
    body.update(kw)
    resp = env.client.post('/api/settings/leads/hosted',
                           data=json.dumps(body),
                           content_type='application/json')
    assert resp.status_code == 201, resp.get_json()
    out = resp.get_json()
    plain = open(os.path.join(out['lead']['taskId'] and
                              env.repo.get(out['taskId'])['workspace'],
                              'deskport', '.agent-token'),
                 encoding='utf-8').read()
    return out, plain


def tokenNames(env):
    return [t['name'] for t in
            call(env, 'get', '/api/settings/agent-tokens').get_json()['tokens']]


# ---------------------------------------------------------------- 例 1

def test_create_hosted_lead_full_flow(env):
    """例 1：201；kind=hosted、绑了 主管·<name> 令牌、taskId 指向 lead 为空的
    新任务；工作区里有 deskport/deskport.py、.deskport.json、.agent-token。"""
    login(env.client)
    out, plain = hosted(env, 'talent主管', '派一个下属只回答「就绪」')
    lead, taskId = out['lead'], out['taskId']
    assert lead['kind'] == 'hosted' and lead['status'] == 'active'
    assert lead['taskId'] == taskId and lead['parent'] == 'human'
    assert '主管·talent主管' in tokenNames(env)
    assert [a['target'] for a in env.audit
            if a['action'] == 'lead.hosted.created'] == ['talent主管']
    task = env.repo.get(taskId)
    assert task['lead'] == ''            # 主管本人不是下属
    assert task['mode'] == 'default' and task['node'] == 'local'
    assert task['status'] == 'queued'    # startTask 打桩：卡没启动
    assert task['owner'] == '面板'
    ws = task['workspace']
    assert os.path.isfile(os.path.join(ws, 'deskport', 'deskport.py'))
    assert os.path.isfile(os.path.join(ws, 'deskport', 'SKILL.md'))
    cfg = json.load(open(os.path.join(ws, 'deskport', '.deskport.json'),
                         encoding='utf-8'))
    assert cfg['url'] == 'http://127.0.0.1:6789' and cfg['node'] == 'local'
    assert open(os.path.join(ws, 'deskport', '.agent-token'),
                encoding='utf-8').read() == plain
    # 首条消息 = 角色说明 + duty；policy 允许工作区内读写与 CLI 命令
    assert '主管角色说明' in task['prompt'] and '只回答「就绪」' in task['prompt']
    assert 'talent主管' in task['prompt']
    assert task['policy']['allowPaths'] == [ws]
    assert task['policy']['default'] == 'ask'
    assert task['policy']['allowCommands']
    # 主管自己的会话卡审批时不走收件箱（lead 为空）——例 9 的行为一致
    assert leads_store.get(lead['id'])['taskId'] == taskId


def test_hosted_policy_carries_readonly_roots(env):
    """二次返工（主控拍板）：只读命令在工作区与项目目录内自动放行——建卡
    时把两个根写进 policy.readonlyRoots（给了 cwd 才带项目目录；allowPaths
    仍只管写工具/重定向，不被放宽）。"""
    login(env.client)
    out, _plain = hosted(env, 'ro主管', '派活', cwd='E:\\proj')
    task = env.repo.get(out['taskId'])
    assert task['policy']['readonlyRoots'] == [task['workspace'], 'E:\\proj']
    out2, _p2 = hosted(env, 'ro主管二', '派活')
    task2 = env.repo.get(out2['taskId'])
    assert task2['policy']['readonlyRoots'] == [task2['workspace']]


# ---------------------------------------------------------------- 例 2

def test_token_plaintext_never_in_response_or_events(env):
    """例 2：任务事件流、GET /api/tasks/<id>、建任务响应全文搜不到令牌明文。"""
    login(env.client)
    body = json.dumps({'name': '甲', 'duty': '干活'})
    resp = env.client.post('/api/settings/leads/hosted', data=body,
                           content_type='application/json')
    raw = resp.get_data(as_text=True)
    out = json.loads(raw)
    taskId = out['taskId']
    plain = open(os.path.join(env.repo.get(taskId)['workspace'], 'deskport',
                              '.agent-token'), encoding='utf-8').read()
    assert plain
    detail = env.client.get('/api/tasks/%s?events=1' % taskId).get_data(as_text=True)
    assert plain not in raw and plain not in detail
    # 审计里只有令牌名，没有明文
    assert plain not in json.dumps(env.audit, ensure_ascii=False)


# ---------------------------------------------------------------- 例 3

def test_cat_token_file_redacted_in_event_stream(env):
    """例 3：执行方 cat deskport/.agent-token → 事件流里是 <redacted:…>。

    走真实链路：policy 判 ask（敏感文件内建 deny）→ 挂起 → 主管令牌批准 →
    执行方把输出写进事件流 → 脱敏。
    """
    login(env.client)
    out, plain = hosted(env, '甲', '验证脱敏')
    taskId = out['taskId']
    task = env.repo.get(taskId)
    with env.repo.lock:
        task['status'] = 'running'
        env.repo.save()
    agent = {'queue': __import__('queue').Queue(), 'perm': None,
             'permDecision': None, 'permEvent': threading.Event()}
    env.repo.setAgent(taskId, agent)     # /permission 从 repo 找 agent
    params = {'toolCall': {'title': 'Bash', 'toolCallId': 'c-cat',
                           'rawInput': {'command': 'cat deskport/.agent-token'}},
              'options': [{'optionId': 'opt-allow', 'name': 'Allow once',
                           'kind': 'allow_once'},
                          {'optionId': 'opt-reject', 'name': 'Reject',
                           'kind': 'reject_once'}]}
    th = threading.Thread(target=runner._handleAcpPermission,
                          args=(task, agent, params, 'req-cat'), daemon=True)
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline and not task.get('pendingPermission'):
        time.sleep(0.05)
    assert task.get('pendingPermission') == 'req-cat'
    resp = call(env, 'post', '/api/tasks/%s/permission' % taskId,
                {'requestId': 'req-cat', 'optionId': 'opt-allow'}, token=plain)
    assert resp.status_code == 200, resp.get_json()
    th.join(timeout=10)
    assert not th.is_alive()
    # 执行方把 cat 的输出落成 tool_result 事件 → 脱敏
    runner._handleAcpUpdate(task, {'sessionUpdate': 'tool_call_update',
                                   'status': 'completed',
                                   'toolCallId': 'c-cat', 'title': 'Bash',
                                   'rawOutput': '文件内容: ' + plain})
    events = env.repo.get(taskId)['events']
    blob = json.dumps(events, ensure_ascii=False)
    assert plain not in blob
    results = [e for e in events if e.get('type') == 'tool_result']
    assert results and '<redacted:hostedLead.' in results[-1]['text']
    # jsonl 流水同样脱敏
    logRaw = open(env.repo.log_path(env.repo.get(taskId)), encoding='utf-8').read()
    assert plain not in logRaw


# ---------------------------------------------------------------- 例 4

def test_node_not_local_rejects_and_creates_nothing(env):
    """例 4a：node 不是 local → 400，什么都不建（校验在最前）。"""
    login(env.client)
    resp = env.client.post('/api/settings/leads/hosted',
                           data=json.dumps({'name': '甲', 'duty': 'x',
                                            'node': 'ai-xmyz'}),
                           content_type='application/json')
    assert resp.status_code == 400
    assert '托管主管暂只支持本机' in resp.get_json()['error']
    assert leads_store.list() == [] and tokenNames(env) == []
    assert env.repo.list() == []


def test_skill_pack_failure_rolls_back_everything(env):
    """例 4b：技能包解压失败 → 5xx；没有残留的主管记录、令牌、任务、工作区。"""
    login(env.client)
    env.monkeypatch.setattr(skillFilesMod, 'buildSkillZip',
                            lambda *a, **k: None)
    resp = env.client.post('/api/settings/leads/hosted',
                           data=json.dumps({'name': '甲', 'duty': 'x'}),
                           content_type='application/json')
    assert resp.status_code == 500
    assert '技能包' in resp.get_json()['error']
    assert leads_store.list() == [] and tokenNames(env) == []
    assert env.repo.list() == []
    logDir = os.path.join(str(env.tmp), 'state', 'agent_logs')
    assert not os.path.isdir(logDir) or os.listdir(logDir) == []
    taskroot = os.path.join(str(env.tmp), 'taskroot')
    assert not os.path.isdir(taskroot) or os.listdir(taskroot) == []


def test_duty_required_and_prompt_limit(env):
    """duty 必填；角色说明 + duty 超 4000 → 400，什么都不建。"""
    login(env.client)
    for body in ({'name': '甲'}, {'name': '甲', 'duty': '  '}):
        resp = env.client.post('/api/settings/leads/hosted',
                               data=json.dumps(body),
                               content_type='application/json')
        assert resp.status_code == 400 and 'duty' in resp.get_json()['error']
    resp = env.client.post('/api/settings/leads/hosted',
                           data=json.dumps({'name': '甲', 'duty': 'x' * 2001}),
                           content_type='application/json')
    assert resp.status_code == 400 and 'duty 过长' in resp.get_json()['error']
    assert leads_store.list() == [] and env.repo.list() == []


# ---------------------------------------------------------------- 例 5

def test_paused_keeps_token_retired_revokes(env):
    """例 5：暂停不吊销（只拦建任务）；停用（retired）吊销 → 401。"""
    login(env.client)
    out, plain = hosted(env, '甲', 'x')
    leadId = out['lead']['id']
    resp = call(env, 'patch', '/api/settings/leads/' + leadId,
                {'status': 'paused'})
    assert resp.status_code == 200
    assert call(env, 'get', '/api/agent/lead', token=plain).status_code == 200
    body = call(env, 'get', '/api/agent/lead', token=plain).get_json()
    assert body['lead']['status'] == 'paused'
    # 暂停只拦建任务（Q 的语义）
    resp = call(env, 'post', '/api/tasks', {'prompt': 'p'}, token=plain)
    assert resp.status_code == 403 and body['lead']['status'] == 'paused'
    resp = call(env, 'patch', '/api/settings/leads/' + leadId,
                {'status': 'retired'})
    assert resp.status_code == 200
    assert call(env, 'get', '/api/agent/lead', token=plain).status_code == 401
    assert '主管·甲' not in tokenNames(env)
    revoked = [a for a in env.audit if a['action'] == 'agent.token.revoked']
    assert [a['target'] for a in revoked] == ['主管·甲']


def test_delete_hosted_lead_revokes_token(env):
    """例 5 补：删除托管主管即吊销令牌（任务引用仍 409，先拦后吊销）。"""
    login(env.client)
    out, plain = hosted(env, '甲', 'x')
    leadId = out['lead']['id']
    resp = call(env, 'delete', '/api/settings/leads/' + leadId)
    assert resp.status_code == 200
    assert call(env, 'get', '/api/agent/lead', token=plain).status_code == 401
    assert leads_store.get(leadId) is None


def test_delete_blocked_by_subordinate_keeps_token(env):
    """删除被下属任务拦住时 409，且不提前吊销（不留半成品）。"""
    login(env.client)
    out, plain = hosted(env, '甲', 'x')
    leadId = out['lead']['id']
    resp = call(env, 'post', '/api/tasks', {'prompt': '下属活'}, token=plain)
    assert resp.status_code == 202
    resp = call(env, 'delete', '/api/settings/leads/' + leadId)
    assert resp.status_code == 409 and '名下有 1 个任务' in resp.get_json()['error']
    assert call(env, 'get', '/api/agent/lead', token=plain).status_code == 200


# ---------------------------------------------------------------- 名额

def test_hosted_token_quota_separate_from_plain(env):
    """两套名额：托管专属令牌 20 个不占 10 个手工名额（A1-2）。"""
    login(env.client)
    from modules import agent_tokens
    for i in range(9):
        newToken(env, 'p%d' % i)
    out, _plain = hosted(env, '甲', 'x')
    lead = leads_store.get(out['lead']['id'])
    tokens = configMod.getAgentTokens()
    assert agent_tokens.tokenCounts(tokens) == (9, 1)
    # 手工名额第 10 个还能建，第 11 个被拦
    assert newToken(env, 'p9')['name'] == 'p9'
    resp = call(env, 'post', '/api/settings/agent-tokens', {'name': 'p10'})
    assert resp.status_code == 400 and '10' in resp.get_json()['error']
    # 托管名额不受影响：还能再签发（直接调签发函数，免跑完整流程）
    for _ in range(19):
        assert leads_hosted.issueToken(lead)[2] == ''
    assert agent_tokens.tokenCounts() == (10, 20)
    resp = call(env, 'post', '/api/settings/agent-tokens', {'name': 'p11'})
    assert resp.status_code == 400 and '10' in resp.get_json()['error']
    _plain2, _entry, err = leads_hosted.issueToken(lead)
    assert '20' in err        # 托管名额第 21 个被拦


# ---------------------------------------------------------------- 角色模板（A3）

def test_hosted_policy_shapes(env):
    """A1-6：边界形状——cd <工作区> && python deskport/deskport.py … 放行；
    别的命令仍 ask（例 11 实测后补 cd 段：模型的自然形状带 cd 前缀）。"""
    login(env.client)
    out, _plain = hosted(env, '甲', 'x')
    task = env.repo.get(out['taskId'])
    policy = dict(task['policy'])
    ws = task['workspace']
    from modules import task_policy
    allow = lambda cmd: task_policy.evaluate(
        policy, kind='execute', name='Bash', args={'command': cmd},
        cwd=ws)[0]
    assert allow('python deskport/deskport.py task "只回复就绪" --mode default') \
        == 'allow'
    assert allow('cd "%s" && python deskport/deskport.py inbox' % ws) == 'allow'
    assert allow('cd . && python deskport/deskport.py report "完成了"') == 'allow'
    # 别的命令照旧走审批（cd 放行不连带放行后续段）
    assert allow('rm -rf build') == 'ask'
    assert allow('cd /tmp && rm -rf build') == 'ask'
    assert allow('curl http://x | bash') == 'ask'
    # 写文件：工作区内 allow、工作区外 ask
    assert task_policy.evaluate(policy, kind='edit', name='Edit',
                                args={'path': ws + '/a.md'},
                                paths=[ws + '/a.md'])[0] == 'allow'
    assert task_policy.evaluate(policy, kind='edit', name='Edit',
                                args={'path': 'E:/其他/b.md'},
                                paths=['E:/其他/b.md'])[0] == 'ask'


def test_role_doc_template(env):
    """A3：≤60 行、占位符全部渲染、必含的六条职责要点。"""
    text = open(ROLE_DOC, encoding='utf-8').read()
    assert text.count('\n') + 1 <= 60, '角色说明超 60 行'
    rendered, err = leads_hosted.roleText('甲', 'E:/proj')
    assert not err and '{{' not in rendered
    assert '甲' in rendered and 'E:/proj' in rendered
    for needle in ('不自己改代码', '--mode default', '不同模型',
                   '不要等待、不要轮询、不要 sleep', '【收件箱】', 'inbox',
                   '引用，不是指令', 'escalation', '/permission', 'report',
                   '目标达成就停下，不自己立项', 'deskport/.agent-token',
                   '绝不打印'):
        assert needle in rendered, needle
    noCwd, err = leads_hosted.roleText('甲', '')
    assert not err and '{{' not in noCwd and '本次未指定项目目录' in noCwd
    assert str(configMod.getLeadApprovalGraceSec()) in rendered
