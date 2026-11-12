# deskport.py —— 外部智能体用的 Python 客户端

[`deskport.py`](deskport.py) 是 Deskport 智能体 API 的单文件客户端：**只用标准库**，
拷贝到任何一台有 Python 3.9+ 的机器（另一台电脑、远端执行机、Claude Code / Kimi
的工作目录）就能用，不需要 pip 安装，也不需要读 HTTP 文档。

三个文件就是一套：

| 文件 | 内容 |
|---|---|
| `deskport.py` | 客户端 + 命令行，约 600 行，零依赖 |
| `.agent-token` | 中枢签发的令牌（明文，**不要入库、不要打印**） |
| `.deskport.json` | `{"url": "...", "host": "...", "insecure": false, "cafile": "...", "pin": true}` |

配置从低到高：脚本同目录 `.deskport.json` → 环境变量 `DESKPORT_URL` /
`DESKPORT_HOST` / `DESKPORT_INSECURE` → 函数参数。令牌：参数 `token` →
`DESKPORT_TOKEN` → 脚本同目录 `.agent-token`。

## 五分钟

```python
import deskport

h = deskport.connect()              # 用 .deskport.json 里的 host（例：远端执行机）
print(h.sh("uptime"))               # 返回 stdout，非零退出抛 CommandError
repo = h.cd("/srv/app")             # 绑定工作目录，之后相对路径都按它解析
print(repo.sh("git log --oneline -5"))
print(repo.read_text("notes.txt"))
repo.write("notes.txt", "hello")

local = deskport.connect("local")   # 本机（宿主机），路径可写 E:\事项 或 /e/事项
```

派活给托管智能体（多步、长任务走这条）：

```python
t = h.dp.create_task("把 tests/ 下 3 个失败用例修掉，只改这 3 个文件",
                     mode="auto",
                     policy={"allowPaths": ["tests"], "denyCommands": ["git push"]})
print(t.until(600))                 # 阻塞等服务端给一行结论：status/finalText/pending
```

工作线（跨会话的接力棒）：

```python
w = h.dp.workline("r5")
routed, task = w.send("继续第 5 批：补 SSH 隧道重连测试")   # 自动决定：投递 / 排队 / 开新任务
print(routed, task.until(600)["finalText"])
```

## 对象一览

| 对象 | 拿到的办法 | 关键方法 |
|---|---|---|
| `Deskport` | `deskport.Deskport(url, token=...)` | `hosts` `host` `run` `ls` `read` `write` `upload` `session` `nodes` `create_task` `task` `tasks` `worklines` `create_workline` `kimi_sessions` |
| `Host` | `deskport.connect(host, cwd=...)` 或 `dp.host(name)` | `sh` `run` `cd` `using(shell)` `session` `ls` `read` `read_text` `write` `path` |
| `Session` | `h.session()` | `run` `sh` `close`（`with` 自动关；cd/export/venv 都延续） |
| `Task` | `dp.create_task` / `dp.task(id)` | `until` `wait` `events` `message` `permission` `compact` `cancel` `close` |
| `Workline` | `dp.workline(id)` / `dp.create_workline` | `send` `update_doc` `rotate` `info` `delete` |

`Host.sh` 非零退出抛 `CommandError`；`Host.run` 返回 `ExecResult`（`exit` / `out` /
`err` / `timed_out` / `truncated`），要自己判退出码时用它。HTTP 错误统一抛
`DeskportError(status, message)`，message 里带服务端提示（`availableHosts` /
`nodes` / `models` / `requestId`），名字写错时按提示改即可。

## 命令行

```bash
python deskport.py hosts                    # 列出主机
python deskport.py run "df -h" -C /srv      # 远程执行，退出码透传（超时 124）
python deskport.py ls /srv                  # 远程列目录
python deskport.py cat /etc/hosts           # 远程取文件到 stdout（二进制安全）
```

## 已知的服务端边界

客户端把这些写进了方法里，正常使用不会撞上，排查的时候要知道：

- exec 无状态（`cd` 不延续），单条 `timeoutSec` 1–600 越界即报错，输出上限 2 MiB
  （超了 `truncated=True`），命令含 NUL 客户端提前拒绝（服务端会 500）；
- `read` 整读上限 8 MB，超过自动按 `offset`/`length` 分块拼接；
- `create_task` 的 `prompt` 上限 4000 字符，长材料用 `attachments` 或先 `upload`；
- `acp` 引擎不接受 `cwd`（服务端会静默忽略），要指定目录得用 `engine="remote"`；
- 隧道偶尔截断大响应（`IncompleteRead`），幂等 `GET` 自动重试一次。

## 放进一个项目里

外部智能体接手某个项目时，这套东西的使用约定（来自真实的长任务实践）：

1. **只读引用，不复制**：客户端、令牌、技能文档都指向原件，原件更新即生效；自己
   的目录只放项目私有文件（规格、验收记录、边界配置）。
2. **边界写成文件**：一份 `project.json` 声明可写目录、危险命令正则、预授权档位，
   由薄封装加载后注入；命令落在边界内自动放行，越界才停下来问人。
3. **输入写文件，结论读一行**：每个任务别名一个目录（`run/<别名>/msg.txt`、
   `last.txt`、`state.json`），规格写进文件、结论只读一行——这就是「响应即上下文」
   在客户端侧的做法。回合中插话（服务端 409）在客户端排队，本轮结束自动补发。

网络这一层的设计原则与实测协作成本见 [../docs/agent-network.md](../docs/agent-network.md)，
接口逐条对照见 [../docs/agent-api.md](../docs/agent-api.md)。