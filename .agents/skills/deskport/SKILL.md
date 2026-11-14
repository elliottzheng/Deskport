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
- 主管归属（指挥链 P1）：令牌可绑主管（设置页管理），建的任务自动记 `task.lead`；
  `GET /api/agent/lead` 自查绑没绑（没绑 `{"lead": null}`）。
- 按主管筛任务：`GET /api/tasks?lead=me`（按自己令牌绑的主管筛，没绑 400）
  或 `?lead=<主管id>`；主管被停用时建任务 403，已有任务的读/续聊/审批/关闭照常。

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

## 协作原则（八条）

不管你是派活的还是接活的，先把这八条过一遍（30 秒）：

1. **先读后做**：不确定就先看——`GET /api/tasks/nodes`、`GET /api/tasks/<id>`、读文件，别猜着写。为什么：现场事实以查到的为准，猜错方向比多一次查询贵得多。
2. **只读优先**：能查就别改；要写文件、装软件、动服务，先说清要做什么再动手。为什么：写操作不可逆或影响面大，先说明，别人才有机会拦住你。
3. **复述验收标准**：开工前把任务复述成“什么算做完”；有歧义就停下来问一句。为什么：验收标准含糊，做成没做成就会各说各话，返工比问一句贵。
4. **边界**：只碰被授权的目录与主机，`cwd` 必须在执行点声明的根之内；越界宁可不做。为什么：越界改动可能毁掉别人正在用的东西，“顺手”从来不是授权。
5. **不自夸**：不许用“我跑过了/没问题”代替证据；给命令原文与输出，失败就说失败。为什么：能被采信的只有你报告的现象，“已完成”必须可验证，掩盖失败会让错误活下去。
6. **收尾**：任务结束就 `POST /api/tasks/<id>/close`；失败也留一句原因。为什么：悬着的任务占着进程与名额，别人不知道你是做完了还是卡住了。
7. **审批**：`wait` 返回 `pending` 时，说明“要做什么、为什么”；**不要**试图绕过审批或伪造批准。为什么：审批是边界唯一真正生效的地方，绕过得了一次，信任和凭据一起报废。
8. **凭据**：令牌只放请求头；**不要**写进文件、日志、URL、代码或提交里。为什么：落盘的令牌等于把远程执行权送给每个能读到那份文件的人。

## 规矩

- 命令与任务都真实执行：优先只读；写操作（写文件、装软件、动服务）先向用户说明再执行。
- 名字写错时响应带提示（`availableHosts` / `nodes` / `models`），直接改用，不用再列。
- 401 = 令牌失效：检查令牌是否仍由部署者签发（服务端按 `config.json` 里的令牌指纹校验）。
- 审计全留痕（agent.exec / agent.sftp.* / agent.session.* / agent.task.*），部署者可在控制台回看。
- 本技能由中枢分发，可自举更新：整包 `/api/agent/skill.zip` 与单个文件走同一套 URL（见上节），升级后重拉即同步；`X-Skill-Sha256` 用来判断要不要更新本地副本。
- 完整接口参考：仓库内 `docs/agent-api.md`；agent node 侧接口见 `agent_node/README.md`。
