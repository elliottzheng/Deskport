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

## 审计

每次调用写 `events` 流（`agent.exec` / `agent.sftp.list|get|put`），
可在控制台审计里回看：哪个令牌、哪台主机、执行了什么。

## 已知边界

- exec 是非 pty 通道：无交互式程序（vim/top），需要交互请用浏览器终端；
- 超时掐断只关闭通道，远端进程是否残留取决于命令本身；
- 令牌即远程执行权：泄露请立即撤销（删除 config 条目并重启）。
