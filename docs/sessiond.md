# 会话守护进程（终端 / SSH 分治）

解决"重启 web 就丢掉终端和 SSH"的问题：把会话从 web 进程里搬出来，拆成两个独立的小服务。
web 只做认证、代理和 UI，重启它不再影响正在跑的 pty / SSH channel。

## 一、架构

```
浏览器 ──WS──> web(6789, 认证/UI/文件/控制台) ──代理──> locald(127.0.0.1:6791, ConPTY)
                                              └──代理──> sshd  (127.0.0.1:6792, paramiko)
```

- **locald**：只加载 `termcore` + `terminal`，管理本机终端会话。
- **sshd**：只加载 `termcore` + `ssh`，管理 SSH 会话与连接注册表。
- 两个进程、两个端口、两个令牌、两份日志，互不依赖；重启其中一个不影响另一个。
- web 与守护进程用 `%APPDATA%\<stateDir>\sessiond_token_local|ssh` 共享令牌，守护进程只监听 127.0.0.1。

## 二、配置

```json
{
  "sessiondUrl": "http://127.0.0.1:6791",
  "sshSessiondUrl": "http://127.0.0.1:6792"
}
```

- 任意一项为空 → 对应会话回到 web 进程内运行（旧行为），可单独启用。
- 守护进程不在线 → 对应会话列表返回 503，WS 收到 error 帧；另一个服务不受影响。

## 三、启动

```bash
python sessiond.py local    # locald
python sessiond.py ssh      # sshd
```

仓库根提供两个隐藏启动脚本：`start-locald.vbs`、`start-sshd.vbs`（各自做端口占用检查）。
`watchdog.py` 每 30 秒检查两个守护进程与 web，连续 2 次失败用对应 vbs 拉起；
日志在启动脚本里按 10MB 轮转一次。

自启统一由 `start-hidden.vbs` 负责：先拉起 locald / sshd / watchdog，再启动 web。
只需在启动文件夹保留一个指向该脚本的快捷方式。

## 协议版本与内部契约

- `hello` 帧带 `proto: 1` 与 `caps: ['replay','preempt','resize']`；web 代理原样透传。
  web 与守护进程可以分开升级：新前端连旧守护时，未知能力按缺省处理。
- 所有内部路径、令牌头、健康检查、SSH disconnect 收敛在 `modules/internal_api.py`；
  web 与守护进程都不再互相 import 实现模块。

## 四、web 侧代理

| 浏览器路径 | 代理到 | 说明 |
|---|---|---|
| `/api/terminal/ws` | locald `/ws/terminal` | WS 双向透传，帧协议不变 |
| `/api/terminal/sessions`、`DELETE /<id>` | locald `/sessions` | 列表与关闭 |
| `/api/ssh/ws` | sshd `/ws/ssh` | WS 双向透传 |
| `/api/ssh/sessions`、`DELETE /<id>` | sshd `/sessions` | 列表与关闭 |
| `/api/ssh/disconnect` | sshd `/disconnect` | 结束该主机的全部 SSH 会话 |

浏览器不直连守护进程，frp 与登录鉴权保持原样。

## 五、能活过什么

| 场景 | 结果 |
|---|---|
| 重启/发布 web | 会话存活；页面 WS 断 2–5 秒后自动重连并回放 |
| 只重启 locald | 本机终端断，SSH 不受影响 |
| 只重启 sshd | SSH 断，本机终端不受影响 |
| 重启某个守护进程本身 | 该服务的会话断（维护窗口） |
| 关机 / 注销登录 | 全部断（ConPTY 绑定登录会话） |

## 六、实测

- 终端：页面内跑 `1..600 | % { "TICK$_"; Start-Sleep 1 }`，重启 web 后 `已连接`，TICK 从 4 继续到 53，同一 session id，`attached=true`。
- SSH：远端 `while true; do echo TICK; sleep 1; done`，重启 web 后 TICK 从 6 继续到 51，同一 session id。
- 隔离：重启 locald 后 sshd 会话仍在；重启 sshd 后 locald 会话仍在。
- SFTP：不经过 sshd，web 自己建连接，重启 web/守护进程均不受影响。
- 故障：停掉 locald，`/api/terminal/sessions` 返回 503 且 SSH 列表正常。

## 七、已知限制

- WS 代理按 0.05 秒轮询转发，单会话开销很小；会话非常多时可改成阻塞式 select。
- 守护进程需要在用户登录会话里启动（ConPTY 依赖桌面会话），与 web 相同。
- 守护进程本身升级需要重启它，属于唯一需要挑时间的维护窗口。
- 目前没有守护进程崩溃自动拉起；可后续加计划任务 watchdog。