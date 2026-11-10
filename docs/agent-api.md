# 智能体 API（Agent API）

给外部 AI 程序的机器接口：用长效令牌直接在已保存的 SSH 主机上**非交互执行命令**
和读写文件。与浏览器终端互补——人用 WS 会话，智能体用这套 HTTP API。

安全模型：**外部智能体 = 可信代码**。令牌等价于在受控主机上执行任意命令的能力，
请只签发给自己的程序，并妥善保管。

## 签发令牌

1. 生成随机令牌并计算哈希：

```bash
TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
echo "TOKEN=$TOKEN"
python -c "import hashlib; print('tokenHash =', hashlib.sha256('$TOKEN'.encode()).hexdigest())"
```

2. 把 `tokenHash` 写进 `config.json`（令牌明文自己保存，仓库与日志里都不落）：

```json
"agentTokens": [
  { "id": "kimi", "name": "Kimi Code", "tokenHash": "<上一步的 SHA-256>" }
]
```

3. 重启 Deskport 生效；撤销 = 删除对应条目再重启。

## 认证

每次请求带 `X-Agent-Token: <令牌>` 或 `Authorization: Bearer <令牌>`。
未带/不匹配 → `401 {"error": "unauthorized (需要 X-Agent-Token)"}`。
浏览器 Cookie 登录态同样放行（面板 UI 复用同一组端点）。

## 端点

### GET /api/agent/hosts

主机列表（只有 id / name / host / port，不含凭据）。

### POST /api/agent/ssh/exec

```json
{ "host": "ai-xmyz", "command": "uptime", "timeoutSec": 30 }
```

- `host`：主机的 id 或名称（见 /api/agent/hosts）；
- `timeoutSec`：1–300，缺省 30；超时会掐断通道并返回已收到的输出与
  `timedOut: true`（远端进程不保证被终止）；
- 响应：`{ "exitCode": 0|null, "stdout": "...", "stderr": "...",
  "timedOut": false, "truncated": false, "durationMs": 123 }`。

### POST /api/agent/sftp/list · GET/POST /api/agent/sftp/get · POST /api/agent/sftp/put

- `list`：`{ "host": "...", "path": "/srv" }` → `{ entries: [{name,size,mtime,dir}] }`
- `get`：`{ "host": "...", "path": "..." }` → base64 内容（上限 8MB）
- `put`：`{ "host": "...", "path": "...", "contentB64": "..." }`（上限 8MB，覆盖写）

### GET /api/agent/ssh/sessions · POST /api/agent/ssh/sessions（持久会话）

- `POST { "host": "...", "cwd": "/path" }` → `{ sessionId }`；同会话 cd/export 延续；
- `POST /api/agent/ssh/sessions/<sid> { "command": "...", "timeoutSec": 30 }` 执行；
  `DELETE` 关闭。见 `modules/agent_sessions.py`。

## 任务 API（委托智能体）

把目标交给 Deskport 托管的 Kimi 会话（本机或远端节点）执行，全程结构化事件回流，
权限可审批——这是「智能体指挥智能体」的正道，同样走 `X-Agent-Token` 认证。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/tasks | 任务列表（`running` 上限 2） |
| POST | /api/tasks | 建任务 → 202；`{prompt, engine, node, cwd, mode, paths}` |
| GET | /api/tasks/<id> | 任务状态（status/lastText/error/pendingPermission） |
| GET | /api/tasks/<id>/events?after=N | 增量事件（轮询备用） |
| GET | /api/tasks/<id>/events/stream?since=N | SSE 推送（外部智能体实时观察） |
| POST | /api/tasks/<id>/message | 续聊 `{text}`（waiting/paused 时自动接续上下文） |
| POST | /api/tasks/<id>/permission | 权限决策 `{requestId, optionId, deny}` |
| POST | /api/tasks/<id>/mode | 切换批准模式 `{mode}` |
| POST | /api/tasks/<id>/cancel · /close | 停止当前轮 / 结束会话 |

- `engine: "acp"` = 本机 Kimi（由本机 agent node 托管，web 重启任务无感存活；
  `paths` 可带本机输入文件，复制到任务工作区）；
- `engine: "remote"` = 远端节点，需 `node`（`GET /api/tasks/nodes` 列表）与 `cwd`（远端目录）；
- `mode`：`auto` / `default`（逐项审批，`permission_request` 事件等决策）/
  `plan` / `yolo`；
- 事件类型见 `docs` 内任务页文档；`assistant` 即 Kimi 的回答文本，
  `status: waiting` = 本轮完成，`failed` 时看 `error`；
- 令牌来源的任务会写审计 `agent.task.create`（独立于任务卡事件）。

## 技能分发（URL 自举）

技能文档由中枢统一分发，外部智能体一条命令即可完成安装/更新：

```text
GET /api/agent/skills            → {skills: [{name, size, mtime}]}
GET /api/agent/skills/<name>     → {name, content}   （SKILL.md 原文）
```

新智能体入网（有 token 即可自举）：

```bash
mkdir -p ~/.kimi-code/skills/deskport
curl -s -H "X-Agent-Token: $TOKEN"   http://<中枢>/api/agent/skills/deskport   | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])"   > ~/.kimi-code/skills/deskport/SKILL.md
```

Deskport 升级后重新拉取即更新到最新版（mtime 可用于检测变更）。

## 审计

每次调用写 `events` 流（`agent.exec` / `agent.sftp.list|get|put`），
可在控制台审计里回看：哪个令牌、哪台主机、执行了什么。

## 已知边界

- exec 是非 pty 通道：无交互式程序（vim/top），需要交互请用浏览器终端；
- 超时掐断只关闭通道，远端进程是否残留取决于命令本身；
- 令牌即远程执行权：泄露请立即撤销（删除 config 条目并重启）。


## 技能封装（给智能体用）

仓库内 `.agents/skills/deskport/SKILL.md` 是本 API + 任务 API 的智能体技能封装，
把它链入你自己的 agents 技能目录（如 `~/.agents/skills/`），智能体即可自动
获得「远程执行 / SFTP 读写 / 任务派活给其他智能体」的能力。令牌文件路径由
部署约定（建议放部署目录旁并加入忽略清单），技能文件里不落明文。
