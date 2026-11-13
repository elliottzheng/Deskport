# Deskport Agent Handbook

Deskport 是一台电脑上的远程执行与任务托管服务：你可以让它在宿主机器（以及它已保存的 SSH 主机）上执行命令、读写文件，或把多步任务委托给它托管的智能体。
你只需要一个 HTTP 客户端：不需要安装 CLI 或 SDK，也不需要是 Kimi 或其他特定品牌的智能体。
令牌由部署 Deskport 的人另外发给你，每次请求放在请求头 `X-Agent-Token` 里（也接受 `Authorization: Bearer <令牌>`）。

- API base（本次请求的入口）：{{BASE}}
{{LAN_LINE}}
- 手册版本：{{VERSION}}
- 本手册是公开的，不含任何密钥；可以随时重读。程序化读取用 `GET {{BASE}}/api/agent/handbook?format=json`，返回 `{version, base, markdown}`。
- 令牌可能被收窄到某些 scope（例如只给 `share`）；权限不够时 403 会带 `need`，让部署者补发即可。

## 证书是自签的

公网入口（HTTPS 隧道）用的是隧道方的自签名证书，系统信任库里没有它——不处理会报证书错误（`curl` 退出码 60）。三种连法：

- `curl` 加 `-k`：`curl -k https://<入口>/api/agent/handbook`；
- `deskport.py`：`.deskport.json` 里写 `"insecure": true`，或用 `"cafile"` 指向部署者给你的证书文件（只信任这一张，比全局跳过校验更稳）；两样都不配会抛 `CERTIFICATE_VERIFY_FAILED`；
- 局域网直连是纯 HTTP，没有这个问题，也不需要 `-k`。

这只在你确认连的就是自己的 Deskport 时才可接受；对别的地址别顺手 `-k`。下面无需令牌的下载示例都按公网入口写、带 `-k`；带令牌的示例同理，内网 http 直连时 `-k` 可去掉。

## 最短路径

```bash
D={{BASE}}
T=<部署者发你的令牌>
```

1. `GET $D/api/tasks/nodes` 列出执行点。`local` 是这台电脑本身。
2. `POST $D/api/tasks` 建任务：body 给 `prompt`；`node` 缺省 `local`，`cwd` 缺省为服务端分配的任务目录。
3. `GET $D/api/tasks/<id>/wait?after=0&timeout=300` 阻塞等结果。`status` 变成 `waiting` / `failed` 就是本轮结束；`pending` 非空表示等你审批。
4. `POST $D/api/tasks/<id>/message` 续聊，`POST $D/api/tasks/<id>/close` 明确结束。

所有请求和响应都是 JSON（分享接口是 multipart）。失败也返回 JSON：`{"error": "..."}`，部分错误带 `need` 或提示字段。

## 工具箱（任选其一，不必全用）

上面的裸 HTTP 是底座：任何语言都能照做。不想自己写轮询/审批/任务管理，就用现成工具——三级递进，**任选其一，不必全用**。

**想一条命令拿全？** 整包 zip 把技能手册、客户端、驱动模板、`driver.json` 样例、协作原则和现成的 `.deskport.json` 打在一起（公开，无需令牌）：

```bash
curl -s -k $D/api/agent/skill.zip -o deskport-skill.zip && unzip -oq deskport-skill.zip   # 公网入口自签证书要 -k；内网 http 不需要
```

解开得到 `deskport/` 目录，`SKILL.md` 里的地址按本次下载填入。**包里 `SKILL.md` 与 `COLLABORATION.md` 是必读**（不是"需要时再读"）：前者是完整用法，后者讲主控与执行方怎么分工、派活、验收。要当 Kimi 技能用就解到技能目录：`unzip -oq deskport-skill.zip -d ~/.kimi-code/skills/`。

### 1. 裸 HTTP（本手册其余部分）

不带任何依赖，`curl` 或标准库 HTTP 即可。适合一次性调用，或要把调用逻辑嵌进你自己的程序。

### 2. deskport.py — 零依赖单文件客户端

一个 Python 文件、只用标准库，下载即可用：

```bash
curl -s -k $D/api/agent/tools/deskport.py -o deskport.py
```

最快三行：

```python
import deskport
h = deskport.connect()   # 地址与令牌：同目录 .deskport.json，或 DESKPORT_URL / DESKPORT_NODE / DESKPORT_TOKEN
print(h.sh("uptime"))    # 失败抛 CommandError；h.cd("E:/data") 绑定工作目录（Linux 换成 "/srv/app"）后再发命令
```

也能当 CLI：

```bash
python deskport.py hosts
python deskport.py run "uptime"
```

派活、等结果、审批、持久会话都有对应方法（`create_task` / `until` / `permission` / `session`），不用自己写轮询。

### 3. driver.py — 长期驱动另一个智能体

适用场景：**持续驱动一个托管智能体干活**——别名目录、边界内自动放行、越界停下等人、本轮忙碌时消息排队（结束后自动补发）。

```bash
curl -s -k $D/api/agent/tools/driver.py -o driver.py
```

诚实声明：它假定执行方是 **Kimi Code**——`new <kimi会话id>` 接手已有会话、`/compact` 压缩上下文都是 Kimi 侧的能力；执行方不是 Kimi 时请用第 1 或第 2 级。用法一行：`driver.py <别名> [y|n|c|m|new <会话id>|st]`，边界写在 `driver.json`（必填 root/danger/safe，字段见随包的 `driver.example.json`，首次运行照它抄一份）；先 `new` 建任务，之后才能 y/n/c/m/st。

`COLLABORATION.md` 讲主控怎么分工与验收——**必读**（真去指挥别的智能体之前先读一遍）：

```bash
curl -s -k $D/api/agent/tools/COLLABORATION.md -o COLLABORATION.md
```

## 端点

### 执行点：GET /api/tasks/nodes

返回 `{"nodes": [{"name", "via", "kind", "models", "defaultModel", "modelSelectable", "os", "shells", "cwdRoots", "defaultCwd"}]}`。
`name` 就是建任务时 `node` 的取值；`local` 是本机。`models` 可选模型由执行点上报。
`os`（`windows` / `linux` / `darwin`，未知时缺省）与 `shells` 同样由执行点自报——**路径和 shell 按它选**（见「执行命令」开头），`cwdRoots` 是执行点声明的允许根。

```bash
curl -s -H "X-Agent-Token: $T" "$D/api/tasks/nodes"
```

### 建任务：POST /api/tasks

```bash
curl -s -X POST "$D/api/tasks" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"prompt": "修掉 tests/ 里的 3 个失败用例", "node": "local", "cwd": "E:/data", "mode": "auto"}'
# Linux 执行点：cwd 换成 "/srv/app"
```

- `prompt` 必填，上限 4000 字符。
- `node` 是执行点（去哪台机器），缺省 `local`；`cwd` 是执行目录，缺省由服务端分配的任务目录。
- `cwd` 是执行目录，**按执行点所在机器的 OS 书写**（Windows 用 `E:/data` 这样的盘符路径、正斜杠就能用；Linux 用 `/srv/app`；判断方法见「执行命令」开头）：本机是可写的项目目录；非本机执行点必填，目录须存在于那台机器，且必须在执行点声明的允许根（`GET /api/tasks/nodes` 的 `cwdRoots`，空数组=未知不校验）之内，越界 → 400。
- `mode` 不传 = 逐项审批（安全默认）；要自动批准显式传 `"mode": "auto"`。
- `model` 可选，取值见执行点的 `models`。
- `paths` 可选：`["/绝对路径/a.pdf"]`，把本机已有文件带进任务；`attachments` 直接内联 `[{name, contentB64}]`。
- 响应 `202`：`{"task": {"id": "...", "status": "queued", ...}}`。`202` 是受理，不是完成。

### 任务状态：GET /api/tasks/&lt;id&gt;

```bash
curl -s -H "X-Agent-Token: $T" "$D/api/tasks/<id>"
```

响应 `{"task": {...}}`，常用字段：`status`（`queued` / `running` / `waiting` / `failed` / `cancelled` / `closed`）、`finalText`、`error`、`model`、`workspace`、`lastSeq`。

### 等结果：GET /api/tasks/&lt;id&gt;/wait

```bash
curl -s -H "X-Agent-Token: $T" "$D/api/tasks/<id>/wait?after=0&timeout=300"
```

一次请求阻塞到「需要你决策」或「本轮结束」，避免空轮询。`timeout` 缺省 300 秒，上限 600。

```json
{"taskId": "...", "status": "waiting", "finalText": "回答全文",
 "pending": null, "stats": {"events": 42, "tools": 8, "autoApproved": 0},
 "lastSeq": 42, "error": ""}
```

- `status` 为 `waiting` / `failed` = 本轮结束；`running` = 超时，带 `lastSeq` 再调即可（不丢不重）。
- `pending` 非空 = 等审批：`{requestId, callId, name, args, options: [{optionId, name, kind}]}`。
- `status` 为 `permission` 时同样看 `pending`。

### 续聊：POST /api/tasks/&lt;id&gt;/message

```bash
curl -s -X POST "$D/api/tasks/<id>/message" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"text": "先跑测试，再给结论", "mode": "queue"}'
```

- `mode` 缺省 `queue`（本轮结束后投递）；`steer` 打断当前轮并把消息插到队首；`reject` 在轮中直接 409。
- 可带 `"wait": 300`，发完直接阻塞返回下一批决策。
- 回合已结束（`waiting`）时直接续聊；任务已 `closed` 则 409。

### 审批：POST /api/tasks/&lt;id&gt;/permission

```bash
curl -s -X POST "$D/api/tasks/<id>/permission" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"requestId": "<pending.requestId>", "optionId": "<options 里的一个>"}'
```

拒绝用 `{"requestId": "<pending.requestId>", "deny": true}`。`optionId` 必须是该请求 `options` 里的值，否则 400；请求已失效返回 409。可带 `"wait": 300` 直接等下一个决策点。

### 结束：POST /api/tasks/&lt;id&gt;/close

```bash
curl -s -X POST -H "X-Agent-Token: $T" "$D/api/tasks/<id>/close"
```

明确结束任务并回收执行进程；幂等，返回 `{"ok": true, "status": "..."}`。

### 主机清单：GET /api/agent/hosts

```bash
curl -s -H "X-Agent-Token: $T" "$D/api/agent/hosts"
```

返回 `{"hosts": [...]}`。每条 SSH 主机只有 `{id, name, host, port}`（不含凭据）；`local`（本机）条目额外带 `os` / `shells` / `defaultShell`：`os` 是 `windows` / `linux` / `darwin`，`shells` 列出这台机器支持哪些 shell（Windows 上是 `bash` / `powershell` / `pwsh`），本机 `ssh/exec` 的 `shell` 参数从里面挑（缺省 `defaultShell`）。远端 SSH 主机不带 `os`——路径按那台机器的实际系统写，拿不准先跑一条 `uname -s` 探。

### 执行命令：POST /api/agent/ssh/exec

单发、无状态（`cd` 不延续）；需要多步状态用任务的 `cwd`，或反复带 `cwd`。

> **路径按目标机器的 OS 写**：先 `GET /api/agent/hosts`（本机条目）或 `GET /api/tasks/nodes`（agent node / 联邦成员）看 `os` 字段——`windows` / `linux` / `darwin`，未知时缺省。Windows 用 `E:/data`（正斜杠就能用），Linux 用 `/srv/app`。写错风格的路径，命令还没执行就死在 `cd: No such file or directory`。

```bash
# Windows 本机（E:/data 换成你的目录）
curl -s -X POST "$D/api/agent/ssh/exec" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"node": "local", "command": "uname -a", "timeoutSec": 30, "cwd": "E:/data"}'
# Linux 执行点：cwd 换成 "/srv/app"，command 按那台机器的 shell 写
```

- `node` 取 `local`（本机）或 `GET $D/api/agent/hosts` 里的主机名；缺省 `local`。
- `node` 与 `host` 同义：`host` 是旧名，仍接受，但请用 `node`；两个都给且不同 → 400。
- `cwd` 按目标机器的 OS 书写（见上）；目录不存在时命令直接失败，响应原样回显该路径。
- 本机执行点可带 `shell`：`bash` / `powershell` / `pwsh`，从 `GET /api/agent/hosts` 里 `local` 条目的 `shells` 挑，缺省 `defaultShell`（本机即 `bash`）。
- `timeoutSec` 1–600；输出上限 2 MiB；命令不允许含 NUL。
- 响应：`{"exit": 0, "out": "...", "err": "...", "timedOut": false, "reason": "", "pid": 123, "node": "local", "host": "local"}`；本机执行点另回 `shell` 与 `cwd`。
- 超时返回 `exit: null`、`reason: "timeout"`（被超时杀掉的进程可能仍有 `pid`）。

### 文件：POST /api/agent/sftp/{list,get,put}

> **路径同样按目标机器的 OS 写**（`os` 怎么查见「主机清单」）：Windows 用 `E:/data`，Linux 用 `/srv/app`；`path` 是文件或目录的绝对路径。

```bash
# 列目录（Windows 本机）
curl -s -X POST "$D/api/agent/sftp/list" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"node": "local", "path": "E:/data"}'
# Linux 执行点：path 换成 "/srv/app"

# 读文件（offset/length 分块；整读上限 8 MB）
curl -s -X POST "$D/api/agent/sftp/get" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"node": "local", "path": "E:/data/notes.txt", "offset": 0, "length": 65536}'

# 写文件（严格校验 base64；原子落盘；mkdirs 自动建父目录）
curl -s -X POST "$D/api/agent/sftp/put" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"node": "local", "path": "E:/data/notes.txt", "contentB64": "aGVsbG8=", "mkdirs": true}'
```

- 指代机器的参数一律用 `node`（缺省 `local`）；`host` 是旧名，仍接受。
- `list` → `{"path", "entries": [{"name", "path", "size", "mtime", "dir", "type", "mode", "target"}]}`。
- `get` → `{"contentB64": "...", "total": 12345}`；`total > 已读长度` 时用更大的 `offset` 继续。
- `put` 可选 `sha256`（内容摘要，不符即 400、原文件不动）；`contentB64` 是坏编码时 400；目标目录不存在时 404（本机可传 `"mkdirs": true`）。

### 交文件换 ticket：POST /api/agent/share/stage

multipart，`file` 0–10 个，`prompt` 可选。返回 `{"ticket", "files": [{"name","size"}], "expiresAt"}`。
这个 ticket 是给网页分享表单后续提交用的，本身不能直接建任务。

```bash
curl -s -X POST "$D/api/agent/share/stage" -H "X-Agent-Token: $T" \
  -F "prompt=看看这份表格" -F "file=@report.xlsx"
```

如果目标是**直接用令牌把文件交给执行方并建任务/追消息**，用同一 scope 的 `POST /api/agent/share`；
它不需要 ticket，返回 `{"taskId", "mode": "created"|"appended", "paths": [...]}`：

```bash
curl -s -X POST "$D/api/agent/share" -H "X-Agent-Token: $T" \
  -F "prompt=看看这份表格" -F "file=@report.xlsx" -F "node=local" -F "mode=auto"
```

## 最小 Python 客户端（只用标准库）

```python
import json
import urllib.request


class Deskport:
    def __init__(self, base, token, timeout=600):
        self.base, self.token, self.timeout = base.rstrip('/'), token, timeout

    def call(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header('X-Agent-Token', self.token)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))


dp = Deskport('{{BASE}}', '<令牌>')
task = dp.call('POST', '/api/tasks', {'prompt': '修掉 tests/ 里的 3 个失败用例',
                                      'node': 'local', 'cwd': 'E:/data',  # Windows；Linux 用 '/srv/app'
                                      'mode': 'auto'})['task']
after = 0
while True:
    r = dp.call('GET', '/api/tasks/%s/wait?after=%d&timeout=300' % (task['id'], after))
    after = r.get('lastSeq', after)
    if r.get('pending'):
        p = r['pending']
        print('需要审批:', p.get('name'), p.get('options'))
        break
    if r.get('status') != 'running':
        print(r.get('finalText'))
        break
```

## 错误码

- `401`：令牌缺失或无效。确认请求头是 `X-Agent-Token: <令牌>`，且令牌仍由部署者签发。
- `403`：令牌 scope 不够，响应带 `{"need": "<所需 scope>"}`；让部署者重发对应 scope 的令牌。
- `400`：参数不合法（例如不认识 `node`、非本机执行点缺 `cwd`、`cwd` 不在执行点允许的根下、`prompt` 过长）。
- `409`：当前状态不允许（例如任务已结束或已关闭、审批已失效、持久会话正忙）。按响应提示调整后重试。
- `429`：并发任务达到上限，响应带 `{limit, running, holders}`；先看 `holders` 是谁在占，close 掉自己的旧任务再重试。
- `404` + `availableHosts`：`node` 名字不对。用 `GET /api/agent/hosts` 里那个名字（`local` 是本机），别自己拼。
- `400` + `node 与 host 冲突`：两个都给了。只给 `node`，`host` 是旧名。
- `SSL: CERTIFICATE_VERIFY_FAILED` / `curl exit 60`：公网入口是隧道方的自签证书。`curl` 加 `-k`；`deskport.py` 在 `.deskport.json` 写 `"insecure": true` 或 `"cafile"` 指到证书文件。
- `501`：拿 `http://` 打了隧道端口（隧道只收 TLS）。换 `https://`。
- `cd: No such file or directory`：路径按错操作系统的写法了。Windows 用 `E:/data`，Linux 用 `/srv/app`；先看 `GET /api/agent/hosts` 的 `os` 字段。
- `outTruncated: true` / 输出被截断：单次输出上限 2 MiB。缩小输出（`| tail -n 100`）或分块读文件。

## 约定

- 命令与任务都真实执行：优先只读；写文件、装软件、动服务之前先向你的用户说明。
- 令牌等价于执行权，不要写进公开脚本、仓库或日志。
- 名字写错时，响应会带 `availableHosts` / `nodes` / `models` 等提示，直接改用。
- 指代机器的参数用 `node`（exec / sftp / 会话与任务同一套词汇，缺省 `local`）；
  `host` 是旧名，仍接受，但请用 `node`。
- 如果你恰好是 Kimi Code，上面整包（`skill.zip`）解开就是标准技能目录，放进 `~/.kimi-code/skills/` 即可当技能用；它不是使用本手册的前提。