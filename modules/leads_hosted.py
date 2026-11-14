# -*- coding: utf-8 -*-
"""一键建托管主管（批次 A A1）：POST /api/settings/leads/hosted。

用户点一下就有一个托管主管在 Deskport 机器上跑起来。服务端按顺序做，任一步
失败都回滚已做的步骤、不留半成品（规格 A1）：

1. 建主管记录（kind='hosted'）；
2. 签发专属令牌（名字 主管·<name>，超长截断；上限 20，不占 agentTokens
   的 10 个手工名额——两套名额在 agent_tokens.tokenCounts 分开数）；
3. 建主管自己的任务（node 只支持 local；lead 必须为空——它是主管本人，
   不是下属；工作目录用 Deskport 分配的任务目录，cwd 只写进角色说明）；
4. 工作区里放好技能包：解出与 /api/agent/skill.zip 同内容的整包到
   <workspace>/deskport/，写 .deskport.json（url 指向本机 127.0.0.1:<port>，
   node=local），写 .agent-token（令牌明文只写这一个文件）；
5. 令牌登记进 sensitive_values 的脱敏来源（执行方打印它，写事件流前也被
   替换成 <redacted:…>）；
6. 首条消息 = 角色说明（docs/lead-role.md）+ duty；mode=default，policy
   允许工作区内读写与 python deskport/deskport.py 命令，其它走审批；
7. 绑成该主管的托管任务并启动会话（建卡与启动分两步：先落盘再起会话，
   失败回滚时会话从没起来过，不必杀进程）。

本模块还管托管主管专属令牌的吊销（规格 A1 末段：停用=吊销、删除=吊销、
暂停=不吊销——沿用 Q 的语义），供 leads_api 的 PATCH/DELETE 调。
"""
import datetime
import io
import json
import logging
import os
import secrets
import shutil
import zipfile

from flask import Blueprint, jsonify, request

import config
from modules import (agent_skill_files, agent_tokens, events, leads_store,
                     sensitive_values, tasks_api, tasks_store)
from modules.agent_api import tokenHash

log = logging.getLogger('leads_hosted')
blueprint = Blueprint('leads_hosted', __name__)

SKILL_NAME = 'deskport'
MAX_DUTY = 2000                 # duty 上限（规格：≤2000 字）
PROMPT_LIMIT = 4000             # 任务首条消息上限（createTaskResponse 同款）
TOKEN_PREFIX = '主管·'
# 主管任务的审批边界（A1-6）：python deskport/deskport.py 命令自动放行；路径
# 分隔符用 [\\/]——Windows 上主管可能打反斜杠。allowPaths 建卡时才知道工作区
# 路径，建卡后补（见 createHostedLead）；其余走审批（default=ask）。
# 第二条 ^cd\b（例 11 实测后加）：模型的自然形状是
# `cd "<工作区>" && python deskport/deskport.py …`——cd 段不匹配就整条挂起等
# 用户，无人干预链断在这里。cd 单独无害（每条命令都是新 shell，落不了盘），
# 后续段仍逐段过闸（`cd x && rm -rf y` 的 rm 段照样 ask）；重定向目标仍须落在
# allowPaths 内（task_policy 的 redirect 判定用 cd 目标做基准）
BASE_POLICY = {'allowCommands': [r'python3?\s+deskport[\\/]deskport\.py\b',
                                 r'^cd\b'],
               'default': 'ask'}
ROLE_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'docs', 'lead-role.md')


class _Abort(Exception):
    """带 HTTP 状态码的失败：回滚后原样返回给调用方。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 角色说明（A3）

def roleText(name, cwd=''):
    """渲染角色说明模板（docs/lead-role.md）：返回 (text, error)。

    模板里的 {{NAME}} / {{CWD_LINE}} / {{GRACE}} 由服务端注入——用户给的
    cwd 写进角色说明作为「你负责的项目目录」（规格 A1-3：不作为会话工作
    目录）。
    """
    try:
        with open(ROLE_DOC, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        return '', '角色说明模板缺失（%s）：%s' % (ROLE_DOC, e)
    cwdLine = ('你负责的项目目录：%s。注意：会话的工作目录不是它，而是你的任务'
               '工作区；项目里的文件按需读写，产出仍放任务工作区。' % cwd) \
        if cwd else '（本次未指定项目目录，你的工作区就是 Deskport 分配的任务目录。）'
    text = (text.replace('{{NAME}}', str(name or ''))
                .replace('{{CWD_LINE}}', cwdLine)
                .replace('{{GRACE}}', str(config.getLeadApprovalGraceSec())))
    return text.strip(), ''


# ---------------------------------------------------------------- 令牌（A1-2）

def issueToken(lead):
    """签发托管主管的专属令牌：返回 (plaintext, entry, error)。

    名字 主管·<name>（超长截断到 24，重名追加数字后缀）；绑定到该主管；
    明文只进返回值——调用方负责只写 <workspace>/.agent-token 一个文件，
    绝不进响应、事件流、日志。名额校验：托管名额 20 个（不占 10 个手工
    名额，agent_tokens.tokenCounts）。
    """
    tokens = config.getAgentTokens()
    _plainUsed, hostedUsed = agent_tokens.tokenCounts(tokens)
    if hostedUsed >= agent_tokens.MAX_HOSTED_TOKENS:
        return '', '', ('托管主管的专属令牌最多 %d 个（已 %d）——先停用或删除'
                        '不用的托管主管'
                        % (agent_tokens.MAX_HOSTED_TOKENS, hostedUsed))
    taken = {str(t.get('name') or '').strip() for t in tokens}
    base = (TOKEN_PREFIX + str(lead.get('name') or ''))[:agent_tokens.MAX_NAME]
    name, n = base, 2
    while name in taken:
        suffix = str(n)
        name = base[:agent_tokens.MAX_NAME - len(suffix)] + suffix
        n += 1
    plaintext = secrets.token_urlsafe(32)
    entry = {'id': secrets.token_hex(8), 'name': name,
             'tokenHash': tokenHash(plaintext),
             'createdAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             'lead': str(lead.get('id') or '')}
    tokens.append(entry)
    config.setAgentTokens(tokens)
    return plaintext, entry, ''


def revokeLeadTokens(lead):
    """吊销该主管绑定的全部令牌（规格 A1 末段：停用=吊销、删除=吊销）。

    返回吊销的令牌名（审计用）；暂停不调本函数（只拦新派活，Q 的语义）。
    """
    leadId = str(lead.get('id') or '')
    tokens = config.getAgentTokens()
    doomed = [t for t in tokens if str(t.get('lead') or '') == leadId]
    if not doomed:
        return []
    config.setAgentTokens([t for t in tokens
                           if str(t.get('lead') or '') != leadId])
    for entry in doomed:
        events.log('agent.token.revoked', str(entry.get('name') or ''), 'ok',
                   '随主管停用/删除吊销（%s）' % (lead.get('name') or leadId),
                   request.remote_addr)
    return [str(t.get('name') or '') for t in doomed]


# ---------------------------------------------------------------- 技能包落盘（A1-4）

def placeSkillPack(workspace, tokenPlain):
    """把技能包与令牌放进任务工作区；失败抛 _Abort（调用方回滚）。

    解出与 /api/agent/skill.zip 同内容的整包（zip 内条目都带 deskport/ 前缀，
    解到工作区根即得 <workspace>/deskport/），随后写 .deskport.json（url 指向
    本机 127.0.0.1:<port>，node=local）与 .agent-token（明文只此一个文件）。
    """
    manual = agent_skill_files.renderedSkillManual(SKILL_NAME)
    packed = agent_skill_files.buildSkillZip(SKILL_NAME, manual=manual)
    if packed is None:
        raise _Abort('技能包缺失：.agents/skills/%s/SKILL.md 不在' % SKILL_NAME,
                     500)
    blob, _count = packed
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(workspace)      # 包内路径全部以 deskport/ 开头
        cfg = {'url': 'http://127.0.0.1:%d' % config.getPort(),
               'node': 'local', 'host': 'local'}
        with open(os.path.join(workspace, SKILL_NAME, '.deskport.json'),
                  'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with open(os.path.join(workspace, SKILL_NAME, '.agent-token'),
                  'w', encoding='utf-8') as f:
            f.write(tokenPlain)
    except (OSError, zipfile.BadZipFile, KeyError) as e:
        raise _Abort('技能包解压/落盘失败：%s' % e, 500)


# ---------------------------------------------------------------- 回滚（A1）

def _dropTask(taskId, workspace):
    """把刚建、还没启动的任务从仓储摘掉并删工作区（回滚专用）。

    tasks_store 本批不许改（另一批在改），没有公开的删除入口；这里只动
    本模块自己刚刚 createTaskResponse(start=False) 建出来、尚未 startTask
    的任务——会话线程从没起来过，摘记录不会丢活进程。
    """
    with tasks_store.repo.lock:
        tasks_store.repo.tasks.pop(taskId, None)
    tasks_store.repo.save()
    try:
        os.unlink(os.path.join(tasks_store.logDir(), taskId + '.jsonl'))
    except OSError:
        pass
    if workspace:
        shutil.rmtree(workspace, ignore_errors=True)


def _rollback(leadId, tokenId, taskId, workspace):
    """撤掉已做的步骤：任务（含工作区）→ 令牌 → 主管记录。"""
    if taskId:
        _dropTask(taskId, workspace)
    if tokenId:
        tokens = config.getAgentTokens()
        config.setAgentTokens([t for t in tokens
                               if str(t.get('id') or '') != tokenId])
    if leadId:
        leads_store.remove(leadId)


# ---------------------------------------------------------------- 端点（A1）

def _clean(data):
    """入参校验（建任何东西之前）：返回 (fields, errorResponse)。"""
    name, err = leads_store.cleanName(data.get('name'))
    if err:
        return None, (jsonify({'error': err}), 400)
    project, err = leads_store.cleanProject(data.get('project'))
    if err:
        return None, (jsonify({'error': err}), 400)
    duty = str(data.get('duty') or '').strip()
    if not duty:
        return None, (jsonify({'error': 'duty 不能为空：写清这个主管负责什么'
                                          '（≤%d 字）' % MAX_DUTY}), 400)
    if len(duty) > MAX_DUTY:
        return None, (jsonify({'error': 'duty 过长（上限 %d 字）' % MAX_DUTY}),
                      400)
    node = str(data.get('node') or 'local').strip() or 'local'
    if node != 'local':
        return None, (jsonify({'error': '托管主管暂只支持本机（node=local）；'
                                          '其它执行点以后开'}), 400)
    model = str(data.get('model') or '').strip()
    if model and (len(model) > 100 or any(c < ' ' for c in model)):
        return None, (jsonify({'error': 'model 不合法（≤100 字符，无控制'
                                          '字符）'}), 400)
    cwd = str(data.get('cwd') or '').strip()
    if len(cwd) > 500:
        return None, (jsonify({'error': 'cwd 过长（上限 500 字符）'}), 400)
    return {'name': name, 'project': project, 'duty': duty, 'model': model,
            'cwd': cwd}, None


@blueprint.route('/api/settings/leads/hosted', methods=['POST'])
def createHostedLead():
    """一键建托管主管（批次 A A1）：201 {"lead": record, "taskId": "..."}。

    只认网页会话（同 /api/settings/leads*——智能体/设备令牌打这些管理接口
    一律 401，authGuard 自动做到）。任一步失败回滚已做的步骤，4xx/5xx 带
    原因，不留半成品。
    """
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    fields, err = _clean(data)
    if err:
        return err
    role, err = roleText(fields['name'], fields['cwd'])
    if err:
        return jsonify({'error': err}), 500
    prompt = role + '\n\n' + fields['duty']
    if len(prompt) > PROMPT_LIMIT:
        return jsonify({'error': '角色说明加 duty 共 %d 字符，超过任务描述'
                                 '上限 %d——duty 写精炼点'
                          % (len(prompt), PROMPT_LIMIT)}), 400
    lead, err = leads_store.create(fields['name'], fields['project'],
                                   kind='hosted')
    if err:
        return jsonify({'error': err}), 400
    leadId = str(lead.get('id') or '')
    tokenId = taskId = workspace = ''
    try:
        plaintext, entry, err = issueToken(lead)
        if err:
            raise _Abort(err, 400)
        tokenId = str(entry.get('id') or '')
        payload, code = tasks_api.createTaskResponse({
            'prompt': prompt, 'node': 'local', 'mode': 'default',
            'model': fields['model'] or None,
            'policy': BASE_POLICY}, start=False)
        if code >= 400:
            raise _Abort(str(payload.get('error') or '建任务失败'), code)
        taskId = str(payload['task']['id'])
        workspace = str(payload['task'].get('workspace') or '')
        task = tasks_store.repo.get(taskId)
        if task is None:
            raise _Abort('任务记录丢失: %s' % taskId, 500)
        # 工作区路径建卡时才知道：补进 allowPaths（A1-6）再起会话
        with tasks_store.repo.lock:
            task['policy']['allowPaths'] = [workspace]
            tasks_store.repo.save()
        placeSkillPack(workspace, plaintext)
        bound, err = leads_store.bindTask(leadId, taskId)
        if err:
            raise _Abort(str(err), 400)
        # 令牌明文已落盘：脱敏注册表按文件收集（sensitive_values），这里显式
        # 失效一次缓存，避免同秒落盘/采集的 mtime 精度竞态
        sensitive_values.invalidate()
        tasks_api.startTask(task, [])
    except _Abort as e:
        _rollback(leadId, tokenId, taskId, workspace)
        log.warning('hosted lead create rolled back (%s): %s', e.status, e)
        return jsonify({'error': str(e)}), e.status
    except Exception as e:      # 未预期失败同样回滚，不留半成品
        _rollback(leadId, tokenId, taskId, workspace)
        log.warning('hosted lead create failed: %s', e)
        return jsonify({'error': '建托管主管失败：%s' % e}), 500
    events.log('lead.hosted.created', fields['name'], 'ok',
               'task=%s token=%s model=%s'
               % (taskId, entry.get('name') or '', fields['model'] or '-'),
               request.remote_addr)
    record = leads_store.get(leadId) or bound
    return jsonify({'lead': record, 'taskId': taskId}), 201
