# 智能体技能包：手册 + 客户端 + 驱动

外部智能体接入 Deskport 只需要一个包，放在仓库 `.agents/skills/deskport/`，由中枢
按 URL 分发（同一套自举通道，升级后重拉即同步）。

| 文件 | 面向 | 用途 |
|---|---|---|
| `SKILL.md` | 智能体 | 接口速查：快命令、SFTP、持久会话、任务派活、工作线 |
| `COLLABORATION.md` | 主控 | 协作原则：分工、派活规格、审批边界、验收、省上下文 |
| `deskport.py` | 两边 | Python 客户端（605 行、零依赖），也能当 CLI |
| `driver.py` | 主控 | 驱动模板：别名目录、边界判定、自动放行、409 排队 |

客户端与驱动只用标准库（Python ≥ 3.9），拷一个文件到任何机器就能跑，不需要 pip。

## 自举（装到本地技能目录）

```bash
D=<中枢地址>; T=<令牌>; S=~/.kimi-code/skills/deskport; mkdir -p "$S"
curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])" > "$S/SKILL.md"
for f in deskport.py driver.py COLLABORATION.md; do
  curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/$f?raw=1" -o "$S/$f"
done
# 清单与版本：GET /api/agent/skills（含 files / sha256），响应头 X-Skill-Sha256
```

客户端还需要两个文件（不随包分发，由部署者给）：`.agent-token`（令牌明文）和
`.deskport.json`（`{"url": "...", "host": "...", "insecure": false, "cafile": "...", "pin": true}`）。
配置从低到高：脚本同目录 `.deskport.json` → `DESKPORT_URL` / `DESKPORT_HOST` /
`DESKPORT_INSECURE` → 函数参数；令牌：参数 `token` → `DESKPORT_TOKEN` → 同目录 `.agent-token`。

## 客户端：五分钟

```python
import deskport

h = deskport.connect()              # 用 .deskport.json 里的 host（例：远端执行机）
print(h.sh("uptime"))               # 返回 stdout，非零退出抛 CommandError
repo = h.cd("/srv/app")             # 绑定工作目录，之后相对路径都按它解析
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
| `Deskport` | `deskport.Deskport(url, token=...)` | `hosts` `host` `run` `ls` `read` `write` `upload` `session` `nodes` `create_task` `task` `tasks` `worklines` `create_workline` `kimi_sessions` |
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

已知的服务端边界（客户端已吸收）：exec 无状态、单条 1–600s、输出上限 2 MiB、含 NUL
提前拒绝；`read` 整读上限 8 MB，超出自动分块；`create_task` 的 prompt 上限 4000 字符；
`acp` 引擎不接受 `cwd`；隧道偶尔截断大响应（幂等 GET 自动重试一次）。

## 驱动模板：一条命令盯一个执行方

`driver.py` 把「派活—审批—收结论」压成两个字符一条命令，边界写在 `driver.json`：

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
driver.py <别名> new <kimi会话id|w:工作线id|-> [档位]   用 msg.txt 建任务
driver.py <别名> m [档位]      发 run/<别名>/msg.txt（同内容不重发）
driver.py <别名> y | n         批准 / 拒绝卡住的审批
driver.py <别名> c             压缩执行方上下文（/compact）
driver.py <别名> st            任务状态、档位、上下文用量、有无挂起审批
driver.py <别名>               （无参数）接着跑，等这一轮结束
```

- **边界而非每一步**：命中 `danger` 才停，其余按边界放行；`profiles` 是规格预授权档位
  （`m commit` 打开提交权限）。同时给服务端 `policy` 兜一层（`serverPolicy`）。
- **越界只看真正执行的部分**：heredoc 正文、引号里的字符串不算；`cd <root> && rm -rf build`
  这种清自己产物不算危险。
- **结论一行**：`DONE ...` / `STOP 原因 | 工具 | 命令摘要` / `TIMEOUT` 写进
  `run/<别名>/last.txt`，全量流水进 `run/<别名>/log`；`state.json` 记住任务号、档位、
  未送达的消息（409 排队，本轮结束补发）。
- **退出码**：0 本轮结束、1 出错（含中枢不支持 `/wait`）、2 超时、3 停下待判断、
  4 审批已失效（回放污染）、5 消息未更新、6 同一别名已有驱动在跑。
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