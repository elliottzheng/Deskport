---
name: deskport
description: 通过 Deskport 智能体 API 操作已保存的远程主机和 Deskport 本机（执行命令、读写文件、持久会话），以及把复杂任务派活给其他智能体（可选模型，全程结构化事件回流、权限可审批）。当任务需要远程/本机执行、读写文件，或需要委托另一个智能体完成多步复杂任务时使用。认证用长效令牌（部署时签发），令牌等价于远程执行权，勿外传。
---

# deskport

通过 Deskport 的智能体 API 操作主机（SSH 主机 + Deskport 本机 `host:"local"`）、
并把复杂任务委托给其他智能体。所有请求带 `X-Agent-Token` 头。

## 速查

- API base: 部署 Deskport 的地址（本机部署为 `http://127.0.0.1:6789`；外网走部署者配的 HTTPS 隧道地址）
- 认证: `X-Agent-Token: <令牌>`；每个智能体应单独签发令牌（审计按令牌名留痕）
- 主机: `GET /api/agent/hosts` 列出（SSH 主机 + 本机 `local`，本机带 os/defaultShell/shells）
- 执行点: `GET /api/tasks/nodes` 列出（本机节点 `local`、远端节点如 `ai-xmyz`、联邦成员），每个带 `models`/`defaultModel`/`modelSelectable`（远端节点升级后自动上报）
- 错误全是 JSON：404/405/500 带 `error`/`code`（500 另有 `requestId`）

## 这个包里还有什么（一起拉）

技能包不只有这份手册——同一个目录里还有能直接用的东西：

| 文件 | 用途 |
|---|---|
| `deskport.py` | Python 客户端（单文件、零依赖），也能当 CLI：`python deskport.py run "uptime"` |
| `driver.py` | 驱动模板：别名目录、边界判定、审批自动放行、409 排队；边界写在 `driver.json` |
| `COLLABORATION.md` | 协作原则：主控与执行方怎么分工、派活、验收（真去指挥别的智能体前读一遍） |

自举：**一条链接 + 一个令牌就够了**——整包是个 zip，解开就是技能目录，
里面的 `.deskport.json` 已经填好本次下载用的地址（令牌单独给，不在包里）：

```bash
D=<中枢地址>; T=<令牌>
curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/pack.zip" -o pack.zip
unzip -oq pack.zip -d ~/.kimi-code/skills/        # → ~/.kimi-code/skills/deskport/
export DESKPORT_TOKEN=$T                          # 或写进 deskport/.agent-token
python ~/.kimi-code/skills/deskport/deskport.py hosts   # 验一下
```

不想解压整包时，也可以单个文件拉（`?raw=1` 直接落盘，响应头 `X-Skill-Sha256` 校验）：

```bash
S=~/.kimi-code/skills/deskport; mkdir -p "$S"
curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport" | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])" > "$S/SKILL.md"
for f in deskport.py driver.py COLLABORATION.md; do
  curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/$f?raw=1" -o "$S/$f"
done
# 清单（含 files / pack / sha256）：GET /api/agent/skills
```

客户端与驱动只用标准库（Python ≥ 3.9）。驱动默认读自己目录的 `driver.json`，
`DRIVER_CONFIG` 可指到别处，别名目录在脚本目录的 `run/<别名>/` 下。

## 快命令（无状态，单发）

```text
POST /api/agent/ssh/exec
{"host": "<主机或local>", "command": "uptime", "timeoutSec": 30, "cwd": "/path"}
→ {"exit": 0, "out": "...", "err": "...", "timedOut": false, "reason": "", "pid": 4321}
```

- exec 无状态：cd 不延续。多步任务用 `cwd` 参数（每条都带），或用持久会话 / 任务派活（见下）。
- `timeoutSec` 1–600，越界 400；命令含 NUL 400；stdin 即时 EOF（读 stdin 的命令不挂死）。
- 输出上限 2MB；`"encoding": "base64"` 拿 `outB64`/`errB64` 原文字节。
- 超时：`exit:null` + `reason:"timeout"` + `pid`，远端进程组被整组击杀。
- 本机路径双写法（`/e/事项` = `E:\事项`）；本机可加 `"shell": "powershell"|"pwsh"`（默认 bash，退出码已统一）。

## 持久会话（cd / export / venv 激活都延续）

```text
POST   /api/agent/ssh/sessions              {"host": "...", "cwd": "..."} → {"sessionId"}
POST   /api/agent/ssh/sessions/<sid>        {"command": "...", "timeoutSec": 30}
DELETE /api/agent/ssh/sessions/<sid>
```

- `cwd` 不存在 → 开会话就 400（不会再"创建成功、执行全 500"）。
- 语法错误（未闭合引号等）只影响本条：`exit:2` + bash 原始报错，会话不会被卡死。
- `read x`/`cat` 直接见 EOF；响应 `cwd` 是当前目录（实时回报）。
- 超时 `timedOut:true, exit:null`；旧命令没跑完时下一条返回 409 busy（等它落地自动恢复）；`exit 7` → `exit:7, alive:false`，之后该会话 410。
- 本机会话只支持 bash；powershell/pwsh 用单发 exec。

## SFTP（文件读写）

```text
POST /api/agent/sftp/list  {"host": "...", "path": "/srv"} → {path, entries:[{name,path,size,mtime,dir,type,mode,target}]}
POST /api/agent/sftp/get   {"host": "...", "path": "...", "offset": 0, "length": 4096} → {contentB64, total}
POST /api/agent/sftp/put   {"host": "...", "path": "...", "contentB64": "...", "sha256": "可选"}
```

- `~` 与相对路径都解析为绝对路径；错误码：403 权限 / 404 不存在 / 400 用法（对目录 get 等）/ 413 超限。
- 整读上限 8MB，超限用 `offset`/`length` 分块（413 响应带 `total`）。
- put 严格校验 base64（坏编码 400 拒写，原文件不动）；可选 sha256 校验；原子写入。

## 任务派活（委托另一个智能体，多步复杂任务推荐）

```text
POST /api/tasks
{"prompt": "目标描述", "engine": "acp", "mode": "auto", "model": "<可选>"}
→ 202 {"task": {"id": "...", ...}}
```

- `mode` 不传 = `default`（逐项审批，安全默认）；要自动批准显式传 `"mode": "auto"`。
- `model` 可选：按活的性质挑模型（机械活用快的，验收用与实施方不同的模型）；
  可选列表看 `GET /api/tasks/nodes` 的 `models`；续聊沿用原模型。
- 接手已有 Kimi 会话：`GET /api/tasks/kimi-sessions?cwd=<目录>` 挑候选，
  `{"kimiSessionId": "<id>"}` 建任务（默认 fork 副本不干扰原会话，占用时 400 透传原因）。
- 任务 close = 明确结束（node 侧进程立即回收）；不 close 的会话保留 45 分钟可随时续聊。
- `engine: "remote"` 加 `"node": "...", "cwd": "<远端目录>"`；acp 不接受 cwd。
- `prompt` 上限 4000 字符；本机文件用 `paths: [绝对路径]`；没有现成文件时先上传：
  `POST /api/tasks/files {"name": "a.md", "contentB64": "..."}` → 返回 path 塞进 paths，
  或建任务时直接带 `attachments: [{name, contentB64}]`。

**收事件（推荐：阻塞 wait，一次请求等到需要你决策为止）**：

```text
GET /api/tasks/<id>/wait?after=<上次最大seq>&timeout=300   （默认300，上限600）
→ {"status": "waiting|failed|permission|running", "finalText": "回答全文",
   "pending": {"requestId","callId","name","args","options"} 或 null,
   "stats": {...autoApproved...}, "lastSeq": n}
```

- `pending` 非空 = 等审批；`waiting/failed` = 本轮结束（`finalText` 全文）；
  超时 `running`——带 `lastSeq` 再调（断线续接只存 lastSeq，不丢不重）。
- 派活/续聊/审批 POST 都支持 `"wait": <秒>`，发完直接阻塞返回。
- 备用：SSE `events/stream?since=` 与轮询 `events?after=`。

事件类型：`status` / `user` / `assistant`（Kimi 的回答终版）/ `tool` / `tool_result` /
`plan` / `permission_request`（**带 `callId` 与 `args`**，能对回具体调用）/ `error`。

**关键操作**：

```text
GET  /api/tasks/<id>                              → 状态（含 model/workspace/error）
GET  /api/tasks/<id>/log                          → 完整 jsonl 流水
POST /api/tasks/<id>/message    {"text": "..."}   → 续聊/追问（回合间）
POST /api/tasks/<id>/permission {"requestId": "...", "optionId": "...", "deny": false}
POST /api/tasks/<id>/cancel | /close              → 都返回 status；close 幂等
```

- 任务结束判定：`status: waiting`（这轮完成）或 `failed`（看 `error`）。
- 会话接手：node 托管的会话 web 重启后 `message` 可续（`canResume`）；
  stdio 回退（未启用 node）时中断会明确 409，请重建任务。
- 并发上限 `taskMaxRunning` 可配置（config.json，缺省 8，0=不限，改完热
  生效无需重启）；超限 429 响应带 `{limit, running, holders}`（谁占着名额）；
  202 是受理不是完成。

## 规矩

- 命令与任务都真实执行：优先只读；写操作（写文件、装软件、动服务）先向用户说明再执行。
- 名字写错时响应带提示（`availableHosts` / `nodes` / `models`），直接改用，不用再列。
- 401 = 令牌失效：检查令牌与 `config.json` 的 `agentTokens` 指纹是否匹配。
- 审计全留痕（agent.exec / agent.sftp.* / agent.session.* / agent.task.*），部署者可在控制台回看。
- 本技能由中枢分发，可自举更新：手册与三个附件走同一套 URL（见上节），升级后重拉即同步；`X-Skill-Sha256` 用来判断要不要更新本地副本。
- 完整接口参考：仓库内 `docs/agent-api.md`；agent node 侧接口见 `agent_node/README.md`。
