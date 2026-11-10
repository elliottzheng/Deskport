---
name: deskport
description: 通过 Deskport 智能体 API 操作已保存的远程主机（执行命令、读写文件、持久会话），以及把复杂任务派活给其他智能体（Deskport 本机或远端节点上的 Kimi 会话，全程结构化事件回流、权限可审批）。当任务需要远程执行、读写远程文件，或需要委托另一个智能体完成多步复杂任务时使用。认证用长效令牌（部署时签发），令牌等价于远程执行权，勿外传。
---

# deskport

通过 Deskport 的智能体 API 操作远程主机、并把复杂任务委托给其他智能体。所有请求带 `X-Agent-Token` 头。

## 速查

- API base: 部署 Deskport 的地址（本机部署为 `http://127.0.0.1:6789`；外网走部署者配的 HTTPS 隧道地址）
- 认证: `X-Agent-Token: <令牌>`；令牌由部署者在 `config.json` 的 `agentTokens[]` 签发（存 SHA-256 指纹，明文只给智能体一次）
- 主机: 用名称或 id；`GET /api/agent/hosts` 可列出（只含 id/name/host/port，无凭据）
- 智能体节点: `GET /api/tasks/nodes` 可列出（本机节点名 `local`，远端节点如 `ai-xmyz`）

## 快命令（无状态，单发）

```text
POST /api/agent/ssh/exec
{"host": "<主机>", "command": "uptime", "timeoutSec": 30, "cwd": "/path"}
→ {"exit": 0, "out": "...", "err": "...", "timedOut": false}
```

- exec 无状态：cd 不延续。多步任务用 `cwd` 参数（每条都带），或用持久会话 / 任务派活（见下）。
- 输出上限 2MB；超时上限 600s；超时只关闭通道，长命令后台化再轮询日志。

## 持久会话（cd / export / venv 激活都延续）

```text
POST   /api/agent/ssh/sessions              {"host": "<主机>", "cwd": "/path"} → {"sessionId"}
POST   /api/agent/ssh/sessions/<sid>        {"command": "...", "timeoutSec": 30}
DELETE /api/agent/ssh/sessions/<sid>
```

- 同一会话里 cd / export / source 都延续（单 bash 进程常驻）。
- 响应字段：`exit`、`out`（末尾 8KB）、`err`、`cwd`、`alive`；`combined: true` 并入 stderr，`full: true` 全量输出。
- 命令卡死超时会话可能残留状态，建议 close 重开。

## SFTP（文件读写，8MB 上限）

```text
POST /api/agent/sftp/list  {"host": "<主机>", "path": "/path"} → {entries: [...]}
POST /api/agent/sftp/get   {"host": "...", "path": "..."}     → {contentB64}
POST /api/agent/sftp/put   {"host": "...", "path": "...", "contentB64": "..."}
```

## 任务派活（委托另一个智能体，多步复杂任务推荐）

把一句目标交给 Deskport 托管的 Kimi 会话执行：它自己规划、调工具、回报结果；
危险操作会停下等你审批（见权限）。人也能在任务卡上随时插话或接手。

```text
POST /api/tasks
{"prompt": "目标描述", "engine": "acp", "mode": "auto"}
→ 202 {"task": {"id": "...", ...}}
```

- `engine: "acp"` = Deskport 本机的 Kimi（workspace 为本机临时目录，输出文件会列在任务卡）
- `engine: "remote"` = 远端节点执行，需加 `"node": "<节点名>", "cwd": "<远端目录>"`；节点列表 `GET /api/tasks/nodes`
- `mode`: `auto`（自动批准，推荐）/ `default`（逐项审批）/ `plan` / `yolo`
- `paths`（仅 acp）：本机输入文件路径数组，会复制到任务工作区（只读原件）

**收事件**（SSE 推送，挂一条连接即可；JSON 轮询端点保留备用）：

```text
GET /api/tasks/<id>/events/stream?since=<上次最大seq>
→ data: {seq, type, text, name, options, ...} 逐条推送；18s 心跳；closed 后 end
GET /api/tasks/<id>/events?after=<上次最大seq>    → {events: [...]}（轮询备用）
```

事件类型：`status`（状态流转）、`user`（输入）、`assistant`（Kimi 的回答文本）、
`tool` / `tool_result`（工具调用与结果）、`plan`（计划更新）、
`permission_request`（等待你审批，`options` 里选一个 `optionId`）、`error`。

**关键操作**：

```text
GET  /api/tasks/<id>                          → 任务状态（status/lastText/error）
GET  /api/tasks/<id>/log                      → 完整 jsonl 流水
POST /api/tasks/<id>/message    {"text": "..."}   → 续聊/追问（回合间）
POST /api/tasks/<id>/permission {"requestId": "...", "optionId": "...", "deny": false} → 审批
POST /api/tasks/<id>/cancel                     → 停止当前轮
POST /api/tasks/<id>/close                      → 结束会话
```

- 任务结束判定：`status: waiting`（这轮完成，等下一句）或 `failed`（看 `error`）。
- 会话可接手：`status: paused` 或 waiting 时 `message` 续聊会自动接续原上下文；
  web 重启不打断会话（由 agent node 托管）。
- 并发上限 2 个 running 任务；创建 202 表示受理，不是完成。

## 规矩

- 命令与任务都真实执行：优先只读；写操作（写文件、装软件、动服务）先向用户说明再执行。
- 主机/节点名写错时响应带提示（`availableHosts` / `nodes`），直接改用，不用再列。
- 401 = 令牌失效：检查令牌与 `config.json` 的 `agentTokens` 指纹是否匹配。
- 审计全留痕（agent.exec / agent.sftp.* / agent.task.create），部署者可在控制台回看。
- 完整接口参考：仓库内 `docs/agent-api.md`；agent node 侧接口见 `agent_node/README.md`。
