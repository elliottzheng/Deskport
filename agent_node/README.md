# Agent Node — 远端智能体节点

在任意 Linux/macOS/Windows 主机上托管 `kimi acp` 会话并暴露 HTTP API，
让 Deskport（或其他调用方）把任务派到远端执行。纯标准库，零第三方依赖。

## 部署

```bash
# 1. 目标机需已安装并登录 kimi CLI（kimi --version 可用）
# 2. 拷贝 agent_node.py 到目标机任意目录
# 3. 生成 token 并启动（端口按需）
python3 agent_node.py --port 8622 --token <TOKEN> --kimi ~/.kimi-code/bin/kimi
```

会话存档写在 `~/.deskport-agent-node/sessions.json`；日志自管（建议重定向）。

## Deskport 侧接入

`config.json`：

```json
"agentNodes": [
  {"name": "ai-xmyz", "url": "http://127.0.0.1:8622",
   "token": "<TOKEN>", "via": "<SSH主机id>", "remote": "127.0.0.1:8622"}
]
```

- 直连可达时省略 `via`/`remote`。
- node 端口被防火墙过滤时填 `via`（SSH 主机 id 或名称）：Deskport 经 SSH
  direct-tcpip 隧道转发 HTTP，token 照常生效，无需改防火墙。

## 开机自启（Linux 无 sudo 时）

追加到用户 crontab（`crontab -e`）：

```
@reboot sleep 10 && cd <部署目录> && /usr/bin/python3 agent_node.py \
  --port 8622 --token <TOKEN> --kimi /home/<user>/.kimi-code/bin/kimi \
  >> <部署目录>/node.log 2>&1
```

SSH 远程拉起时用 `setsid`，避免会话退出连带杀掉进程。

## API 一览（除 /health 外均需 X-Agent-Token）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 健康检查（免认证） |
| GET | /api/sessions | 会话列表（含归档） |
| POST | /api/sessions | 建会话 `{cwd, mode, sessionId?}`，带 sessionId 即 resume |
| GET | /api/sessions/<sid> | 会话元信息 |
| POST | /api/sessions/<sid>/prompt | 派活 `{text}`，202 受理 |
| GET | /api/sessions/<sid>/events?since=N | 增量事件流（update/权限/turn_end） |
| POST | /api/sessions/<sid>/permission | 权限决策 `{requestId, optionId}` |
| POST | /api/sessions/<sid>/mode | 切换批准模式 `{mode}` |
| POST | /api/sessions/<sid>/cancel | 中断当前回合 |
| DELETE | /api/sessions/<sid> | 关闭并移除会话进程 |

语义要点：

- 事件 seq 单调递增，`replay: true` 标记 resume 重放的历史，调用方可跳过展示。
- 同一 kimiSessionId 重复建会话时复用存活进程（多端接手同一会话）。
- 空闲 45 分钟进程自动回收；kimiSessionId 已存档，随时可 resume 接续上下文。
- 权限请求 900 秒无人决策自动取消。
