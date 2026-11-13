# -*- coding: utf-8 -*-
"""手机转发文件 → 交给执行方 — /api/agent/share（scope=share）。

手机令牌只有 upload 时只能往收件箱丢文件；要建任务就得 full，等于把
exec / SFTP / 改配置的全权交给手机。本模块新增 share scope，只放行
「投文件 + 建任务 / 往已有任务追一条消息」这一件事。

端点（挂在 agent_api 的蓝图上，app.py import 即随 agentApiModule 注册）:
    GET  /api/agent/share/nodes   → {nodes:[...], default}
    GET  /api/agent/share/tasks   → {tasks:[...]} 最近 20 条非 closed
    POST /api/agent/share         multipart（file 0..10、prompt、taskId、
        node、cwd、model、mode、organize、folderName）
      → 200 {taskId, mode:"created"|"appended", paths:[...], prompt}

行为:
    - 文件先落任务暂存区（modules/staging.saveStagedStream），响应/消息里给
      绝对路径；organize=folder 时 paths 给新建文件夹，prompt 末尾补目录说明；
      重名自动 (2)，绝不覆盖；
    - taskId 给了 → tasks_api.deliverMessage（回合中排队）；没给 →
      tasks_api.createTaskResponse 建任务；
    - 令牌缺 share → 403 {"need":"share"}；远端执行点还要 upload_remote；
    - 审计 agent.share（target=taskId, detail=文件数+prompt 长度）。

字段规则、三个去处与列表都下沉到 modules/share_core.py（批次 D）：网页
`/api/share/submit`（Cookie 会话）调同一份函数，不复制第二份逻辑。

执行点边界（2026-11-13 批次 B）:
    远端执行点（agent node / 联邦成员）的附件由 task_inputs 投到该机器的
    `<cwd>/.deskport-inputs/<taskId>/`，暂存区只作中转，投递成功即删；响应
    paths 换成远端绝对路径。投递失败 → 任务不建/消息不投，不留远端残留。
    远端需要 cwd；node 归一化交给 task_node。
"""
import os

from flask import jsonify, request

from modules import share_core, staging, task_node, tasks_store
from modules.agent_api import (SCOPE_SHARE, SCOPE_UPLOAD_REMOTE,
                               requireAgentScope, agentHasScope, blueprint)

MAX_FILES = share_core.MAX_FILES
MAX_PROMPT = share_core.MAX_PROMPT
MAX_FILE_MB = share_core.MAX_FILE_MB      # 测试按本模块名打桩
FILES_ONLY_PROMPT = share_core.FILES_ONLY_PROMPT


@blueprint.route('/api/agent/share/nodes')
def shareNodes():
    agent, err = requireAgentScope(SCOPE_SHARE)
    if agent is None:
        return err
    return jsonify(share_core.nodesPayload())


@blueprint.route('/api/agent/share/tasks')
def shareTasks():
    agent, err = requireAgentScope(SCOPE_SHARE)
    if agent is None:
        return err
    return jsonify(share_core.tasksPayload())


@blueprint.route('/api/agent/share', methods=['POST'])
def shareSubmit():
    agent, err = requireAgentScope(SCOPE_SHARE)
    if agent is None:
        return err

    uploads = [f for f in request.files.getlist('file')
               if f is not None and (f.filename or '')]
    if len(uploads) > MAX_FILES:
        share_core._audit('agent.share', '', 'fail',
                          'files=%d 超过 %d' % (len(uploads), MAX_FILES))
        return jsonify({'error': '一次最多 %d 个文件' % MAX_FILES}), 400

    opts, err = share_core.parseShareOptions(request.form, len(uploads))
    if err is not None:
        payload, code = err
        share_core._audit('agent.share', '', 'fail', payload['error'])
        return jsonify(payload), code
    taskId = opts['taskId']

    # 接到已有任务：先确认目标存在且可投，再落盘（避免无主垃圾文件）
    task = None
    if taskId:
        task, err = share_core.findOpenTask(taskId)
        if err is not None:
            payload, code = err
            share_core._audit('agent.share', taskId, 'fail', payload['error'])
            return jsonify(payload), code

    # 授权门（批次 B2）：往远端执行点投附件要第三层授权。成员端根白名单与
    # 中枢 filesWrite 只回答"能写哪里"，令牌 scope 才回答"谁可以写"。
    targetNode = task_node.of(task) if task is not None else opts['node']
    if not task_node.isLocal(targetNode) \
            and not agentHasScope(agent, SCOPE_UPLOAD_REMOTE):
        share_core._audit('agent.share', taskId, 'deny',
                          'need=upload_remote node=%s' % targetNode[:60])
        return jsonify({'error': '向远端执行点投递附件需要 upload_remote 权限',
                        'need': 'upload_remote'}), 403

    stagedFiles = []
    stagedFolder = ''
    folderName = ''
    if opts['organize'] == 'folder':
        try:
            stagedFolder = staging.createStagedFolder(opts['folderName'],
                                                      tasks_store.taskRoot())
        except ValueError as e:
            share_core._audit('agent.share', taskId, 'fail', '文件夹名不合法')
            return jsonify({'error': str(e)}), 400
        except OSError as e:
            share_core._audit('agent.share', taskId, 'fail', '建文件夹失败')
            return jsonify({'error': '建文件夹失败: %s' % e}), 500
        folderName = os.path.basename(stagedFolder)   # 重名时以 (2) 后的实际名为准
    for item in uploads:
        name = staging.safeAttachmentName(item.filename)
        if not name:
            share_core._discard(stagedFiles, stagedFolder)
            share_core._audit('agent.share', taskId, 'fail', '文件名不合法')
            return jsonify({'error': '文件名不合法（需纯文件名，≤120 字符）: %s'
                                     % (item.filename or '')}), 400
        try:
            stagedFiles.append(staging.saveStagedStream(
                name, item.stream, MAX_FILE_MB * 1024 * 1024,
                tasks_store.taskRoot(), folder=stagedFolder or None))
        except ValueError:
            share_core._discard(stagedFiles, stagedFolder)
            share_core._audit('agent.share', taskId, 'fail',
                              '文件超过 %dMB' % MAX_FILE_MB)
            return jsonify({'error': '单文件超过 %dMB 上限: %s'
                                     % (MAX_FILE_MB, name)}), 413
        except OSError as e:
            share_core._discard(stagedFiles, stagedFolder)
            share_core._audit('agent.share', taskId, 'fail', '写盘失败')
            return jsonify({'error': '写盘失败: %s' % e}), 500

    payload, code = share_core.deliverShare(opts, stagedFiles, stagedFolder,
                                            folderName, task)
    return jsonify(payload), code