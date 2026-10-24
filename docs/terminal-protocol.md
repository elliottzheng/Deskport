# 终端 WebSocket 帧协议

本文是 `WS /api/terminal/ws`（服务端 `modules/terminal.py`，前端 `frontend/assets/js/term-client.js`；`terminal.js` / `ssh-page.js` 为页面装配层）帧协议的唯一权威描述，覆盖：连接参数与握手、全部帧类型、回放语义、抢占接管、会话保活与回收、四条硬约束。
本文不覆盖：会话列表 REST 端点（见 [api.md](api.md)）、线程模型的实现细节（见 [architecture.md](architecture.md)）。

协议语义自早期版本忠实移植，有实测背书，**已冻结**：任何改动必须保持外部行为一致并过回归清单（多会话、断线重连、刷新回放、抢占接管、输出爆发期刷新）。

## 连接

```
GET ws(s)://<host>:6789/api/terminal/ws?cmd=&cwd=&session=&replay=
```

| 参数 | 缺省 | 说明 |
|---|---|---|
| `cmd` | `pwsh.exe`（PATH 无则 `cmd.exe`） | 终端程序；服务端以 `TERM=xterm-256color` 环境变量、初始 24 行 x 80 列 spawn |
| `cwd` | 用户主目录 | 初始工作目录 |
| `session` | 无 | 既有会话 ID；命中且 `alive` 则附着复用（`resumed=true`），否则新开会话 |
| `replay` | 无 | `=1` 且本次为复用时，先回放历史输出再接实时流 |

握手前过登录守卫：未认证的升级请求被 401 拒绝，浏览器表现为 close code 1006。

会话标识为 32 位十六进制（`uuid4().hex`），在服务端 `hello` 帧下发，前端不得自行构造。

## 帧协议总览

连接为单条 WebSocket，文本帧双通道复用：**能被解析为带 `type` 字段的 JSON 对象即控制帧，否则一律视为原始输入/输出字节**。服务端判定逻辑在 `termcore._decodeFrame`：JSON 解析失败、或解析结果不是带 `type` 的 dict，都按原始文本处理——因此终端程序输出合法 JSON 文本也不会被误当控制帧。

### 前 → 后

| 帧 | 形态 | 语义 |
|---|---|---|
| 原始输入 | 文本帧，UTF-8 | xterm.js `onData` 的原样按键序列（含 ESC/方向键等 VT 序列）。服务端把 `\r\n`、`\n` 统一转 `\r` 再写入 pty（Windows 控制台回车约定），进程已退出时静默丢弃 |
| resize | `{"type":"resize","cols":<int>,"rows":<int>}` | 调整 pty 窗口为 `setwinsize(rows, cols)`；`cols`/`rows` 缺省 80/24；进程不存在时忽略；转换失败静默忽略 |
| ping | `{"type":"ping"}` | 心跳，服务端原样回 pong |
| close | `{"type":"close"}` | **主动销毁会话**：杀 pty 进程树并从会话表注销。与"直接断开 WS"（进程保活）语义相反，前端关会话按钮走此帧 |

### 后 → 前

| 帧 | 形态 | 语义 |
|---|---|---|
| 原始输出 | 文本帧 | pty 的 VT 输出流，前端直接 `term.write()` |
| hello | `{"type":"hello","session":<id>,"resumed":<bool>,"label":<str>,"kind":"local"/"ssh","cwd":<str|null>,"cwdLabel":<str>,"foreground":<str>,"foregroundCwd":<str>,"foregroundCwdLabel":<str>,"proto":<int>,"caps":[<str>]}` | 握手完成、附着生效。字段形状以 `modules/contracts.py` 的 `HelloFrame` 为准。前端据此记录会话 ID；`resumed=false` 时 `term.reset()` 清屏（新会话）；`true` 时保留（复用/回放）。收到 hello 后前端立即发送一次 resize 同步真实尺寸 |
| exit | `{"type":"exit","code":<int|null>}` | 终端进程已退出，携带退出码（拿不到时为 `null`）。服务端发完即 close；前端显示灰字 `[进程已退出 code=...]` 并把会话置为 ended，不再重连 |
| error | `{"type":"error","msg":"<文本>"}` | 致命错误，出现即终止本次连接：`仅支持 Windows 环境`（非 Windows 平台）、`终端启动超时`（spawn 15 秒未就绪，含 spawn 失败）、`启动失败: <原因>`（主循环消费到 spawn 失败队列项）。前端显示红字并把会话置为 ended |
| pong | `{"type":"pong"}` | ping 的应答，前端忽略 |

## 连接时序

服务端 `wsLocalTerminal`/`wsSshTerminal` 汇入 `termcore.attachAndPump`，按序：

1. **解析参数、定位会话**：`session` 参数命中存活会话则复用，否则新建（spawn 在 reader 线程异步进行）。
2. **附着**：`session.attached = ws`（单附着，见"抢占接管"）。
3. **等待 spawn 就绪**：最多 15 秒。超时发 `error("终端启动超时")` 并封套退出——期间不收任何输入，避免丢字。
4. **回放准备**（仅 `resumed` 且 `replay=1`）：对 `replayBuf` 做快照，然后排空 `outQueue`（积压是快照的子集，见下节）。
5. **发 hello** → 逐块回放快照 → 进入主循环接实时流。
6. **主循环**（每轮）：检查附着权（失权即退出，不杀进程）→ 排空 `outQueue` 逐帧转发（`error`/`exit` 队列项转为对应 JSON 帧后 close）→ `ws.receive(timeout=0.05)` 收输入帧处理。
7. **退出**（无论何因）：`_detach`（仅当前附着者本人可摘除）+ `_sealSocketAfterClose` 封套。

## 回放语义

目标：页面刷新后重建终端画面（滚动历史、光标、备用屏状态都随输出序列恢复），且**不重不丢**。

服务端保证顺序的关键在 `TermSession.enqueueOut`：每个输出块**先 append 进 `replayBuf`，再 put 进 `outQueue`**，因此任意时刻"队列积压 ⊆ 回放快照"。重连时执行"快照 → 清积压 → hello → 回放 → 主循环"：

```
snapshot = list(session.replayBuf)     # 定格历史
排空 outQueue                           # 丢弃与快照重复的积压
ws.send(hello)
for chunk in snapshot: ws.send(chunk)   # 回放历史
主循环从 outQueue 接实时流               # 与回放天然无缝
```

参数与边界：

- `REPLAY_MAX = 512 * 1024`：回放缓冲按字节封顶，超限丢弃最旧块——超长滚动区最旧部分不回放，但当前屏必在缓冲尾部。
- 全屏 TUI 回放后由前端 resize 触发重绘，最终画面以重绘为准。
- 前端用一次性标志 `needReplay` 控制仅在"页面恢复出的全新空终端"上带 `&replay=1`；hello 到达即消费该标志，同页后续断线重连不回放，避免画面重复。

## 抢占接管

同一会话**单附着**：新 WS 连接在第 2 步直接覆写 `session.attached = ws`，不通知旧连接、不跨线程 close 旧 socket。

**回放结束标记 (2026-10-18)**：带 `replay=1` 的附着在回放快照发送完毕后，服务端额外下发 `{type:'replay-end'}` 控制帧；客户端在此之前丢弃全部本地输入——回放期间 ConPTY 会发终端能力查询（OSC 颜色/DA/XTGETTCAP），xterm.js 的自动应答若照常上行会落进 shell 输入行成为乱码（TERM-BUG-01）。此机制取代旧的"hello 后 1.5s 定时解除"（frp 高延迟下回放远超 1.5s，定时不可靠）。

**抢占通知 (2026-10-18)**：新附着发生时，旧 handler 失权退出前会向旧 ws 下发 `{type:'preempted'}` 控制帧并主动 close。前端收到该帧后**停止自动重连**（否则两台设备会陷入 1s 级互踢死循环），状态转"已断开"并提示"会话已在其他窗口打开，点击会话可重新接管"；点击侧栏会话即手动重新附着(再次抢占, 对称成立)。会话恢复(/api/ssh/sessions → 恢复逻辑)亦不再抢附 `attached=true` 的会话。

旧 handler 靠主循环每轮的失权检测（`session.attached is ws` 为假）自行退出，进程**继续存活**、服务无感切换——旧连接退出时只做 detach（因非附着者，实际无操作）与封套。前端配套防抖：同一会话 `readyState <= 2` 时禁止重复建连（`term-client.js` `connectWs` 守卫），消除同页误抢占的触发源。

## 保活与回收

| 参数 | 值 | 语义 |
|---|---|---|
| 前端 ping 周期 | 15 秒 | `{"type":"ping"}`，防中间设备闲置断链；pong 被忽略 |
| `terminalIdleTimeoutHours` | 0（缺省，永不回收） | 无人附着的宽限期，单位小时；0 = 会话像 tmux 一样常驻 |
| `REAPER_INTERVAL` | 10 秒 | 回收线程扫描周期（每轮重读配置，改 config.json 即时生效） |
| `MAX_BACKLOG` | 20000 条 | 无人附着时 `outQueue` 上限，入队时丢最旧 |
| `REPLAY_MAX` | 512 KB | 回放缓冲上限，丢最旧 |

会话生命周期：

```
新建 ──spawn──> 存活 ──WS 断开──> 存活(后台, 输出入队/写回放缓冲)
                  │                     └──重连附着──> 存活(附着)
                  │                     └──空闲超宽限期(默认不回收)──> reaper 杀进程+注销
                  └──exit 帧──> 注销
close 帧 ──────────────────────> 杀进程 + 注销 (任何状态)
```

## 硬约束（实测教训，不可违背）

1. **spawn 与 read 必须同线程**。pywinpty 有线程亲和性：`PtyProcess.spawn` 与后续 `read()` 必须在同一条 reader 线程，跨线程会挂死/丢输出。
2. **`ws.send` 全部收敛到附着主循环线程**。多线程并发 `ws.send` 会交错帧字节；输出一律经 `outQueue` 由主循环独占发送，其余位置（pong、error、hello）也在 handler 线程内发出。
3. **pywinpty 的空串返回是内部忽略帧，不是 EOF**。`read()` 返回空字符串必须 `continue`；真正退出以空 `bytes`（或异常）为据，退出码从 `proc.exitcode` 取。
4. **handler 退出必须 `_sealSocketAfterClose` 封套**。werkzeug 在 handler 正常 return 时会向已升级 socket 补写 HTTP 200 响应头，被浏览器当帧解析即 `Invalid frame header`/1006。封套（发 close 帧 + `shutdown(SHUT_WR)`）使补写必然 `BrokenPipeError` 被 werkzeug 静默吞掉。

## 前端重连行为

`term-client.js`：非人工关闭的断线按指数退避重连（1s 起，`x2` 递增，30s 封顶）；重连 URL 携带既有 `session` ID；close code 1006 时探测 `/api/auth/status`（该端点豁免守卫，未登录返回 200 `{"authenticated": false}`）判断是否会话过期，命中即跳转登录页，非 2xx 响应同样视为未登录。切换会话/关页产生的 `manualClose` 不触发重连。
