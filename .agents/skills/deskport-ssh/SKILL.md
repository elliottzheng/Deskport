---
name: deskport-ssh
description: 通过 Deskport 智能体 API 在已保存的远程主机上执行命令、巡检状态、读写文件。当任务需要在远程主机（而非本机）上执行命令、查看服务状态、读写远程文件时使用。认证用长效令牌（部署时签发，存放位置见部署文档），令牌等价于远程执行权，勿外传。
---

# deskport-ssh

通过 Deskport 的智能体 API 操作远程主机。所有请求带 `X-Agent-Token` 头。

## 速查

- API base: 部署 Deskport 的地址（本机部署为 `http://127.0.0.1:6789`；外网走你给 Deskport 配的 HTTPS 隧道地址）
- 认证: `X-Agent-Token: <令牌>`；令牌由部署者在 `config.json` 的 `agentTokens[]` 签发（存 SHA-256 指纹，明文只给智能体一次）
- 主机: 用名称或 id；`GET /api/agent/hosts` 可列出（只含 id/name/host/port，无凭据）

## 端点

### 执行命令（无状态，单发快命令）

```text
POST /api/agent/ssh/exec
{"host": "<主机>", "command": "uptime", "timeoutSec": 30, "cwd": "/path"}
→ {"exit": 0, "out": "...", "err": "...", "timedOut": false}
```

- exec 无状态：cd 不延续。多步任务用 `cwd` 参数（每条都带），或用持久会话（见下，推荐）。
- 输出上限 2MB；`outTruncated: true` 表示被截断。超时上限 600s。
- 超时只关闭通道并返回已收到的输出，远端进程不保证终止——长命令改用
  `nohup xxx > /tmp/xxx.log 2>&1 &` 后台化再轮询日志。

### 持久会话（cd / export / venv 激活都延续，多步任务推荐）

```text
POST   /api/agent/ssh/sessions              {"host": "<主机>", "cwd": "/path"} → {"sessionId"}
POST   /api/agent/ssh/sessions/<sid>        {"command": "...", "timeoutSec": 30}
DELETE /api/agent/ssh/sessions/<sid>
```

- 同一会话里 cd / export / source 都延续（单 bash 进程常驻）。
- 响应字段：`exit`（退出码）、`out`（末尾 8KB）、`err`、`cwd`、`alive`。
- `combined: true` 把 stderr 并入 out；`full: true` 返回全量输出。
- 完成检测由宿主自动处理（就绪标记自动剔除），`out` 不含噪音。
- 命令卡死超时会话可能残留状态，建议 `close` 重开。

### SFTP（文件读写，8MB 上限）

```text
POST /api/agent/sftp/list  {"host": "<主机>", "path": "/path"} → {entries: [{name, path, size, mtime, dir}]}
POST /api/agent/sftp/get   {"host": "<主机>", "path": "..."}  → {contentB64}  (base64)
POST /api/agent/sftp/put   {"host": "<主机>", "path": "...", "contentB64": "..."}
```

## 模板 (Python + requests)

```python
import requests
TOKEN = open(r'<令牌文件路径>', encoding='utf-8').read().strip()
base = 'http://127.0.0.1:6789'
H = {'X-Agent-Token': TOKEN}

def run(cmd, cwd=None, timeout=30):
    payload = {'host': '<主机>', 'command': cmd, 'timeoutSec': timeout}
    if cwd:
        payload['cwd'] = cwd
    r = requests.post(base + '/api/agent/ssh/exec', headers=H,
                      json=payload, timeout=timeout + 10)
    r.raise_for_status()
    return r.json()['exit'], r.json()['out']
```

## 规矩

- 命令在远程主机上真实执行：优先只读命令；写操作（写文件、装软件、动服务）先向用户说明再执行。
- 主机名写错时响应带 `availableHosts` 提示，直接改用即可，不用再列主机。
- 401 = 令牌失效：检查令牌文件与 config.json 的 `agentTokens` 指纹是否匹配。
- 审计全留痕（agent.exec / agent.sftp.*），部署者可在控制台回看每一次执行。
- 完整接口参考：仓库内 `docs/agent-api.md`。
