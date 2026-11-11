# Agent Node — 远端智能体节点

在任意 Linux/macOS/Windows 主机上托管 `kimi acp` 会话并暴露 HTTP API，
让 Deskport（或其他调用方）把任务派到远端执行。纯标准库，零第三方依赖。

**Python ≥ 3.11**（模型自动发现依赖 tomllib）；入口有版本守卫，老解释器
会拒绝启动。建议用 uv：`uv python install 3.13` 后以 `uv python find 3.13`
给出的解释器路径启动。

## 部署

```bash
# 1. 目标机需已安装并登录 kimi CLI（kimi --version 可用）
# 2. 拷贝本目录（agent_node/，共 3 个 py 文件）到目标机任意目录
# 3. 建令牌文件（R4-6：令牌绝不进进程参数；也可用环境变量 AGENT_NODE_TOKEN）
echo -n "<令牌>" > ~/.agent_node_token && chmod 600 ~/.agent_node_token
# 4. 启动
python3.11 agent_node.py --port 8622 --token-file ~/.agent_node_token \
  --kimi ~/.kimi-code/bin/kimi
```

### 在 ai-xmyz 上从 --token 切换到 token-file（≤10 行，人工执行）

```bash
# 1. 写令牌文件（内容与现用 --token 一致，无需换令牌）
echo -n '2FKshjYnIsVBDr8LV44SMl6vqDN1nuAS' > ~/deskport-agent-node/token && chmod 600 ~/deskport-agent-node/token
# 2. 记下现进程 PID 并杀掉
pgrep -f '^python3 agent_node.py'   # 记下输出
kill <PID>
# 3. 用 token-file 重启（注意用 ~/py3.13/bin/python3，见上方版本要求）
cd ~/deskport-agent-node && setsid nohup ~/py3.13/bin/python3 agent_node.py \
  --port 8622 --token-file ~/deskport-agent-node/token \
  --kimi ~/.kimi-code/bin/kimi >> node.log 2>&1 < /dev/null &
# 4. 验证：健康正常，且参数里再无明文令牌
curl -s http://127.0.0.1:8622/health
ps -eo args | grep -c -- '--token '   # 期望 0
```

- 会话存档写在 `~/.deskport-agent-node/sessions.json`；日志自管（建议重定向）。
- `--token-file <路径>`：从文件读令牌，文件不存在则以随机值原子创建——
  Deskport 侧与本机 node 共享令牌就用这个（本机部署时 Deskport 读
  `~/.deskport-agent-node/token`，见 `config.getAgentNodeToken`）。

### 升级远端 node（合并 token-file 切换，单次重启）

```bash
# 1) 同步代码（本机）：scp acp_session.py node_api.py agent_node.py 到部署目录
# 2) 解释器：~/py3.13/bin/python3（≥3.11，tomllib 必需；新代码仅用 stdlib）
# 3) token 保号：把当前 --token 明文写入 token 文件（Deskport 侧配置不变）
echo "<当前token明文>" > ~/.deskport-agent-node/token
# 4) 显式 PID 重启（勿用 pkill -f 模糊匹配，会误杀外层 shell）
kill $(pgrep -f '^python3.*agent_node.py')
cd <部署目录> && setsid nohup ~/py3.13/bin/python3 agent_node.py --port 8622   --token-file ~/.deskport-agent-node/token   --kimi ~/.kimi-code/bin/kimi >> node.log 2>&1 < /dev/null &
# 5) 验证：/health 返回 models/defaultModel，/api/sessions 列表正常
```

升级会终止 node 托管的 kimi 进程（打断运行中回合）；任务在下一条
message 时自动 session/load 接续（R4-5 余项 B），选对方任务 waiting
的时机执行可完全无损。

## Deskport 侧接入

## Deskport 侧接入

`config.json`：

```json
"agentNodes": [
  {"name": "ai-xmyz", "url": "http://127.0.0.1:8622",
   "token": "<TOKEN>", "via": "<SSH主机id>", "remote": "127.0.0.1:8622"}
],
"localAgentNodePort": 8622
```

- 直连可达时省略 `via`/`remote`。
- node 端口被防火墙过滤时填 `via`（SSH 主机 id 或名称）：Deskport 经 SSH
  direct-tcpip 隧道转发 HTTP，token 照常生效，无需改防火墙。
- `localAgentNodePort`：本机 agent node 端口（>0 启用）。启用后 engine=acp
  的任务也由本机 node 托管——web 重启任务无感存活；置 0 回退 web 内托管。
- Windows 本机部署：仓库根的 `start-agentnode.vbs` + watchdog（`anode` 检查项）
  负责常驻与自愈。

## 开机自启（Linux 无 sudo 时）

追加到用户 crontab（`crontab -e`）：

```
@reboot sleep 10 && cd <部署目录> && <Python≥3.11 的绝对路径> agent_node.py \
  --port 8622 --token-file ~/.deskport-agent-node/token \
  --kimi /home/<user>/.kimi-code/bin/kimi >> <部署目录>/node.log 2>&1
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
| GET | /api/sessions/<sid>/events?since=N | 增量事件快照（一次性返回） |
| GET | /api/sessions/<sid>/events/stream?since=N | SSE 长连接推送（25s 心跳，断开以 since 续传） |
| POST | /api/sessions/<sid>/permission | 权限决策 `{requestId, optionId}` |
| POST | /api/sessions/<sid>/mode | 切换批准模式 `{mode}` |
| POST | /api/sessions/<sid>/cancel | 中断当前回合 |
| DELETE | /api/sessions/<sid> | 关闭并移除会话进程 |

SSE 流格式：`data: {事件json}` 逐条推送；`: keepalive` 心跳行；
`event: end` 表示进程已退出且事件推完（客户端可停止重连）。

语义要点：

- 事件 seq 单调递增，`replay: true` 标记 resume 重放的历史，调用方可跳过展示。
- 同一 kimiSessionId 重复建会话时复用存活进程（多端接手同一会话）。
- 空闲 45 分钟进程自动回收；kimiSessionId 已存档，随时可 resume 接续上下文。
- 权限请求 900 秒无人决策自动取消。
- 协议实现与 Deskport 内置客户端（KimiAcp）由 `tests/test_acp_parity.py`
  对拍测试保证语义一致。
