# -*- coding: utf-8 -*-
"""结构门禁的登记数据（批次 G1 从 check_structure.py 拆出腾预算）。

预算/存量例外、特性模块表、允许的依赖方向、window 全局白名单、跨模块私有
接缝白名单——纯数据，无逻辑。check_structure.py 只留扫描逻辑。
"""
BUDGET = {'.py': 400, '.js': 500, '.css': 500}

# 存量例外: 只减不增（行数上限）。2026-11-05 read_lines 空转暴露的 7 个文件已
# 拆完移除; 2026-11-12 起按"先认清现状"重新登记现有超预算文件（上限 = 当前
# 行数），之后只能变短; 未列本表的新文件仍必须直接符合预算。
LEGACY = {
    'modules/task_policy.py': 432,                # 审批边界判定（纯函数）；2026-11-15 合并 main（拆出 task_policy_pipe/task_policy_readonly）后新登记：L5 按名字批量杀进程内建询问（KILL_BY_NAME_RES/killByNameHit + evaluate 接线，policy 压不掉）
    'config.py': 603,   # 全局配置聚合（读取/热改/默认值/迁移），拆分会散落语义；2026-11-14 +32=主管收件箱两个阈值 leadWakeMergeSec/leadIdleSec（批次 P3）；2026-11-15 +38=批次 L 三个阈值 leadResponseSec/autoResumeAfterRestart/toolStallSec；+17=主管审批限期 leadApprovalGraceSec（批次 A）
    # .agents/skills/deskport/{deskport,driver}.py 是发给外部智能体的「拷一个文件就能跑」
    # 客户端/驱动（零依赖、也能当 CLI），拆分会破坏这个约定——按单文件登记，只许变短。
    '.agents/skills/deskport/deskport.py': 854,   # 单文件客户端（见上注）；2026-11-13 加 task/tasks/wait；2026-11-14 +34=技能包版本协商（自报 X-Deskport-Skill-Version、收 stale 标记并提醒重拉）；2026-11-15 合并批次 A/SR 与 D：+91=主管通信子命令 lead/inbox/report + close/approve（派活收件箱汇报之外，主管关下属与批审批也要在 A1-6 边界内，例 11 无人干预实测要用）+ 库方法（批次 A A2），+39=lead --register 自助成为外部主管与 register_lead 库方法（批次 SR，两侧 lead 子命令合一：不带参查看、--register 注册），+11=inboxAck 收件箱确认 + create_task/CLI 的 --title 标题字段（批次 D，服务端批次 T 起接收），+4=task --session 接回原会话继续干活（kimiSessionId + fork=false，返工/复验复用执行方），+8=answer 命令（L2 答复下属提问）
    '.agents/skills/deskport/driver.py': 597,     # 单文件驱动模板（见上注）；2026-11-14 +19=审批 409 分类处理（P0-3）；2026-11-15 +111=inbox 收件箱模式（外部主管长轮询+自动 ack）+ resume 暂停续接 + title.txt 作为 title 字段传给建任务（批次 D，不拼首行）
    'agent_node/acp_session.py': 454,             # ACP 会话单文件实现（见上注）
    'agent_node/node_api.py': 417,                # 节点 API 单文件实现（见上注）
    'frontend/assets/css/files.css': 522,         # 文件页样式一体，选择器跨文件耦合，拆分暂缓
    'modules/agent_local.py': 491,                # 本机内置主机全等价实现（exec/sftp/读文件），拆分中
    'modules/agent_api.py': 412,                  # agent 家族门面：守卫/主机解析/入参归一都在这
                                                  # （批次 C 把各模块的私有名收进来，批次 H 加
                                                  # onBehalfOfKwargs，批次 J 加 nodeArg）——只减不增，
                                                  # 下一步把主机解析与入参归一拆成独立小模块
    'modules/agent_nodes.py': 592,                # 节点注册+隧道+引擎注册一体；2026-11-15 +12=RemoteAcpClient.cancel 发完再读一次节点实况、如实回报回合是否还在跑（L4）
    'modules/agent_sessions.py': 443, 'modules/leads_inbox.py': 405, 'modules/leads_api.py': 409,  # leads_inbox：主管收件箱（存储+触发点+sweeper+端点一体），11-15 +20=escalation 落定（entryResolved + inbox resolved 字段）；11-15 N1 拆回预算内（down notice 瘦身 liveDownEntries 与无动静提示 idleNotice 移归 lead_notices，启动补标内联进 bindApp），下次改先拆；2026-11-15 批 G1：+2=URGENT_KINDS 增加 task_judgment/result_missing（需判断的结果与缺结果立即唤醒）；sessions：持久会话（bash 常驻通道）管理｜leads_api：主管管理面 + 令牌通道自查/自助注册，2026-11-15 合并批次 A（+16=托管主管停用/删除即吊销专属令牌）与批次 SR（+114=POST /api/agent/lead 自助成为主管 + 认领历史任务）后 409，下次改先拆
    'modules/tasks_api.py': 1044,                  # 任务 API 面最大，拆分需配回归；2026-11-14 +12=收件箱关闭触发点 + by 记名（补 4，批次 P3）；+18=wait timeout 契约整改（P3b 退回：0/负数显式回 1s，helper+两条回归）；2026-11-15 +16=建卡与启动分步（start 参数 + startTask，批次 A）+ 审批决定者 by（A4-4）；+21=answer 端点（L2）+ cancel 响应 nodeStillRunning（L4）；+14=批次 DR：排空期间消息只排队不开轮（deliverMessage）+ 远端 fork:false 接管未关闭占用 409 + close 终态后统一释放节点会话（原仅强杀分支）
    'modules/tasks_runner.py': 944,               # 任务执行编排；+17=lastTurn 空转标记（11-13）；+5=清死审批标记（11-14，P0-3）；+39=收件箱四个触发点+finalLen+by 记名（11-14，批次 P3）；+2=drain 转发 by（11-14，批次 P3b）；2026-11-15 +12=流式参数后到补丁行（tool_args_patch 落 jsonl，删空转 save）；+18=残片首帧也回填（_needsBackfill(args, argsText)：已落卡空/非法 JSON 且后到合法 JSON 才补，含 U 批实测 "{\"command\": " 半截形状）（批次 PS1）；+15=审批先上交主管（A4：notice→escalation 调用 + grace 限期 + 结果事件 by）+6=流式残片参数让位完整快照（例 11 实测：byCall 首帧 "{" 挡住 streamed，边界内命令被误挂起）（批次 A）；+80=批次 L：L2 提问路径（_isAskUserQuestion/perm.question/队首当答复/answerPermission）+ L4 清 _toolStall；+10=批 PS2 削峰：chunk 路径去 save（lastText 轮末随关键变迁落盘）+ 五处非关键 save 改 touch（commands/contextHigh×2/lastTurn/liveFrom/resuming）；+11=批次 DR（合并 PS2 后净增，按合并后实际行数核对）：_promptOnce 排空闸门（关闭则跳过本轮）与四个调用点守卫、_drainQueuedMessages 排空即留队、startSession 接回重试包装、resume 看门狗排空不计时
    'modules/tasks_store.py': 608, 'modules/task_events.py': 420,  # tasks_store：任务存储；2026-11-14 +7=启动清死审批标记（P0-3）；+2=summaryBrief/summary 带 task.lead（批次 Q）；2026-11-15 +143=批 PS2 削峰：touch/flush/_writeNow/shutdown/_closeIdleJsonl + 共享 flush 线程（弱引用登记，每进程一条）与 atexit 退出 flush + JsonlWriter 句柄管理（append_event 不再每事件 save，log_write 长开句柄）；+10=批次 L：L3 pausedBy='restart'/清 resuming 唤醒标记 + L6 只读闸门（save/log_write 两个漏斗入口）；2026-11-15 批 G1：+4=首条消息按 expects 提示 submit_result（结构化交付要求）；2026-11-15 批 G2：+1=summaryBrief 带 workItemId（指挥台工作项展开）｜task_events：批 PS1 新建（357 行，原在 400 预算内）；2026-11-15 +63=批 PS2：JsonlWriter 每任务长开句柄（方案 §6 风险 6，替代 appendJsonl 每条 open/close）+ _applyPatch 补丁应用单一来源（回填与 eventBySeq 共用）+ readLogTailWithPatches 的 'tool_args_patch' 廉价预筛
    'tests/test_agent_api.py': 611,               # 智能体 API 回归集
    'tests/test_r47_remote_adopt.py': 481,        # R47 远端 adopt 回归集
    'tests/test_leads_inbox_store.py': 402, 'tests/test_tasks_api.py': 1620, 'tests/test_leads_escalation.py': 475, 'tests/test_task_policy.py': 506, 'tests/test_task_persist_restart.py': 450,  # inbox_store：11-15 合并 A 与 V 后 402，下次改先拆｜tasks_api：任务 API 回归集（面最大）；11-14 +15=P0-1 闸门、+12=wait 超时 leapfrog 修复回归（含三个存量测试改新契约）；+39=wait timeout 0/负数老契约回归（P3b 退回整改）｜leads_escalation：审批上交主管回归集，11-15 +58=escalation 落定（resolved 不计未读/L1 不提醒/grace 不上交 + inbox resolved 字段）｜task_policy：审批边界回归集，11-15 合并 main（拆出 task_policy_pipe/readonly）后 506：L5 按名字杀进程正反例（含 //IM 双斜杠与 Get-Process 管道返工用例）｜persist_restart：批 PS1 新建（378 行）；2026-11-15 +72=批 PS2：开句柄失败两例先关句柄再注入（长开句柄后 open 只在首写/句柄作废后发生）+ 新增 6c 写中途失败作废句柄、重试与恢复
}

SKIP_DIRS = {'.git', '.venv', '__pycache__', 'vendor', 'node_modules', '.deploy-backup', '.run', '.pytest-tmp'}  # pytest basetemp：测试现造文件，非源码
FEATURES = {'aihandoff', 'agent_api', 'agent_local', 'agent_nodes', 'agent_sftp', 'agent_sftp_write',
            'agent_sessions', 'agent_share', 'agent_skill_files', 'agent_tunnel', 'command', 'command_view', 'control',
            'feedback', 'feedback_store', 'files', 'files_archive', 'files_base', 'files_browse',
            'files_federation', 'files_pins', 'files_transfer', 'lead_notices', 'leads_api', 'leads_escalation', 'leads_hosted', 'leads_inbox', 'leads_store',
            'screen', 'ssh', 'task_cwd', 'task_health', 'task_node', 'tasks', 'tasks_api', 'tasks_runner', 'tasks_store',
            'terminal', 'worklines_api', 'worklines_store', 'feedback_envelope', 'feedback_status', 'feedback_rate', 'feedback_views', 'feedback_ops',
            'workitem_store', 'workitem_state', 'workitem_projects', 'workitem_core',
            'workitem_git', 'workitem_prompts', 'workitem_view', 'workitem_setup',
            'workitem_actions', 'workitem_flow', 'workitem_pipeline', 'workitem_watch',
            'workitem_recover', 'workitem_integrate'}
# 允许的“上游 → 下游”依赖；其余同层 import 视为违规
ALLOWED_FEATURE_IMPORTS = {('aihandoff', 'files'), ('tasks', 'tasks_api'),
                           ('tasks_api', 'tasks_runner'), ('tasks_api', 'tasks_store'),
                           ('tasks_runner', 'tasks_store')}
# 批次 C（2026-11-13）扩面登记：按 modules/ 现名核实的特性模块全部纳入后，
# 逐条认定现有依赖方向。约定: agent_api/files_base/feedback_store/tasks_store 等
# 是各自家族的「底座」（蓝图/守卫/仓库），家族成员 → 底座是设计方向；
# 反向（底座 → 成员）只在函数内延迟导入、且只为请求期分工时登记。
ALLOWED_FEATURE_IMPORTS.update({
    # —— agent 家族（蓝图同源, app.py 显式登记; agent_api 是门面与守卫基座）——
    ('agent_local', 'agent_api'),    # 本机内置主机实现, 复用 agent_api 的令牌守卫/输出上限常量
    ('agent_local', 'agent_sftp'),   # 本机文件读复用 agent_sftp 的错误码语义（mapSftpError 公开助手）
    ('agent_api', 'agent_local'),    # 反向仅请求期: agentExec 本地分支委托 agentLocalExec, 函数内延迟导入不成加载环
    ('agent_sftp', 'agent_api'),     # SFTP 路由复用守卫/主机解析
    ('agent_sftp', 'agent_local'),   # 本地分支委托 sftpListLocal 等（函数内延迟导入）
    ('agent_sftp_write', 'agent_api'),   # 写操作路由复用守卫/蓝图
    ('agent_sftp_write', 'agent_local'),   # 函数内延迟导入 localEnabled/normalizeLocalPath
    ('agent_sftp_write', 'agent_sftp'),    # 写操作复用连接/错误码/建目录助手（C2 公开化后）
    ('agent_sessions', 'agent_api'),       # 持久会话复用守卫/主机解析
    ('agent_sessions', 'agent_local'),     # 持久会话的 shell 解析在本机模块
    ('agent_share', 'agent_api'),          # 手机转发入口复用守卫/scope 常量
    ('agent_skill_files', 'agent_api'),    # 技能包附件路由复用守卫/蓝图
    ('agent_upload', 'agent_api'),         # 手机上传复用守卫/scope/蓝图
    ('agent_nodes', 'agent_tunnel'),  # 远端执行点经 HTTP 隧道访问（tunnelPort/dropTunnelsFor）
    ('agent_nodes', 'task_node'),     # task_node 是执行点(node/cwd)语义的唯一出处, 各特性只读消费
    ('agent_nodes', 'tasks_runner'),  # 本地/联邦引擎的会话线程委托 tasks_runner 跑 ACP
    ('agent_share', 'task_node'),     # 执行点语义（同上）
    ('agent_share', 'tasks_store'),   # 暂存区根 taskRoot() 由 tasks_store 提供（staging 文档约定的调用方传入模式）
    # —— files 家族（files_base 是同蓝图共享底座: blueprint/log/属性常量）——
    ('files', 'files_base'),          # files.py 是家族门面, 蓝图出自 files_base
    ('files', 'files_archive'),       # 门面聚合登记各路由模块（noqa: F401 再导出）
    ('files', 'files_browse'),        # 门面聚合（同上）
    ('files', 'files_pins'),          # 门面聚合（同上）
    ('files', 'files_transfer'),      # 门面聚合（同上）
    ('files_archive', 'files_base'),  # 家族底座
    ('files_browse', 'files_base'),   # 家族底座
    ('files_pins', 'files_base'),     # 家族底座
    ('files_transfer', 'files_base'), # 家族底座
    # 批次 F1 拆 feedback 家族：store=表原语/提交/pendingSummary；envelope=信封校验；status=状态机规则（纯）；rate=频控持久化；views=读路径；ops=写操作；feedback.py 聚合路由
    ('feedback', 'feedback_store'), ('feedback', 'feedback_envelope'), ('feedback', 'feedback_views'), ('feedback', 'feedback_ops'), ('feedback', 'feedback_rate'), ('feedback_store', 'feedback_status'), ('feedback_views', 'feedback_store'), ('feedback_ops', 'feedback_store'), ('feedback_ops', 'feedback_status'),
    # leads（批次 Q）主管家族 + leads_inbox（批次 P3 收件箱一体模块）: Q=leads_api→leads_store/agent_api/tasks_store、tasks_api→leads_store（建任务读主管记录）; P3=触发点在 runner/api 各一行（任务主链路→收件箱，方向单向），sweeper 唤醒调 deliverMessage（函数内延迟导入不成环），扫任务读 tasks_store、游标/记录读 leads_store，human 端点与绑任务在 leads_api 反向调 append/wakeNow
    ('leads_api', 'leads_store'), ('leads_api', 'agent_api'), ('leads_api', 'tasks_store'), ('tasks_api', 'leads_store'), ('leads_inbox', 'tasks_api'), ('leads_inbox', 'tasks_store'), ('leads_inbox', 'leads_store'), ('tasks_runner', 'leads_inbox'), ('tasks_api', 'leads_inbox'), ('leads_api', 'leads_inbox'),
    ('command_view', 'tasks_store'), ('command_view', 'leads_store'), ('command_view', 'leads_inbox'),  # 批次 U 指挥台：只读聚合三处现有存储，不改它们
    ('lead_notices', 'leads_inbox'), ('lead_notices', 'leads_store'), ('lead_notices', 'tasks_store'), ('leads_inbox', 'lead_notices'), ('tasks_runner', 'lead_notices'), ('task_health', 'tasks_store'), ('task_health', 'leads_inbox'), ('task_health', 'tasks_runner'), ('tasks_runner', 'tasks_api'),  # 批次 L：lead_notices=L1 超时提醒+L2 提问条目，task_health=L3 续接+L4 巡检（state_owner 归 INFRA）；tasks_runner→tasks_api 仅函数内延迟导入（answerPermission 复用 deliverMessage）
    ('leads_escalation', 'tasks_store'), ('leads_inbox', 'leads_escalation'), ('lead_notices', 'leads_escalation'),  # N1：leads_escalation→tasks_store 读任务终态与 pendingPermission（补标判据与"主管自己待批"提示存续条件，只读）；leads_inbox→leads_escalation 反向仅启动期（bindApp 里跑一次历史补标）、lead_notices→leads_escalation 反向仅扫掠期（ownPermissionLive），均为函数内延迟导入不成环
    ('leads_hosted', 'tasks_api'), ('leads_hosted', 'tasks_store'), ('leads_hosted', 'agent_skill_files'), ('leads_hosted', 'agent_tokens'), ('leads_hosted', 'agent_api'), ('leads_hosted', 'leads_store'), ('leads_api', 'leads_hosted'), ('leads_escalation', 'leads_inbox'), ('leads_escalation', 'leads_store'), ('tasks_runner', 'leads_escalation'),  # leads_hosted（批次 A 一键托管主管，编排层：建任务核心/技能包/令牌名额/吊销入口）+ leads_escalation（A4 审批上交的条目写入，判定与投递走 leads_inbox 公开面；tasks_runner 审批处一行调它；读 leads_store 判主管是否已停用）
    # —— tasks/worklines 家族消费执行点语义与节点注册表（批次 B3/C 后公开的方向）——
    ('tasks_api', 'agent_nodes'),     # 执行点存在性校验/寻址/模型声明
    ('tasks_api', 'task_cwd'),        # cwd 前置校验（远端 cwdRoots, 批次 B3）
    ('tasks_api', 'task_node'),       # 执行点语义
    ('tasks_api', 'worklines_store'), # 工作线是任务的上级聚合, 建/挂任务时读工作线存储
    ('tasks_runner', 'agent_nodes'),  # 联邦成员判定（peerJudged）
    ('tasks_runner', 'task_node'),    # 执行点语义
    ('tasks_runner', 'worklines_store'),  # 执行后回写工作线上下文
    ('tasks_store', 'task_cwd'),      # 摘要/提示渲染里的 cwd 文案
    ('tasks_store', 'task_node'),     # 执行点语义
    ('worklines_api', 'task_node'),   # 执行点语义
    ('worklines_api', 'tasks_api'),   # 工作线 API 的任务编排复用 tasks_api（其文档自述的设计）
    ('worklines_api', 'tasks_store'), # 建/挂任务时读任务仓库
    ('worklines_api', 'worklines_store'),  # 自己的存储层
    ('worklines_store', 'task_node'), # 工作线执行点判定
    # 批次 G1（结构化结果 submit_result）：task_result=核心（校验/提交/追问）、
    # task_result_mcp=MCP 面+会话令牌+注入、task_result_api=HTTP 提交面、
    # task_permission=审批漏斗（从 tasks_runner 拆出）、task_kimi_sessions=接手
    # 候选（从 tasks_api 拆出）。方向：API/runner → 结果家族 → 存储/收件箱底座
    ('task_result', 'leads_inbox'), ('task_result', 'tasks_store'), ('task_result', 'task_node'),
    ('task_result_mcp', 'task_result'), ('task_result_mcp', 'tasks_store'), ('task_result_mcp', 'task_node'),
    ('task_result_api', 'task_result'), ('task_result_api', 'task_result_mcp'), ('task_result_api', 'tasks_store'),
    ('task_permission', 'leads_escalation'), ('task_permission', 'tasks_store'), ('task_permission', 'task_node'), ('task_permission', 'task_result_mcp'),
    ('task_permission', 'agent_nodes'),  # 函数内延迟导入查联邦成员（peerJudged 判定）
    ('task_kimi_sessions', 'agent_nodes'),
    ('tasks_runner', 'task_permission'), ('tasks_runner', 'task_result'), ('tasks_runner', 'task_result_mcp'),
    ('tasks_api', 'task_result'), ('tasks_api', 'task_result_mcp'),
    ('agent_nodes', 'task_result'),       # 联邦工厂镜像对端 result 事件（on_result 回调）
    # 批次 G2（流水线：实施→验收→返工→集成）：workitem_store=工作项存储、
    # workitem_state=状态机/创建校验/足迹判定、workitem_projects=项目配置。
    # 方向：存储 → 状态机（history/迁移唯一出处）；配置 → 审批边界校验
    ('workitem_store', 'workitem_state'),
    ('workitem_projects', 'task_policy'),
    # 引擎家族（core=共用内核/建任务原语；setup=创建调度与验收任务；
    # actions=发消息/接回/换手/决策；flow=结果流转；pipeline=tick 与集成
    # 回调；watch=异常巡检；recover=重启恢复；view=展示口径）。方向自上而下
    # 单向（core 是底座）；actions→setup、flow→pipeline 的反向只在函数内
    # 延迟导入（同 agent_api↔agent_local 的既有做法）
    ('workitem_core', 'tasks_store'), ('workitem_core', 'tasks_api'),
    ('workitem_core', 'leads_inbox'), ('workitem_core', 'worklines_store'),
    ('workitem_core', 'task_result'),
    ('workitem_view', 'tasks_store'), ('workitem_view', 'workitem_core'),
    ('workitem_integrate', 'workitem_git'), ('workitem_integrate', 'workitem_store'),
    ('workitem_setup', 'tasks_api'), ('workitem_setup', 'workitem_view'),
    ('workitem_setup', 'workitem_core'), ('workitem_setup', 'workitem_actions'),
    ('workitem_actions', 'tasks_api'), ('workitem_actions', 'tasks_store'),
    ('workitem_actions', 'workitem_view'), ('workitem_actions', 'workitem_integrate'),
    ('workitem_actions', 'workitem_core'), ('workitem_actions', 'workitem_setup'),
    ('workitem_flow', 'workitem_core'), ('workitem_flow', 'workitem_setup'),
    ('workitem_flow', 'workitem_actions'), ('workitem_flow', 'workitem_integrate'),
    ('workitem_pipeline', 'tasks_api'), ('workitem_pipeline', 'workitem_core'),
    ('workitem_pipeline', 'workitem_setup'), ('workitem_pipeline', 'workitem_actions'),
    ('workitem_pipeline', 'workitem_flow'), ('workitem_pipeline', 'workitem_integrate'),
    ('workitem_watch', 'tasks_store'), ('workitem_watch', 'tasks_api'),
    ('workitem_watch', 'workitem_store'),
    ('workitem_watch', 'workitem_core'),
    ('workitem_watch', 'workitem_pipeline'), ('workitem_watch', 'workitem_actions'),
    ('workitem_recover', 'tasks_store'), ('workitem_recover', 'workitem_core'),
    # 家族内部：底座（store/state/projects/git/prompts/view）可被上层自由消费
    ('workitem_core', 'workitem_git'), ('workitem_core', 'workitem_prompts'),
    ('workitem_core', 'workitem_state'), ('workitem_core', 'workitem_store'),
    ('workitem_core', 'workitem_projects'), ('workitem_core', 'leads_store'),
    ('workitem_view', 'workitem_state'), ('workitem_view', 'workitem_store'),
    ('workitem_setup', 'workitem_git'), ('workitem_setup', 'workitem_prompts'),
    ('workitem_setup', 'workitem_state'), ('workitem_setup', 'workitem_store'),
    ('workitem_setup', 'workitem_projects'),
    ('workitem_actions', 'workitem_git'), ('workitem_actions', 'workitem_prompts'),
    ('workitem_actions', 'workitem_state'), ('workitem_actions', 'workitem_store'),
    ('workitem_flow', 'workitem_git'), ('workitem_flow', 'workitem_prompts'),
    ('workitem_flow', 'workitem_state'), ('workitem_flow', 'workitem_store'),
    ('workitem_pipeline', 'workitem_git'), ('workitem_pipeline', 'workitem_prompts'),
    ('workitem_pipeline', 'workitem_state'), ('workitem_pipeline', 'workitem_store'),
    ('workitem_watch', 'workitem_store'), ('workitem_watch', 'workitem_state'),
    ('workitem_watch', 'workitem_prompts'),
    ('workitem_setup', 'workitem_integrate'), ('workitem_setup', 'leads_store'),
    ('workitem_recover', 'workitem_state'), ('workitem_recover', 'workitem_store'),
    # 函数内延迟导入的反向边（作业/巡检线程回调单一写者）
    ('workitem_actions', 'workitem_pipeline'),
    ('workitem_flow', 'workitem_pipeline'),
})
# 基础设施层: 可被任何层依赖, 但自己不得 import 特性层 (依赖只能向上)
# termcore 是 terminal/ssh 共用的共享内核, 归基础设施而非特性;
# capability 是权限判定入口 (纯函数, 不 import flask/config), 各特性的
# scope/联邦写判定都收敛到它 (批次 H)
INFRA = {'state', 'internal_api', 'events', 'fsutil', 'textio', 'media', 'contracts',
         'decision', 'termcore', 'termprobe', 'wsproto', 'winenv', 'capability'}
ALLOWED_WINDOW = {'WPFeedback', 'WPFeedbackCapture', 'WPFeedbackPanel',   # 反馈入口
                  'fetch', 'onerror',              # 反馈捕获层给请求/错误打的钩子
                  'termManager', 'termUI', 'createSshSession', 'SSHUI',
                  'AppShell', 'AppShellV2', 'Icons', 'SftpMemory',
                  'SftpState', 'SftpMenu', 'SftpSelect', 'SftpTransfer',
                  'FilesMethodsSelect', 'TermSidebar', 'TermGeometry', 'WP_NAV',
                  'TermClient', 'TermFeatures', 'toggleMultiExec', 'WPCommon',
                  'FilesMethodsCore', 'FilesMethodsOps', 'FilesMethodsUi', 'FilesMethodsAi',
                  'SSHUiCtx', 'SSHSftpUI', 'WP_MSG', 'SSHHostStore', 'WP_BRAND',
                  'TermEnv', 'TermSessionUI', 'TermControls',
                  # 分享表单提交体构造（share-form.js，纯函数，tests/js/ 有 node 单测）
                  'WPShareForm', 'WPSettingsLeads', 'WPTasksLeads', 'WPTasksOverview', 'WPTasksResult',  # 批次 S 主管卡片/任务页筛选 + 批次 U 指挥台一屏（纯函数有 node 单测）
                  # 设置页「我的权限」卡片（settings-capability.js，view/kindText 有 node 单测；mixin 把三态和 loadCapability 挂进 settings.js）
                  'WPSettingsCapability', 'WPFeedbackMine', 'WPSettingsFeedback'}   # 批次 F1 后两个：反馈「我的反馈」页签 + 设置页「反馈」处理卡片（纯函数有 node 单测）
# 跨模块私有接缝存量白名单（批次 C, C2/C4）: (使用方, 提供方) → 私有名集合。
# 规则: 跨模块 import 不得出现单下划线前缀（引私有名 / 公有名起私有别名都算）。
# 本表登记的是「同包内部接缝」——提供方是使用方所在家族的共享底座, 私有助手
# 只在该家族内流转; 每条附理由, 且只减不增: 新接缝必须公开化, 不许往这加项。
# 批次 J: node/host 归一（agent_api.nodeArg）后, agent_sftp/agent_sessions 改走
# 公开的 resolveHost / unknownHostError, agent_api 的两条主机解析接缝随之删除。
PRIVATE_SEAM_ALLOWED = {
    ('agent_nodes', 'tasks_federation'): {'_memberCall'},      # federation 成员 RPC 是 tasks_federation 的内部机制
    ('agent_sessions', 'agent_local'): {'_resolveShell'},      # 持久会话的 shell 解析在本机模块
    ('agent_sftp', 'ssh_sftp'): {'_openSftp'},                 # SFTP 连接助手, ssh_sftp → agent_sftp 的本地分支要用
    ('agent_skill_files', 'agent_skills'): {'_skillsDir'},     # 技能包附件读写 agent_skills 的技能目录
    ('aihandoff', 'inbox_scan'): {'_under', '_sha256', '_dedupe'},  # 收件箱家族的遍历/摘要/去重助手
    ('control', 'control_base'): {'_audit', '_readAudit'},     # control 家族共享底座的审计读写
    ('control_input', 'control_base'): {'_audit'},             # 同上
    ('files_federation', 'external_proxy'): {'_dropHopHeaders'},  # 出站代理剥 hop 头的内部助手, 仅文件转发一处调用
    ('model_availability', 'agent_nodes'): {'_healthModelsCached'},  # 读节点注册表的模型健康缓存
    ('model_availability', 'tasks_federation'): {'_memberCall'},     # 同 agent_nodes: 成员 RPC 内部机制
    ('plugins_discover', 'plugins_base'): {'_log'},            # plugins 家族共享底座的日志助手
    ('plugins_load', 'plugins_base'): {'_log'},                # 同上
    ('ssh_sftp', 'ssh_sftp_base'): {'_openSftp', '_sftpSession'},  # ssh_sftp 家族的会话助手, 只在本家族与 agent_sftp/task_inputs_sftp 内流转
    ('ssh_sftp_ops', 'ssh_sftp_base'): {'_sftpSession'},       # 同上
    ('ssh_sftp_transfer', 'ssh_sftp_base'): {'_openSftp', '_sftpSession'},  # 同上
    ('task_inputs_sftp', 'ssh_sftp_base'): {'_openSftp'},      # 同上
    ('tasks_api', 'task_wait'): {'_blockWait', '_parseWaitSeconds', '_waitSnapshot'},  # tasks/worklines 两个 API 共用的等待态助手
    ('worklines_api', 'task_wait'): {'_parseWaitSeconds'},     # 同上
}

# 执行点门禁（2026-11-13）：对外只剩 node（去哪台机器，缺省 local）与
# cwd（在哪个目录跑，缺省任务目录）。engine 不得回到 modules/ 与 frontend/：
# 不许当入参名、不许读旧字段、用户可见文案不许出现「引擎」。
# 唯一白名单 modules/task_node.py：旧任务记录 / 旧工作线元数据读取时映射一次
# （acp→local），以及请求里带 engine 时显式 400——这两处必须认识旧名字。
NODE_FIELD_ALLOWED = {'modules/task_node.py'}
