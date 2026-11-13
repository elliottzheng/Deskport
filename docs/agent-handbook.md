# Deskport Agent Handbook

Deskport 是一台电脑上的远程执行与任务托管服务：你可以让它在宿主机器（以及它已保存的 SSH 主机）上执行命令、读写文件，或把多步任务委托给它托管的智能体。
你只需要一个 HTTP 客户端：不需要安装 CLI 或 SDK，也不需要是 Kimi 或其他特定品牌的智能体。
令牌由部署 Deskport 的人另外发给你，每次请求放在请求头 `X-Agent-Token` 里（也接受 `Authorization: Bearer <令牌>`）。

- API base：{{BASE}}
- 手册版本：{{VERSION}}
- 本手册是公开的，不含任何密钥；可以随时重读。程序化读取用 `GET {{BASE}}/api/agent/handbook?format=json`，返回 `{version, base, markdown}`。
- 令牌可能被收窄到某些 scope（例如只给 `share`）；权限不够时 403 会带 `need`，让部署者补发即可。

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

## 端点

### 执行点：GET /api/tasks/nodes

返回 `{"nodes": [{"name", "via", "kind", "models", "defaultModel", "modelSelectable"}]}`。
`name` 就是建任务时 `node` 的取值；`local` 是本机。`models` 可选模型由执行点上报。

```bash
curl -s -H "X-Agent-Token: $T" "$D/api/tasks/nodes"
```

### 建任务：POST /api/tasks

```bash
curl -s -X POST "$D/api/tasks" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"prompt": "修掉 tests/ 里的 3 个失败用例", "node": "local", "cwd": "/srv/app", "mode": "auto"}'
```

- `prompt` 必填，上限 4000 字符。
- `node` 是执行点（去哪台机器），缺省 `"local"`。**只认 `node` 与 `cwd`；旧字段 `engine` 已移除，传了直接 400。**
- `cwd` 是执行目录：本机是可写的项目目录（Windows 用正斜杠，如 `E:/work/project`）；非本机执行点必填，且目录须存在于那台机器。
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

### 执行命令：POST /api/agent/ssh/exec

单发、无状态（`cd` 不延续）；需要多步状态用任务的 `cwd`，或反复带 `cwd`。

```bash
curl -s -X POST "$D/api/agent/ssh/exec" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"host": "local", "command": "uname -a", "timeoutSec": 30, "cwd": "/srv/app"}'
```

- `host` 取 `local`（本机）或 `GET $D/api/agent/hosts` 里的主机名。
- `timeoutSec` 1–600；输出上限 2 MiB；命令不允许含 NUL。
- 响应：`{"exit": 0, "out": "...", "err": "...", "timedOut": false, "reason": "", "pid": 123}`。
- 超时返回 `exit: null`、`reason: "timeout"`（被超时杀掉的进程可能仍有 `pid`）。

### 文件：POST /api/agent/sftp/{list,get,put}

```bash
# 列目录
curl -s -X POST "$D/api/agent/sftp/list" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"host": "local", "path": "/srv/app"}'

# 读文件（offset/length 分块；整读上限 8 MB）
curl -s -X POST "$D/api/agent/sftp/get" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"host": "local", "path": "/srv/app/app.py", "offset": 0, "length": 65536}'

# 写文件（严格校验 base64；可选 sha256；原子落盘）
curl -s -X POST "$D/api/agent/sftp/put" \
  -H "X-Agent-Token: $T" -H "Content-Type: application/json" \
  -d '{"host": "local", "path": "/srv/app/notes.txt", "contentB64": "aGVsbG8=", "sha256": "<可选>"}'
```

- `list` → `{"path", "entries": [{"name", "path", "size", "mtime", "dir", "type", "mode", "target"}]}`。
- `get` → `{"contentB64": "...", "total": 12345}`；`total > 已读长度` 时用更大的 `offset` 继续。
- `put` 的 `contentB64` 是坏编码时 400，原文件不动；目标目录不存在时 404（本机可传 `"mkdirs": true`）。

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
                                      'node': 'local', 'cwd': '/srv/app',
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
- `400`：参数不合法（例如不认识 `node`、非本机执行点缺 `cwd`、`prompt` 过长、传了旧字段）。
- `409`：当前状态不允许（例如任务已结束或已关闭、审批已失效、持久会话正忙）。按响应提示调整后重试。
- `429`：并发任务达到上限，响应带 `{limit, running, holders}`；退避后重试。

## 约定

- 命令与任务都真实执行：优先只读；写文件、装软件、动服务之前先向你的用户说明。
- 令牌等价于执行权，不要写进公开脚本、仓库或日志。
- 名字写错时，响应会带 `availableHosts` / `nodes` / `models` 等提示，直接改用。
- 如果你恰好是 Kimi Code，同一台机器上另有可选的技能包 `GET {{BASE}}/api/agent/skills/deskport/pack.zip`（需令牌）；它不是使用本手册的前提。