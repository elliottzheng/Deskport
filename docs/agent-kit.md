# 智能体技能包：手册 + 客户端 + 驱动

**主入口是公开手册**：`GET /api/agent/handbook`（无需令牌）给任意智能体一条 URL 就能接入；
下面的技能包是 Kimi Code 一类支持技能目录的智能体的可选便利层，不是使用前提。

外部智能体接入 Deskport 只需要一个包，放在仓库 `.agents/skills/deskport/`，由中枢
按 URL 分发（同一套自举通道，升级后重拉即同步）。

| 文件 | 面向 | 用途 |
|---|---|---|
| `SKILL.md` | 智能体 | 接口速查：快命令、SFTP、持久会话、任务派活、工作线、主管通信 |
| `COLLABORATION.md` | 主控 | 协作原则（指挥链：用户 → 主管 → 下属）：分工、派活规格、审批边界、验收、省上下文 |
| `deskport.py` | 两边 | Python 客户端（851 行、零依赖），也能当 CLI |
| `driver.py` | 主控 | 驱动模板：别名目录、边界判定、自动放行、409 排队、收件箱模式、暂停续接 |
| `driver.example.json` | 主控 | `driver.json` 最小样例（必填 root/danger/safe，带 `_说明`） |

**指挥链**：用户只和少数主管说话，主管指挥执行方。令牌绑到一条主管记录上才进指挥链
（外部智能体用全权令牌跑 `python deskport.py lead --register "<名字>"` 自助绑定，
不需要用户操作；`GET /api/agent/lead` 自查）；用主管令牌建的任务自动记归属，收件箱把下属事件汇总给
主管，主管只用 `report` 向用户汇报。怎么当主管见 `COLLABORATION.md` 第 3、5、6 节，
托管主管的完整角色说明见 `lead-role.md`（一键创建时注入）。

客户端与驱动只用标准库（Python ≥ 3.9），拷一个文件到任何机器就能跑，不需要 pip。
整包下载时还会多一份生成的 `.deskport.json`（`url` + `node`，不含令牌；`host`
是旧名，与 `node` 同值，新旧客户端都读）。

## 自举（一条链接，无需令牌）

整包下载：`GET /api/agent/skill.zip`（公开）。解开就是技能目录，包里的 `.deskport.json`
已按本次下载的地址填好 `url`，`node` 缺省 `local`；`SKILL.md` 里的 `{{BASE}}`/`{{VERSION}}`
按本次请求替换，解开即可照做；**令牌不进包**，由人单独交给智能体。

```bash
D=<中枢地址>
curl -s -k "$D/api/agent/skill.zip" -o deskport-skill.zip   # 公网入口是隧道方的自签证书：不加 -k 会 exit 60；内网 http 直连不需要 -k
unzip -oq deskport-skill.zip -d ./deskport-skill/    # → ./deskport-skill/deskport/
export DESKPORT_TOKEN=<部署者发你的令牌>              # 或写进 ./deskport-skill/deskport/.agent-token
python ./deskport-skill/deskport/deskport.py hosts
```

有令牌时也可走需令牌的整包 `GET /api/agent/skills/deskport/pack.zip`（附件与原文件字节一致）：
`curl -s -k -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/pack.zip" -o pack.zip`。

按文件拉（可选，便于只更新一个文件；`?raw=1` 直接落盘，`X-Skill-Sha256` 校验）：

```bash
S=~/.kimi-code/skills/deskport; mkdir -p "$S"
curl -s -k -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])" > "$S/SKILL.md"
for f in deskport.py driver.py driver.example.json COLLABORATION.md; do
  curl -s -k -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/$f?raw=1" -o "$S/$f"
done
# 清单与版本：GET /api/agent/skills（含 files / pack / sha256）
```

换执行点或地址：改包里的 `.deskport.json`（`node` 可填 SSH 主机名，见
`GET /api/agent/hosts`；远程用 `insecure`/`cafile`/`pin` 处理自签证书）。
配置从低到高：脚本同目录 `.deskport.json` → `DESKPORT_URL` / `DESKPORT_NODE` /
`DESKPORT_INSECURE` → 函数参数；令牌：参数 `token` → `DESKPORT_TOKEN` → 同目录 `.agent-token`。
（旧名 `host` / `DESKPORT_HOST` 仍读，`node` 优先。）

## 没有令牌先拿工具（公开端点）

四个工具文件（`deskport.py` / `driver.py` / `driver.example.json` / `COLLABORATION.md`）
不含凭据，可**不带令牌**直接下载；整包 `skill.zip` 还额外带上按请求渲染的 `SKILL.md`：

```bash
D=<中枢地址>
curl -s $D/api/agent/tools -o -            # 清单 {name, bytes, sha256}
curl -s $D/api/agent/tools/deskport.py -o deskport.py    # 响应头 X-Tools-Sha256 可校验
curl -s $D/api/agent/skill.zip -o deskport-skill.zip     # 整包：6 个条目
```

开关与手册共用 `handbookEnabled`（`false` 时 404）。详见 `docs/agent-api.md`「工具箱」。

## 客户端：五分钟

```python
import deskport

h = deskport.connect()              # 用 .deskport.json 里的 node（例：远端执行机；旧键 host 仍读）
print(h.sh("uptime"))               # 返回 stdout，非零退出抛 CommandError
repo = h.cd("E:/data")              # 绑定工作目录（路径按目标机器的 OS：Windows E:/data，Linux /srv/app），之后相对路径都按它解析
print(repo.sh("git log --oneline -5"))
repo.write("notes.txt", "hello")

local = deskport.connect("local")   # 本机（宿主机），路径可写 E:\事项 或 /e/事项
```

派活与工作线：

```python
t = h.dp.create_task("把 tests/ 下 3 个失败用例修掉，只改这 3 个文件", mode="auto",
                     policy={"allowPaths": ["tests"], "denyCommands": ["git push"]})
print(t.until(600))                 # 阻塞等服务端给一行结论：status/finalText/pending

w = h.dp.workline("r5")
routed, task = w.send("继续第 5 批：补 SSH 隧道重连测试")   # 自动决定：投递 / 排队 / 开新任务
print(routed, task.until(600)["finalText"])
```

## 客户端：对象与命令行

| 对象 | 拿到的办法 | 关键方法 |
|---|---|---|
| `Deskport` | `deskport.Deskport(url, token=...)` | `hosts` `host` `run` `ls` `read` `write` `upload` `session` `nodes` `create_task` `task` `tasks` `worklines` `create_workline` `kimi_sessions` `lead` `inbox` `report` `closeTask` `approve` |
| `Host` | `deskport.connect(host, cwd=...)` 或 `dp.host(name)` | `sh` `run` `cd` `using(shell)` `session` `ls` `read` `read_text` `write` `path` |
| `Session` | `h.session()` | `run` `sh` `close`（`with` 自动关；cd/export/venv 都延续） |
| `Task` | `dp.create_task` / `dp.task(id)` | `until` `wait` `events` `message` `permission` `compact` `cancel` `close` |
| `Workline` | `dp.workline(id)` / `dp.create_workline` | `send` `update_doc` `rotate` `info` `delete` |

`Host.sh` 非零退出抛 `CommandError`；`Host.run` 返回 `ExecResult`（`exit` / `out` /
`err` / `timed_out` / `truncated`）。HTTP 错误统一抛 `DeskportError(status, message)`，
message 里带服务端提示（`availableHosts` / `nodes` / `models` / `requestId`）。

```bash
python deskport.py hosts                    # 列出主机
python deskport.py run "df -h" -C /srv      # 远程执行，退出码透传（超时 124）
python deskport.py ls /srv                  # 远程列目录
python deskport.py cat /etc/hosts           # 远程取文件到 stdout（二进制安全）
```

CLI 全部子命令（`python deskport.py <命令> -h` 看各自动态帮助）：

| 命令 | 用途 |
|---|---|
| `hosts` | 列出主机（SSH 主机 + 本机 `local`） |
| `run <命令> [-t 秒] [-C 目录]` | 单发执行（无状态），退出码透传 |
| `ls [路径]` / `cat <路径>` | 列目录 / 取文件到 stdout |
| `task <描述> [-n 执行点] [-m 模式] [--model 模型] [--title 标题] [--session 会话编号] [--wait 秒] [--allow/--deny]` | 建任务派活；`--wait` 建完就等这一轮结束并打印最终回复 |
| `tasks` | 列出任务（id / 状态 / 执行点 / 标题首行） |
| `wait <任务id> [--timeout 秒] [--allow/--deny]` | 阻塞等某个任务结束并打印最终回复 |
| `lead [--register 名字 [--project X]]` | 看绑定的主管；全权令牌 `--register` 自助成为外部主管（没绑打印一行提示） |
| `inbox [--after N] [--wait 秒]` | 收件箱条目，一行一条，末尾 `lastSeq=` 游标 |
| `report "<一句话>"` | 向用户汇报（主管唯一的嘴） |
| `close <任务id>` | 关闭任务（明确结束，回收进程） |
| `approve <任务id> <requestId> [--deny]` | 审批下属的 escalation（只批 allow_once；拒绝加 `--deny`） |
| `answer <任务id> <requestId> "<答复>"` | 答复下属任务的提问（审批按"允许"放行，答复立即投递） |

`lead` / `inbox` / `report` / `approve` 是指挥链上的主管命令：令牌绑了主管才有
（没绑 `inbox`/`report` 报 400；全权令牌跑 `lead --register "<名字>"` 自助成为主管，
不需要用户操作）。派活用 `--title` 给标题
（≤ 24 字）；不给时服务端从描述首行推导。返工/复验要接回原会话继续干活（复用
执行方）用 `task "<描述>" --session <会话编号>`：请求体带 `kimiSessionId` 且
`fork=false` 直接接管，不用副本（被占用时 409）。

已知的服务端边界（客户端已吸收）：exec 无状态、单条 1–600s、输出上限 2 MiB、含 NUL
提前拒绝；`read` 整读上限 8 MB，超出自动分块；`create_task` 的 prompt 上限 4000 字符；
`create_task` 用 `node` 指执行点（缺省 `local`）、`cwd` 指执行目录：本机是本机
工作目录，非本机是该机器上的目录（必填）；任务目录/输入副本仍由 Deskport 的
workspace 管。非本机任务的 `paths` / `attachments` 由服务端投到该机器的
`<cwd>/.deskport-inputs/<taskId>/`（prompt 末尾写明目录），失败则不建任务；
agent node 走它的 SSH 主机（`agentNodes[].via`），联邦成员要求中枢
`remotes[].filesWrite` 与成员端 `federationRoots` 都开。隧道偶尔截断大响应
（幂等 GET 自动重试一次）。

## 驱动模板：一条命令盯一个执行方

`driver.py` 把「派活—审批—收结论」压成两个字符一条命令，边界写在 `driver.json`
（必填 root/danger/safe，字段与最小样例见技能包里的 `driver.example.json`）：

```json
{
  "root": "E:/proj/app",
  "danger": "rm\\s+-rf|\\bkill\\b|git\\s+(push|reset)\\b|deploy",
  "safe": "^\\s*(ls|cat|grep|git (status|diff|log))\\b",
  "profiles": {"commit": "git\\s+-c\\s+\\S+\\s+(add|commit)"},
  "extraWritable": ["/tmp/build/"], "defaultProfile": "dev",
  "serverPolicy": true
}
```

```text
driver.py <别名> new <kimi会话id|w:工作线id|-> [档位]   用 msg.txt 建任务（title.txt 存在时作为 title 传出）
driver.py <别名> m [档位]      发 run/<别名>/msg.txt（同内容不重发）
driver.py <别名> y | n         批准 / 拒绝卡住的审批
driver.py <别名> c             压缩执行方上下文（/compact）
driver.py <别名> st            任务状态、档位、上下文用量、有无挂起审批
driver.py <别名> resume        任务被部署打成 paused/interrupted 时：先发续接消息再照常驱动
driver.py <别名>               （无参数）接着跑，等这一轮结束
driver.py inbox [--wait 秒]    收件箱模式（外部主管）：长轮询，有新条目一行一条打印
```

- **收件箱模式**（外部主管代替轮询）：`driver.py inbox [--wait 秒]` 长轮询
  `GET /api/agent/leads/inbox`，游标存 `run/_inbox/state.json`；有新条目就一行一条
  打印（`[kind] 摘要`）并退出 0，超时无新条目打印 `IDLE` 退出 2，令牌未绑主管一句话
  报错退出 2；打印完自动 ack。托管主管不用它——中枢会发「【收件箱】…」消息唤醒。
- **暂停续接**：部署（web 重启）会把在跑任务打成 `paused`——不是失败，节点侧会话还在。
  `driver.py <别名> resume` 发一条续接消息（默认「中枢重启过，这一轮被打断了；先确认
  做到哪一步，再从断点继续，边界与交付要求不变」，可用 `run/<别名>/msg.txt` 覆盖），
  然后照常驱动；`drive()` 遇到 paused 的 STOP 提示也指向 `resume`。

- **边界而非每一步**：命中 `danger` 才停，其余按边界放行；`profiles` 是规格预授权档位
  （`m commit` 打开提交权限）。同时给服务端 `policy` 兜一层（`serverPolicy`）。
- **越界只看真正执行的部分**：heredoc 正文、引号里的字符串不算；`cd <root> && rm -rf build`
  这种清自己产物不算危险。
- **结论一行**：`DONE ...` / `STOP 原因 | 工具 | 命令摘要` / `TIMEOUT` 写进
  `run/<别名>/last.txt`，全量流水进 `run/<别名>/log`；`state.json` 记住任务号、档位、
  未送达的消息（409 排队，本轮结束补发）。
- **run 目录**：默认在技能包内 `run/`；技能包目录可能只读、或会被本机清理进程动，
  可用环境变量 `DRIVER_RUN_DIR` 指到别处（例如主控自己的工作区），别名目录
  （`msg.txt` / `last.txt` / `log` / `state.json`）随之迁移。
- **首次可用**：`-h` 看用法（不碰状态）；`driver.json` 缺失/缺键、`state.json` 损坏、
  证书错误都只打印一句人话 + 非零退出码，**不吐栈**（要栈用 `DRIVER_DEBUG=1`）。
- **退出码**：0 本轮结束（收件箱模式 = 有新条目）、1 出错（含中枢不支持 `/wait`）、
  2 超时/配置与状态文件问题/未绑主管/内部错误（收件箱模式超时无新条目打 `IDLE`）、
  3 停下待判断、4 审批已失效（回放污染）、5 消息未更新、6 同一别名已有驱动在跑。
- 一轮内长时间无事件且无工具在跑（上下文膨胀）会判停并提示换新会话接棒。

## 放进一个项目里

1. **只读引用，不复制**：客户端、令牌、手册都指向原件，原件更新即生效；自己的目录只放
   项目私有文件（规格、验收记录、`driver.json`）。
2. **边界写成文件**：可写目录、危险命令、预授权档位都进 `driver.json`，不靠人记。
3. **输入写文件，结论读一行**：规格写 `run/<别名>/msg.txt`，结论 `cat run/<别名>/last.txt`。
4. **单写者**：同一别名（= 同一工作目录）同一时间一个驱动；并行任务用不同别名与不同边界。

接口逐条对照见 [agent-api.md](agent-api.md)，网络层的角色、韧性与设计原则见
[agent-network.md](agent-network.md)，协作规矩见
[../.agents/skills/deskport/COLLABORATION.md](../.agents/skills/deskport/COLLABORATION.md)。
