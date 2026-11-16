---
name: deskport
description: 通过 Deskport 智能体 API 操作已保存的远程主机和 Deskport 本机（执行命令、读写文件、持久会话），以及把复杂任务派活给其他智能体（可选模型，全程结构化事件回流、权限可审批）。当任务需要远程/本机执行、读写文件，或需要委托另一个智能体完成多步复杂任务时使用。认证用长效令牌（部署时签发），令牌等价于远程执行权，勿外传。
---

# deskport

通过 Deskport 的智能体 API 操作主机（SSH 主机 + Deskport 本机 `node:"local"`）、
并把复杂任务委托给其他智能体。所有请求带 `X-Agent-Token` 头。

## 速查

- API base: `{{BASE}}`（按本次下载渲染的真实入口：内网 http 直连，或部署者配的 HTTPS 隧道地址）
- 技能版本: `{{VERSION}}`
- 认证: `X-Agent-Token: <令牌>`；每个智能体应单独签发令牌（审计按令牌名留痕）
- 主机: `GET /api/agent/hosts` 列出（SSH 主机 + 本机 `local`，本机带 os/defaultShell/shells）
- 执行点: `GET /api/tasks/nodes` 列出（本机节点 `local`、远端节点如 `ai-xmyz`、联邦成员），每个带 `models`/`defaultModel`/`modelSelectable`（远端节点升级后自动上报）
- 错误全是 JSON：404/405/500 带 `error`/`code`（500 另有 `requestId`）
- **机器参数用 `node`**（exec/sftp/会话与任务同一套词汇，缺省 `local`）：`host` 是旧名，仍接受；两个都给且不同 → 400
- **提反馈**：`POST /api/feedback`（令牌通道，实名留痕，详见专节）——撞见平台 bug 时用，别只在报告里带一句
- **平级协调**：`GET /api/agent/registry` 查谁在哪台节点；`POST /api/agent/message` 给另一个智能体发消息（占资源前先问，详见专节）
- **派开发类活走工作项**：`deskport.py wi new …`（建 worktree→实施→验收→返工→集成全自动）；直接建任务只用于调查类
- **技能版本**：`deskport.py` 自带版本常量（装配技能包时盖成本次构建号），每个请求自动带上；响应带回线上版本号，对不上时附 `X-Deskport-Skill-Stale: 1`——driver.py 每轮 DONE 行提醒重拉 skill.zip（旧脚本不用等撞上 400 才知道落后，你也无需手动配任何东西）

## 这个包里还有什么（一起拉）

技能包不只有这份手册——同一个目录里还有能直接用的东西：

| 文件 | 用途 |
|---|---|
| `deskport.py` | Python 客户端（单文件、零依赖），也能当 CLI：`python deskport.py run "uptime"` |
| `driver.py` | 驱动模板：别名目录、边界判定、审批自动放行、409 排队；边界写在 `driver.json`（字段见同目录 `driver.example.json`） |
| `driver.example.json` | `driver.json` 的最小样例（带 `_说明`）：驱动首次运行前照它抄一份 |
| `COLLABORATION.md` | 协作原则：主控与执行方怎么分工、派活、验收（真去指挥别的智能体前读一遍） |

> **必读**：`COLLABORATION.md`（主控与执行方怎么分工、怎么验收、什么时候停下问人）。
> 如果你要驱动别的智能体干多步活，先读它再动手。

自举：**一条链接就够了**——整包是个 zip（公开，无需令牌），解开就是技能目录，
里面的 `.deskport.json` 已经填好本次下载用的地址（令牌单独给，不在包里）：

```bash
D={{BASE}}
curl -s -k "$D/api/agent/skill.zip" -o deskport-skill.zip   # 公网入口是隧道方的自签证书：不加 -k 会 exit 60；内网 http 直连不需要 -k
unzip -oq deskport-skill.zip -d ./deskport-skill/    # → ./deskport-skill/deskport/
export DESKPORT_TOKEN=<部署者发你的令牌>              # 或写进 ./deskport-skill/deskport/.agent-token
python ./deskport-skill/deskport/deskport.py hosts   # 验一下
```

如果你是 Kimi Code，也可解到技能目录：`unzip -oq deskport-skill.zip -d ~/.kimi-code/skills/`（→ `~/.kimi-code/skills/deskport/`）。

有令牌时也可以单个文件拉（`?raw=1` 直接落盘，响应头 `X-Skill-Sha256` 校验；清单含 `files`/`pack`/`sha256`，见 `GET /api/agent/skills`）：

```bash
S=./deskport-skill/deskport; mkdir -p "$S"
T=<部署者发你的令牌>
curl -s -k -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport" | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])" > "$S/SKILL.md"
for f in deskport.py driver.py driver.example.json COLLABORATION.md; do
  curl -s -k -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/$f?raw=1" -o "$S/$f"
done
```

证书（只影响公网入口）：隧道方用的是自签证书，`curl` 不加 `-k` 会直接 exit 60；
`deskport.py` 不开校验绕过则会抛 `CERTIFICATE_VERIFY_FAILED`。公网用时把
`"insecure": true` 写进 `.deskport.json`，或 `connect(cafile=<部署者给你的证书文件>)`
（只信任这一张，比全局跳过校验更稳；CLI 等价于 `-k`）；内网 http 直连没有这个问题。
只在确认连的就是自己的 Deskport 时才跳过校验——对别的地址别顺手 `-k`。

客户端与驱动只用标准库（Python ≥ 3.9）。驱动默认读自己目录的 `driver.json`（必填
root/danger/safe，缺文件或缺键会一句话报错并指向同目录 `driver.example.json`），
`DRIVER_CONFIG` 可指到别处，别名目录在脚本目录的 `run/<别名>/` 下。

## 快命令（无状态，单发）

```text
POST /api/agent/ssh/exec
{"node": "<主机或local>", "command": "uptime", "timeoutSec": 30, "cwd": "/path"}
→ {"exit": 0, "out": "...", "err": "...", "timedOut": false, "reason": "", "pid": 4321}
```

- `node` 指机器（缺省 `local`）：`local` 是本机，其余取 `GET /api/agent/hosts`；
  `host` 是旧名，仍接受，但请用 `node`（两个都给且不同 → 400）。
- exec 无状态：cd 不延续。多步任务用 `cwd` 参数（每条都带），或用持久会话 / 任务派活（见下）。
- `timeoutSec` 1–600，越界 400；命令含 NUL 400；stdin 即时 EOF（读 stdin 的命令不挂死）。
- 输出上限 2MB；`"encoding": "base64"` 拿 `outB64`/`errB64` 原文字节。
- 超时：`exit:null` + `reason:"timeout"` + `pid`，远端进程组被整组击杀。
- **长命令（>600s）**：单条上限 600s 是硬上限。受支持模式 = `nohup cmd > log 2>&1 & echo $!` 起后台，再用普通 exec 轮询（`kill -0 <pid>` / `tail log`）。**别用工具自带的 `run_in_background`**：配了 policy 会被直接拒（"后台任务被禁用"），没配时审批可能晚到轮后、被当幽灵审批自动拒（轮末统计 `server-denied` 能看见次数）。
- 本机路径双写法（`/e/事项` = `E:\事项`）；本机可加 `"shell": "powershell"|"pwsh"`（默认 bash，退出码已统一）。

## 持久会话（cd / export / venv 激活都延续）

```text
POST   /api/agent/ssh/sessions              {"node": "...", "cwd": "..."} → {"sessionId"}
POST   /api/agent/ssh/sessions/<sid>        {"command": "...", "timeoutSec": 30}
DELETE /api/agent/ssh/sessions/<sid>
```

- 开会话用 `node` 指机器（缺省 `local`）；`host` 是旧名，仍接受。

- `cwd` 不存在 → 开会话就 400（不会再"创建成功、执行全 500"）。
- 语法错误（未闭合引号等）只影响本条：`exit:2` + bash 原始报错，会话不会被卡死。
- `read x`/`cat` 直接见 EOF；响应 `cwd` 是当前目录（实时回报）。
- 超时 `timedOut:true, exit:null`；旧命令没跑完时下一条返回 409 busy（等它落地自动恢复）；`exit 7` → `exit:7, alive:false`，之后该会话 410。
- 本机会话只支持 bash；powershell/pwsh 用单发 exec。

## SFTP（文件读写）

```text
POST /api/agent/sftp/list  {"node": "...", "path": "/srv"} → {path, entries:[{name,path,size,mtime,dir,type,mode,target}]}
POST /api/agent/sftp/get   {"node": "...", "path": "...", "offset": 0, "length": 4096} → {contentB64, total}
POST /api/agent/sftp/put   {"node": "...", "path": "...", "contentB64": "...", "sha256": "可选"}
```

- 指代机器用 `node`（缺省 `local`）；`host` 是旧名，仍接受。
- `~` 与相对路径都解析为绝对路径；错误码：403 权限 / 404 不存在 / 400 用法（对目录 get 等）/ 413 超限。
- 整读上限 8MB，超限用 `offset`/`length` 分块（413 响应带 `total`）。
- put 严格校验 base64（坏编码 400 拒写，原文件不动）；可选 sha256 校验；原子写入。

## 任务派活（委托另一个智能体，多步复杂任务推荐）

```text
POST /api/tasks
{"prompt": "目标描述", "node": "local", "mode": "auto", "model": "<可选>"}
→ 202 {"task": {"id": "...", ...}}
```

- `node` = 执行点（去哪台机器）：缺省 `"local"`（本机）；其它值见
  `GET /api/tasks/nodes`（agent node / 联邦成员）。`cwd` = 执行目录：
  缺省 = Deskport 分配的任务目录。
- 旧字段（引擎选择）已移除：传了直接 400，不会被忽略或映射。
- `mode` 不传 = `default`（逐项审批，安全默认）；要自动批准显式传 `"mode": "auto"`。
  **`mode=auto/yolo` 与 `policy.denyCommands` 矛盾**：这两个模式下执行方自行批准一切，
  hub 的策略漏斗收不到调用，denyCommands 被静默作废（2026-11-14 实测复现）——
  两个一起传直接 400。要"外推前必须停下审批"就用 `default`。
- `model` 可选：按活的性质挑模型（机械活用快的，验收用与实施方不同的模型）；
  可选列表看 `GET /api/tasks/nodes` 的 `models`；续聊沿用原模型。
- 接手已有 Kimi 会话：`GET /api/tasks/kimi-sessions?cwd=<目录>` 挑候选，
  `{"kimiSessionId": "<id>"}` 建任务（默认 fork 副本不干扰原会话，占用时 400 透传原因）。
- CLI 一行版：`python deskport.py task "<描述>" --session <会话编号>` = 直接接管原会话
  （`kimiSessionId` + `fork=false`，不用副本）——返工/复验复用同一个执行方就用它。
- 任务 close = 明确结束（node 侧进程立即回收）；不 close 的会话保留 45 分钟可随时续聊。
- 非本机执行点（`"node": "<agent node 或联邦成员>"`）必须给 `"cwd": "<该机器上的目录>"`，
  缺 cwd 或执行点不存在 → 400 并附 `nodes` 提示；目录由对端校验。
- 本机也可给 `"cwd": "<本机绝对目录>"`：执行进程在该目录里跑，可以直接改项目；
  **任务目录（workspace）仍由 Deskport 建**——日志、`inputs/`、close 都只动
  workspace，cwd 里的文件不会被清。cwd 必须存在、是目录，且在
  `config.workDirs` / `fallbackDir` 之下（越界 400）；不传 = 跑在 workspace。
  附件仍复制到 `<workspace>/inputs/`；cwd 与 workspace 不同时，prompt 会自动
  写明「工作目录是 cwd、附件在哪个绝对路径」。
- `prompt` 上限 4000 字符；本机文件用 `paths: [绝对路径]`；没有现成文件时先上传：
  `POST /api/tasks/files {"name": "a.md", "contentB64": "..."}` → 返回 path 塞进 paths，
  或建任务时直接带 `attachments: [{name, contentB64}]`。
- `title` 可选（≤40 字）：**主管派活建议显式给**（如 `"title": "批次 T：任务标题"`）——不给就按
  提示词首行自动生成（剥【目标】类标记、取第一句、截 24 字）；改名走 `PATCH /api/tasks/<id>`。
- `expects` 可选（`"impl"` / `"verify"` / 留空）：**要求结构化交付**。给了之后平台会往会话里
  注入 `submit_result` 工具（MCP），执行方完成时必须调它提交（字段见下节）；没交就结束轮次，
  平台自动追发一次，再没有就报主管 `result_missing`。CLI 一行版：
  `python deskport.py task "<描述>" --expects impl`。

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
`plan` / `permission_request`（**带 `callId` 与 `args`**，能对回具体调用）/ `error` /
`result`（结构化结果提交，见下节）。

**结构化交付（有 `expects` 的任务；没有该工具时用本节末尾的 CLI 退路）**：

交付 = 一次 `submit_result` 工具调用（平台自动放行，不弹审批）；自由文本报告只作补充。
入参 schema 按任务的 expects 动态给出（`tools/list` 可见中文说明），校验失败会带逐字段
错误原样返回，改完可重交（同一任务可交多次，最新为准）：

- `expects=impl`（实施方）：`summary`（≤300 字）、`commit`（7–40 位十六进制）、
  `tests{passed,failed,skipped}` 必填；`branch`、`gates{structure,frontend}`（pass/fail）、
  `risks[]`（每条 ≤200 字）、`questions[]`（有内容即表示需要主管判断）按实际填。
- `expects=verify`（验收方）：`verdict`（pass / rework / needs_judgment）、
  `target`（被验收的 commit）必填；`mustFix[]`、`suggest[]`、`evidence[]`（每条 ≤300 字）、
  `unverified[]` 按实际填。
- 结果落进任务卡顶部的结果卡片与事件流（`result` 事件），并给主管收件箱投一条
  `task_result`（needs_judgment / questions 非空时为 `task_judgment`，需判断）。

```text
GET  /api/tasks/<id>          → task.result（最新精简结果）+ task.resultCount
POST /api/tasks/<id>/result   → 同一份 schema（HTTP 提交面；认会话令牌/属主/主管令牌/网页会话）
```

- CLI 一行版：`python deskport.py task <任务id> --result`（打印结果 JSON）、
  `python deskport.py result --task <任务id> --json <文件>`（无 MCP 工具时的退路，
  `-` 读 stdin）。

**主管通信（令牌绑了主管才有）**：

怎么当主管（事件驱动）：派活 → 结束这一轮 → 被「【收件箱】…」叫醒 → `inbox` 看全文 →
验收 / 再派 / `report` 给用户 → 结束这一轮；外部主管没有中枢唤醒，用
`driver.py inbox [--wait 秒]` 长轮询代替。完整做法见 `COLLABORATION.md` 第 3、5、6 节
（派活规格、审批沿链上行、只用 report 汇报），托管主管的角色说明见 `docs/lead-role.md`。
接口：

- 收下属事件：`GET /api/agent/leads/inbox?after=<seq>&wait=<秒>`（长轮询，
  语义同任务 wait；没绑主管 → 400，提示先用 `/api/settings/leads` 建主管并绑令牌）。
  全部条目照返（游标语义不变），审批已落定的 escalation 带 `resolved: true`，
  CLI 显示时加「（已处理）」——别再当未处理去追问。
- 主动向用户汇报：`POST /api/agent/leads/report {"text": "..."}`——怎么送到
  用户眼前由 Deskport 决定（v1 设置页可见），主管只见这一个工具。
- 用户对主管说的话走 `POST /api/settings/leads/<id>/inbox`（网页会话），
  托管主管会被立即唤醒。
- 下属用 AskUserQuestion 提问（批次 L L2）：收件箱里是一条 `kind='question'`
  的条目（问题文本 + 选项 label + requestId），托管主管被立即唤醒。答复用
  `POST /api/tasks/<id>/answer {"requestId": "...", "text": "..."}`（主管令牌
  只许答复 `task.lead` 是自己的任务；网页会话也可）——审批按"允许"放行，
  答复立即投递给该任务。CLI：`deskport.py answer <task> <requestId> "<答复>"`。
- CLI 一行版（托管主管照这个用）：`python deskport/deskport.py lead`（我绑的
  主管）、`python deskport/deskport.py inbox [--after N] [--wait 秒]`（收件箱
  条目，一行一条，末尾 `lastSeq=`）、`python deskport/deskport.py report "<一句话>"`、
  `python deskport/deskport.py close <任务id>`（干完了关掉，回收进程）、
  `python deskport/deskport.py approve <任务id> <requestId> [--deny]`（审批下属
  的 escalation：批准 / 拒绝）。
- 下属卡审批时先找你：收件箱里会出现 `escalation` 条目（含 requestId 与参数
  摘要）并立即唤醒你；在边界内就用该任务的 `permission` 批准，不在或拿不准就
  拒绝并 `report` 向用户说明。约 5 分钟没决定，审批自动上交用户（仍可补批；
  你被停用时令牌已吊销，就只能由用户处理）。`approve` 只选「仅批准一次」
  (allow_once) 的选项，没有就报错——此时用 `report` 把情况下交用户。

**关键操作**：

```text
GET  /api/tasks/<id>                              → 状态（含 model/workspace/error）
GET  /api/tasks/<id>/log                          → 完整 jsonl 流水
POST /api/tasks/<id>/message    {"text": "..."}   → 续聊/追问（回合间）
POST /api/tasks/<id>/answer     {"requestId": "...", "text": "..."}
                                                 → 答复下属提问：放行 + 立即投递
POST /api/tasks/<id>/permission {"requestId": "...", "optionId": "...", "deny": false}
POST /api/tasks/<id>/cancel | /close              → 都返回 status；close 幂等
```

- 任务结束判定：`status: waiting`（这轮完成）或 `failed`（看 `error`）。
- 会话接手：node 托管的会话 web 重启后 `message` 可续（`canResume`）；
  stdio 回退（未启用 node）时中断会明确 409，请重建任务。
- 并发上限 `taskMaxRunning` 可配置（config.json，缺省 8，0=不限，改完热
  生效无需重启）；超限 429 响应带 `{limit, running, holders}`（谁占着名额）；
  202 是受理不是完成。
- 主管归属（指挥链 P1）：令牌可绑主管（设置页管理），建的任务自动记 `task.lead`；
  `GET /api/agent/lead` 自查绑没绑（没绑 `{"lead": null}`）。
- 全权令牌可以用 `deskport.py lead --register <名字>` 自助成为外部主管，无需用户操作。
- 按主管筛任务：`GET /api/tasks?lead=me`（按自己令牌绑的主管筛，没绑 400）
  或 `?lead=<主管id>`；主管被停用时建任务 403，已有任务的读/续聊/审批/关闭照常。

## 工作项（派开发类活：实施 → 验收 → 返工 → 集成）

开发类工作（改代码、跑门禁、合并）**必须**派成工作项，不许直接建任务——
平台会自动建 worktree/分支、派实施、派验收、把返工发回同一实施会话、
主仓跑门禁+全量后合并，主管只在需要判断时被叫醒。【平台强制】

```text
python deskport.py wi new --project deskport --spec <规格文件>     --impl <实施模型> --verify <验收模型> --footprint <GLOB>... [--title "<标题>"]
python deskport.py wi ls                                   # 一行一个：阶段/attempt/verdict/在等谁
python deskport.py wi show <工作项id>                       # 详情（含名下任务与集成日志尾部）
python deskport.py wi decide <工作项id> continue|rework|integrate|cancel [--text "<说明/返工要点>"]
```

- `--footprint` 必填：预计改动的文件/目录（glob）。同项目足迹相交的工作项
  不并行实施——后来者排队，`wi ls` 显示在等谁、共享什么；要强行并行加
  `--force-parallel`（记 history）。
- `--verify` 必须与 `--impl` 不同（同一模型 = 同一种盲区，创建时校验）。
- 实施交付走 `submit_result`（expects=impl）；验收结论 `pass/rework/
  needs_judgment`。`needs_judgment` 会进主管收件箱（`workitem_judgment`），
  用 `wi decide` 处理；`integrated` 即终态（上线部署不在本批）。
- 项目清单：`GET /api/settings/projects`（网页会话）；缺省项目 `deskport`
  指向本仓库副本，门禁/测试命令与 `.drive/merge_branch.py` 一致。

## 给平台提反馈（撞见 Deskport 自身的 bug 时）

干活的途中撞上平台缺陷（审批失灵、会话暴死、driver 抽风、接口行为和手册不符……），
直接提反馈——落进平台的反馈队列，处理进度可回查，别只在任务结尾的报告里带一句：

```text
POST /api/feedback
{"idempotency_key": "<uuid，重复提交不会落两条>",
 "client": {"platform": "web", "app_version": "agent"},
 "context": {"route": "/tasks/<任务id或页面>", "page_id": "TasksPage"},
 "message": "现象 + 任务id + 复现步骤（≤500字）"}
→ {"feedback_id": "...", "status": "submitted"}
```

- 认证同其它端点：`X-Agent-Token`，任何 scope 都行（设备/联邦令牌同样可以）。
- 身份服务端定：`user.id` = 令牌名、`role` = `agent`，信封里自报的 `user` 不采信，
  `anonymous` 对令牌通道无效——提意见一律实名留痕。
- 截图可选：先 `POST /api/feedback/attachments`（multipart 字段 `file`，≤1MB，
  png/jpg/webp）拿 `file_id`，塞进信封的 `attachments: [{"file_id": "..."}]`。
- 频控同网页端：每分钟 ≤5、每日 ≤20（429）；幂等键重复返回首次结果 + `duplicate:true`。
- 只能提：`GET/PATCH/DELETE` 是主人权限，智能体读了 401——处理进度问主控。

## 平级协调（占资源前先问，撞车时能问到人）

同一台机器上会有多个互不认识的智能体。占端口/目录/服务**之前**先查登记薄；
撞车或要协商（「9222 端口还要多久？」）直接给对方发消息——落对方的收件箱，
对方被立即唤醒。回复用同一端点互换 `to` 即可。

```text
GET /api/agent/registry                    # 登记薄：谁（id/名/kind）、在哪（node）
POST /api/agent/message                    # 平级消息（落对方收件箱，立即唤醒）
{"to": "<lead id 或 name>", "text": "...", "ref": "<可选>",
 "context": {"host": "ai-xmyz", "port": 9222}}
→ 202 {"ok": true, "to": "中招系统主控", "seq": 3}
```

- `to` 先按 lead id、再按 name 唯一匹配；找不到 400 带回 `availableLeads`。
- 频控每令牌每分钟 10 条（429 带 `retryAfter`）；不能给自己发。
- 没有群聊/公告板——一对一够用；对方没起托管任务时消息落箱，下次读取可见。

实例（端口撞车）：

```text
GET /api/agent/registry
→ {"leads": [{"id": "ld_1163a796", "name": "中招系统主控",
               "node": "ai-xmyz", "taskId": "6c2013d2a770"}]}
# 我要起 Lightpanda CDP，默认 9222——先问主人
POST /api/agent/message
{"to": "中招系统主控",
 "text": "我要起 Lightpampa CDP，默认端口 9222；你的 Playwright 实例（pid 1741899）还在用吗？还要多久？不行我错峰到 9223。",
 "context": {"host": "ai-xmyz", "port": 9222, "want": "lightpanda-cdp"}}
# 醒来后对方 inbox 里看到这条，回一条（互换 to）即完成协商
```

## 规程（必须遵守）

下面每一条都是硬性要求。标【平台强制】的，Deskport 会直接拦下；其余违反即退回重做。
完整规程与每条规矩的出处见同目录 `COLLABORATION.md`。

**所有使用者**

1. **先读后做**：不确定就先查（`GET /api/tasks/nodes`、`GET /api/tasks/<id>`、读文件），不许猜着写。
2. **只碰被授权的目录与主机**；`cwd` 必须在执行点声明的根之内。越界宁可不做。
3. **拿证据说话**：交付贴命令原文与输出原文；失败就说失败；不许用"我跑过了/没问题"代替证据。
4. **不绕过审批**：`pending` 时说明要做什么、为什么；不许伪造批准、不许换个写法绕过边界。【平台强制】
5. **凭据只放请求头**：令牌不许写进文件、日志、URL、代码、提交、命令参数，不许打印。
6. **不用工具自带的后台任务（run_in_background）。**【平台强制】服务器类进程用 `nohup … &` 启动，不许在前台跑。
7. **只按进程号结束自己启动的进程**；禁止按名字批量结束（`taskkill /IM`、`Stop-Process -Name`、`pkill`、`killall`）。
8. **直接 `import modules` 的脚本先把 `APPDATA` 指到自己的临时目录**，否则会读写线上状态。
9. **收尾**：任务结束就 close，失败也留一句原因。
10. **撞见平台缺陷先提反馈**（`POST /api/feedback`），再继续干活。

**派活的人（主管）另外必须**

11. **先成为主管再派活**：全权令牌运行 `python deskport.py lead --register "<名字>"`，一次即可。【平台强制：只能绑自己】
12. **派活带标题与边界**：`--title "<≤24 字>"`、`--mode default`、`policy` 边界；规格写死结果与例子。
13. **主管不自己改代码**；实施和验收派给不同模型的下属。
14. **派完就结束这一轮**：不等待、不轮询、不 sleep；外部主管用 `driver.py inbox` 等收件箱，不许把多个下属绑在一个等全部结束的循环里。
15. **审批只批一次性授权**（`deskport.py approve`）；拿不准就拒绝并 `report`。【平台强制】
16. **返工与复验接回原会话**（`deskport.py task … --session <会话编号>`），不新开执行方。
17. **只用 `report` 向用户汇报**；目标达成就停下，不自己立项。
18. **有 expects 的任务：交付 = 调 `submit_result` 提交结构化结果**，一轮结束前必须交；
    自由文本报告只作补充，不算交付。没交就结束轮次会被平台追发一次，仍不交报主管
    `result_missing`。【平台强制】
19. **派开发类工作必须用工作项**（`deskport.py wi new`）：平台自动建 worktree、派实施/验收、
    返工回同一会话、门禁+全量后合并；直接建任务只用于调查类。【平台强制】

## 规矩

- 命令与任务都真实执行：优先只读；写操作（写文件、装软件、动服务）先向用户说明再执行。
- 名字写错时响应带提示（`availableHosts` / `nodes` / `models`），直接改用，不用再列。
- 401 = 令牌失效：检查令牌是否仍由部署者签发（服务端按 `config.json` 里的令牌指纹校验）。
- 审计全留痕（agent.exec / agent.sftp.* / agent.session.* / agent.task.*），部署者可在控制台回看。
- 本技能由中枢分发，可自举更新：整包 `/api/agent/skill.zip` 与单个文件走同一套 URL（见上节），升级后重拉即同步；`X-Skill-Sha256` 用来判断要不要更新本地副本。
- 完整接口参考：仓库内 `docs/agent-api.md`；agent node 侧接口见 `agent_node/README.md`。
