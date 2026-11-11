# 智能体 API（Agent API）

给外部 AI 程序的机器接口：用长效令牌直接在**已保存的 SSH 主机和 Deskport 本机**上
执行命令、读写文件、开持久会话，并把复杂任务委托给托管智能体。与浏览器终端互补
——人用 WS 会话，智能体用这套 HTTP API。

安全模型：**外部智能体 = 可信代码**。令牌等价于在受控主机上执行任意命令的能力，
请只签发给自己的程序，并妥善保管。

## 签发令牌

1. 生成随机令牌并计算哈希：

```bash
TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
python -c "import hashlib; print('tokenHash =', hashlib.sha256('$TOKEN'.encode()).hexdigest())"
```

2. 把 `tokenHash` 写进 `config.json`（令牌明文自己保存，仓库与日志里都不落）：

```json
"agentTokens": [
  { "id": "claude", "name": "Claude", "tokenHash": "<SHA-256>" },
  { "id": "kimi",   "name": "Kimi Code", "tokenHash": "<SHA-256>" }
]
```

3. 重启 Deskport 生效；撤销 = 删除对应条目再重启。

**每个智能体单独签发一个令牌**：审计按令牌名留痕，混用一个令牌会让
"谁干了什么"查不清（反馈 §8.1）。任务、会话对所有令牌可见——这是单用户
系统的设计（你的智能体都是自己的），不按令牌隔离（反馈 §8.2）。

## 认证与错误格式

- 每次请求带 `X-Agent-Token: <令牌>` 或 `Authorization: Bearer <令牌>`；
  未带/不匹配 → `401 {"error": ...}`（JSON）。
- `/api/*` 的所有错误都是 JSON：404/405/500 含 `error`/`code`，500 另有
  `requestId`（服务端日志可按 requestId 查 traceback）。未知 API 路径由
  认证守卫统一 401 JSON，不暴露端点存在性。
- 浏览器 Cookie 登录态同样放行（面板 UI 复用同一组端点）。

## 主机：本机与 SSH 全等价

`GET /api/agent/hosts` 返回 SSH 主机 + Deskport 本机（`id: "local"`）：

```json
{"hosts": [
  {"id": "ai-xmyz", "name": "ai-xmyz", "host": "10.110.13.79", "port": 52222},
  {"id": "local", "name": "local", "host": "127.0.0.1", "port": 0,
   "os": "windows", "defaultShell": "bash", "shells": ["bash", "powershell", "pwsh"]}
]}
```

同一套 exec / sessions / sftp 接口，只换 host 名。本机 shell 可选
`bash`（默认，Git Bash）/ `powershell` / `pwsh`，传不支持的值 400 并附可选列表；
响应回显参数。路径双写法：`/e/事项` 与 `E:\事项` 都接受，响应统一 Windows
原生写法。服务端负责抹平 shell 差异：PowerShell 输出统一转 UTF-8、
`$LASTEXITCODE` 收敛为 `exit`；会话 shell 定死在创建时，本机会话暂只支持 bash。
隐私开关：`config.json` 设 `"agentLocalEnabled": false` 可整体下线本机主机
（缺省开启）。

## 快命令（无状态，单发）

```text
POST /api/agent/ssh/exec
{"host": "ai-xmyz", "command": "uptime", "timeoutSec": 30, "cwd": "/path"}
→ {"exit": 0, "out": "...", "err": "...", "timedOut": false, "reason": "",
   "outTruncated": false, "durationMs": 123, "timeoutSec": 30, "pid": 4321}
```

- `cwd` 可省；用户命令原文透传（heredoc/多行/引号安全），`cwd` 由外壳处理；
- `timeoutSec` 严格校验：1–600 整数，越界/非整 → 400，响应回显生效值；
- 含 NUL 字符 → 400；
- stdin 即时 EOF：`git commit`（不带 -m）、`ssh`、`python -` 等
  读 stdin 的命令不会挂起；`read x` 得到 rc=1 + EOF；
- 输出上限 2MB（`outTruncated` 标记）；`"encoding": "base64"` 可拿
  `outB64`/`errB64` 逐字节原文（非 UTF-8 输出不丢内容）；
- 超时 → `exit: null` + `reason: "timeout"`，远端进程组被整组击杀
  （先 TERM 后 KILL）；响应带 `pid` 便于自查；
- 远端默认注入 `LANG=C.UTF-8`：UTF-8 输出是契约。

本机专用别名路由 `POST /api/agent/local/exec` 等价于
`POST /api/agent/ssh/exec` + `host: "local"`。

## SFTP（读写文件，整读 8MB，可分块）

```text
POST /api/agent/sftp/list  {"host": "...", "path": "/srv"}      → {path, entries}
POST /api/agent/sftp/get   {"host": "...", "path": "...", "offset": 0, "length": 4096}
POST /api/agent/sftp/put   {"host": "...", "path": "...", "contentB64": "...", "sha256": "<可选>",
                            "mkdirs": true（可选，父目录不存在时自动创建，本机/SSH 主机均支持）}
```

- 错误码语义：权限不足 **403** / 不存在 **404** / 用法错误（对目录 get、
  对文件 list）**400** / 超限 **413**；
- put 父目录不存在 → **404** `{"error": "父目录不存在：<目录>",
  "hint": "加 mkdirs:true 自动创建"}`（不带内部临时文件名）；传
  `mkdirs: true` 自动逐级创建后写入；
- `list` 的 path 接受 `~` 与相对路径，响应 path 是规范化绝对路径；每项带
  `type`（file/dir/symlink/fifo/…）、`mode`（八进制）、symlink 带跟随前的
  `target`，`dir`/`size` 跟随链接目标；
- `get` 整读上限 8MB，超限 413 响应带 `total`/`chunkLimit`，用
  `offset`/`length` 分块读完整个文件；
- `put` 严格校验 base64（坏编码 400 拒写，**原文件绝不会被清空**）；可选
  `sha256` 校验；写临时文件后原子 rename，覆盖时保留原文件权限；响应回显
  `sha256`。

## 持久会话（cd / export / venv 激活都延续）

```text
POST   /api/agent/ssh/sessions              {"host": "...", "cwd": "...", "shell": "bash"} → {"sessionId"}
POST   /api/agent/ssh/sessions/<sid>        {"command": "...", "timeoutSec": 30}
DELETE /api/agent/ssh/sessions/<sid>
```

- 开会话时 `cwd` 不存在 → 直接 **400**（带回远端原始报错），不再
  "创建成功、执行全 500"；
- 命令在隔离环境执行：语法错误（未闭合引号、坏 heredoc）只影响本条
  ——`exit: 2` + bash 原始报错，**会话永不被语法错误卡死**；
- `read x` / `cat` 等 stdin 读取者直接见 EOF，不会吞掉协议标记或挂死；
- 每条命令唯一哨兵；超时如实返回 `timedOut: true, exit: null,
  reason: "timeout"`，会话进入 pending：
  - 旧命令随后落地 → 下次调用自动恢复（旧输出整段丢弃，绝不串进新命令）；
  - 旧命令仍在跑 → **409 busy**，稍后重试或 DELETE 会话重建；
- `exit 7` → `{exit: 7, alive: false, reason: "shell exited"}`；此后调用
  **410 JSON**；死会话自动从列表回收；
- 响应 `cwd` 始终是**当前**目录（哨兵带回 `$PWD`）。

## 任务 API（委托智能体）

把目标交给 Deskport 托管的 Kimi 会话（本机 / agent node / 联邦成员）执行，
全程结构化事件回流，权限可审批——「智能体指挥智能体」的正道。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/tasks | 任务列表 + `running`/`queued` 计数与 `limit`（全局口径） |
| POST | /api/tasks | 建任务 → 202 `{prompt, engine, node, cwd, mode, model, paths, attachments}` |
| GET | /api/tasks/<id> | 任务状态（含 `model`、`workspace`、`remoteCwd`） |
| GET | /api/tasks/<id>/events?after=N | 增量事件（`since` 等价接受） |
| GET | /api/tasks/<id>/events/stream?since=N | SSE 推送 |
| POST | /api/tasks/<id>/message | 续聊 `{text}`（沿用原模型） |
| POST | /api/tasks/<id>/permission | 权限决策 `{requestId, optionId, deny}` |
| POST | /api/tasks/<id>/mode | 切换批准模式 |
| POST | /api/tasks/<id>/cancel · /close | 停止当前轮 / 结束会话（均返回 `status`） |
| GET | /api/tasks/nodes | 执行点列表，含 `models`/`defaultModel`/`modelSelectable`/`modelStatus`/`usableModels` |
| POST | /api/tasks/nodes/<node>/probe | 手动触发模型可用性探测 `{models?: [...]}`（缺省=全部声明模型），立即入队后台探测 |
| POST | /api/tasks/files | 上传文件 → 返回本机路径（塞进 paths 用） |
| GET | /api/tasks/kimi-sessions?cwd= | 已存在的 Kimi CLI 会话（接手候选，按最近更新排序） |

- `engine: "acp"`（缺省）= 本机 Kimi（agent node 托管，web 重启可接手）；
  `paths` 引用本机输入文件；**不接受 `cwd`**（400，任务固定运行在独立
  工作区）；`prompt` 上限 **4000 字符**，超限报错会写明数字；
- `engine: "remote"` 必须给 `node`（执行点）与 `cwd`（远端已存在目录），
  缺任一 → 400 并附 `nodes` 提示；
- `mode`：不传 = `default`（**逐项审批**，安全默认）；要自动批准必须显式
  `auto`；另有 `plan` / `yolo`；
- `model`（可选）：给执行点选模型（§7）。`GET /api/tasks/nodes` 里每个执行点
  带 `models`（可选列表）/ `defaultModel` / `modelSelectable`；清单三级来源：
  config 显式声明 → node /health 自动上报（节点侧解析 kimi config.toml，
  节点升级即自动点亮）→ 本机自动发现。声明白名单的执行点校验不命中 →
  400 附可选列表；选错别名会在任务 error 里看到 kimi 原始报错；
  联邦成员转发该字段（对端与本中枢保持同版，见下方版本策略）；
- **模型可用性（R4-10）**：`GET /api/tasks/nodes` 每个执行点另带
  `modelStatus`（`{<model>: {usable: true|false|null, checkedAt, error}}`，
  null=还没探测）与 `usableModels`（只列 usable=true）。探测 = 一次最小
  调用（`kimi -p ok --model`，30s 超时），node 侧后台异步跑（并发 ≤2），
  结果缓存 24h；默认只测 defaultModel + 近 7 天用过的，全量走
  `POST /api/tasks/nodes/<node>/probe` 手动触发。建任务时**指定的** model
  已知 usable=false → 400 附 `usableModels`；没探测过的照常放行；
- **敏感值不进事件流（R5-2）**：`tool` / `tool_result` / `assistant` /
  `permission_request` 事件（含 /log 的 jsonl 流水与 full 全文）写入前，
  中枢已知的明文令牌（remotes[].token、agentNodes[].token、federationToken、
  password、decisionApiKey、本机 node token-file；<12 字符不纳入）替换为
  `<redacted:来源>`，config.json 热改即时生效。`exec` / `sftp` / `get` 响应
  **不脱敏**（主控显式取数据）。policy 内建 deny：命令或文件工具路径命中
  `config.json` / 令牌文件（`*.agent-token`、`agent_node_token`、
  `deskport-agent-node/token`）→ 一律 ask，`allowCommands` 压不掉；
- **接手已有 Kimi 会话**：`{"kimiSessionId": "session_<uuid>"}`（仅
  engine=acp）。默认先 `kimi fork` 复制副本再加载——不干扰用户开着的
  终端；会话被占用时 400 透传 kimi 原始原因。`"fork": false` 直接接管，
  但会话已被其他任务持有时 409。候选列表
  `GET /api/tasks/kimi-sessions?cwd=<工作目录>`；
- **close 回收语义**：`waiting`/`paused` 任务的会话进程保留（45 分钟空闲
  回收，随时可续聊）；`close` 是明确结束——node/联邦侧的 ACP 进程立即回收；
- **文件投递**（智能体够不到 Deskport 文件系统时的过渡通道）：
  `POST /api/tasks/files {"name": "报告.md", "contentB64": "..."}` →
  `{"path": "E:\\...\\报告.md"}`，把该 path 放进 `paths`；或建任务时直接带
  `attachments: [{name, contentB64}]`（单附件 32MB / 总量 500MB / ≤20 项）；
- `permission_request` 事件带 `callId` 与 `args`（工具调用的参数原文），
  能精确对回是哪个调用在等审批；ACP 不带 toolCallId 时回退最近一次
  tool_call；
- `close` 返回的就是终态（已 cancel 的任务保持 `cancelled`，close 幂等）。

**任务事件类型**：`status` / `user` / `assistant`（回答终版，逐 chunk 流水在
`/log`）/ `tool` / `tool_result` / `plan` / `permission_request` /
`permission_result` / `error`。`status: waiting` = 本轮完成。

**收事件（推荐：阻塞 wait，一次请求等到需要你决策为止）**：

```text
GET /api/tasks/<id>/wait?after=<上次最大seq>&timeout=300   （默认300，上限600）
→ {"status": "waiting|failed|permission|running|...", "finalText": "回答全文",
   "pending": {"requestId","callId","name","args","options"} 或 null,
   "stats": {"events":n, "tools":n, "autoApproved":n, "errors":n},
   "lastSeq": n, "error": "..."}
```

- `pending` 非空 = 等你审批（policy 自动放行的不算，数量见
  `stats.autoApproved`）；`status: waiting/failed/closed/cancelled` = 回合落定，
  `finalText` 是 after 之后最后一条回答全文（不截断）；
- 超时返回 `status: running`——带上 `lastSeq` 再调一次即可；断线/换端/重启
  后续接也一样：只保存 lastSeq，`wait?after=lastSeq` 不丢、不重；
- `POST /api/tasks`、`/message`、`/permission` 都支持 `"wait": <秒>`，
  发完直接按同样语义阻塞返回，省一次往返；
- 实现：服务端条件变量驱动（Flask threaded 模式，每个等待占一个工作线程，
  无忙等）；SSE 与轮询端点保留作备用。

### 压缩执行方上下文

给任务发一条 `/compact` 消息（POST /api/tasks/<id>/message {"text": "/compact"}）
会触发 Kimi 压缩会话上下文（2026-11-10 实测可用）。什么时候该用：

- 任务详情 `context.used / context.max > 70%` 时（上下文快满，回答质量会掉）；
- 一批活结束、开始下一批之前（带着干净上下文开工）。

压缩是异步的：/compact 发出后等 status=waiting 且 lastText 出现压缩摘要即可继续。

## 重启语义（如实版）

## 重启语义（如实版）

- 交互式终端/SSH 会话：守护进程托管，web 重启**不打断**；
- agent 任务会话：本机 node / 远端 node 托管的会话在 web 重启后可
  `message` 接手（`canResume`）；仅 web 内 stdio 回退（未启用 node）时任务
  中断（`interrupted`），重发 message 会明确 409，请重新建任务；
- 重启进行中事件轮询会拿到 frp 的 502 原文——服务不可用期间任何服务端都
  无法返回 JSON，恢复后一切照旧。

## 技能分发（URL 自举）

```text
GET /api/agent/skills            → {skills: [{name, size, mtime}]}
GET /api/agent/skills/<name>     → {name, content}
```

新智能体入网（有 token 即可自举）：

```bash
mkdir -p ~/.kimi-code/skills/deskport
curl -s -H "X-Agent-Token: $TOKEN" http://<中枢>/api/agent/skills/deskport \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])" \
  > ~/.kimi-code/skills/deskport/SKILL.md
```

Deskport 升级后重新拉取即更新（mtime 检测变更）。

## 审计

每次调用写 `events` 流（`agent.exec` / `agent.sftp.*` / `agent.session.*` /
`agent.task.create` / `agent.task.upload`），控制台审计可回看：哪个令牌、
哪台主机、执行了什么。

## 已知边界

- exec 是非 pty 通道：无交互式程序（vim/top），需要交互请用浏览器终端；
  会话里 stdin 是 EOF，要喂数据用 heredoc 或 sftp/put；
- **版本策略：API 不做跨版本兼容**。联邦/节点各部署面与本中枢保持同版
  （升级时一并升），新字段直接可用，不为旧版保留别名或降级路径；
- agent node 需要 Python ≥ 3.11（模型自动发现依赖 tomllib），入口有
  fail-fast 守卫；建议用 uv 跑 3.13；
- 外部调用方 Python 封装参考：`deskport.py`（仅标准库）已按本 API 契约
  处理 NUL 拒发、`after` 增量去重等；服务端修复后这些规避仍兼容。

## 技能封装（给智能体用）

仓库内 `.agents/skills/deskport/SKILL.md` 是本 API + 任务 API 的智能体技能
封装，链入你的 agents 技能目录即可获得全部能力。
