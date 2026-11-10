# 智能体网络代码审计 — 优雅性 / 内聚耦合 / 设计模式

审计范围：node 化 + SSE + 任务 API 这几轮的新增与改动代码。
基线 commit：`6471f44`（任务 SSE）+ 文档 `f96296e`。
审计日期：2026-11-10。结论分级：P0（正确性缺陷）/ P1（建议尽快）/ P2（观察项）。

---

## 一、实测发现

### P0-1 assistant 流式合并 vs 增量订阅的截断风险

**机制**（`tasks_runner.py:24-48 _appendAssistantText`）：Kimi 的 message chunk
不逐条成事件，而是合并进同一条 assistant 事件（seq 不变、text 原地增长）。
增量订阅者（轮询 `/events?after=` 与新增 SSE `/events/stream`）按 seq 去重——
**已推送 seq 的后续合并对订阅者不可见**。

**实测**：
- 300 字散文任务（131 个 chunk 约 2 秒流完）：SSE 订阅者拿到完整 288 字 ✅
  —— chunk 流完成时间 < 0.6s 扫描间隔时观察不到；
- 推理：慢速长输出（Kimi 逐句流式几十秒）时，订阅者拿到的文本停在
  「最后一次扫描时刻」的中间态，必然截断；
- 前端任务卡未报告过此问题，疑似有 `lastText` 渲染兜底，但 **API 订阅者
  （外部智能体读 `assistant` 事件）是真实受影响的**——这正是智能体指挥
  智能体的核心数据通路。

**修复建议**：`_promptOnce` 返回后（回合结束、turn_end 事件之前），若本轮
发生过合并，以新 seq 追加一条完整 assistant 终版事件（前端按 seq 渲染自然
覆盖中间态；订阅者拿到确定终态）。约 15 行。

### P1-1 权限决策回写失败被静默吞掉

`modules/agent_nodes.py:205-211`：`on_permission` 拿到 optionId 后 POST 给
node，失败时 `except Exception: pass`。后果：决策丢失，node 端权限请求挂满
900 秒超时，任务卡显示「已允许」但远端实际取消了该操作——两端状态分叉且
无任何痕迹。

**修复建议**：失败时写入 `self.stderr_lines`（会进任务卡 error 呈现），
并重试一次（复用 nodeRequest 已有的隧道重建逻辑即可，只是要把结果检查
从 `status==200` 变为失败也报）。

### P1-2 tasks_api.py 逼近行数预算

本轮 +47 行（SSE 端点）后为 379/400。下次任何功能 additions 都会触门禁。
建议预防性拆分：`taskEventStream`（SSE 生成器 ~45 行）移入
`modules/tasks_stream.py`，与未来可能的节点事件流共用。

### P1-3 事件合并语义应有契约说明

P0-1 的机制本身是**有意设计**（避免每 token 一条事件，见
`tasks_runner.py:25` 注释），但「seq 不变、内容可变」这一语义没有写进
`contracts.py` 的 TaskEvent，也没有任何测试锁定。任何新订阅者（含第三方
集成者）都会踩同一个坑。修复 P0-1 后，在 contracts 补注释 + 对拍测试加
「长文本回合」场景。

---

## 二、内聚耦合分析

### 依赖图（静态，无环）

```
config ←────────────────────────────┐
  ↑                                 │
ssh_hosts ← agent_tunnel ← agent_nodes（客户端/引擎/API）
task_engines ←┘            │
kimi_acp（回退客户端）       │
tasks_store ← tasks_runner ← tasks_api
agent_node: agent_node.py → node_api.py → acp_session.py（单向）
```

- 运行期环 `agent_nodes ↔ tasks_runner` 以函数内 lazy import 解开
  （`agent_nodes.py:317/327`、`tasks_runner.py:270`）——已知妥协，边界清晰，
  两处均有注释。**可接受**；彻底解法（注册回调注入 runner）收益不抵改动。
- `agent_node` 三模块单向依赖，acp_session 零内部依赖可独立测试 ✅。
- config 是纯被依赖的叶子 ✅。

### 高内聚评价

| 模块 | 职责 | 评价 |
|---|---|---|
| acp_session.Ring/AcpSession | 一个 ACP 会话的传输+状态机 | 内聚良好；AcpSession 身兼 6 职（进程/传输/ring/权限挂起/turn 队列/meta），目前 250 行尚可控，若再长可拆 AcpConnection（传输）+ AcpSession（状态机） |
| node_api | 存档 + HTTP 路由 | 内聚；模块级单例（_MANAGER/_TOKEN + init()）是标准库 http.server 的惯用妥协，可用 partial 注入 Handler 但收益低（P2） |
| agent_tunnel | 纯隧道传输 | 高内聚零业务依赖 ✅ |
| agent_nodes | 客户端 + 引擎注册 + 节点 API | 三职责一文件（330 行），若再长可拆客户端与 API；`_streamLoop` 单方法含连接/解析/分发/退避四件事，建议拆 `_parseSseLine`（P2） |
| tasks_api.taskEventStream | web 进程内 SSE | **对外 SSE、对内 0.6s 轮询 task dict**——store 层无事件通知机制下的务实权衡；改造成本（store 加 Condition）> 收益，保持现状，报告存档 |

### 低耦合评价

- `clientFactoryFor`（`agent_nodes.py:291`）是引擎与客户端实现的唯一交汇点，
  新增传输方式只改这一个函数 ✅。
- `findNode` 并入 local 节点后，`agentNodes[]` 与 `localAgentNodePort` 两种
  配置形态共用一条寻址路径 ✅。
- tasks SSE 与 node SSE、RemoteAcpClient 与 node 端解析——SSE 编解码
  `data: {json}\n\n` 散落 3 处（各 ~2 行）。抽象成公共 helper 收益低
  （标准库无 sse 模块，三处上下文不同），**不建议**抽象，保持就地。

---

## 三、设计模式分析（GoF 视角）

| 模式 | 落点 | 评价 |
|---|---|---|
| **Strategy** | `clientFactoryFor`（stdio/本机 node/远端 node 三态选择）、`task_engines` 注册表 | 本轮的核心结构。策略切换对任务层零暴露 ✅ |
| **Adapter** | `RemoteAcpClient`（HTTP→KimiAcp 接口）、`ThreadEngine`（线程体→引擎接口） | 本轮最关键的 Adapter，接口对齐由 `AcpClient` Protocol + 对拍测试双保险 ✅ |
| **Template Method** | `_sessionThread` 任务生命周期骨架（start→prompt→循环），`clientFactory` 为 hook | 教科书式用法：骨架不动，传输替换 ✅ |
| **Facade** | agent node 的 REST API（把 ACP JSON-RPC 会话语义暴露为 sessions/prompt/events） | ✅ |
| **Observer** | Ring+Condition（node 端）、两处 SSE（node→Deskport、Deskport→外部） | 推送模型三段贯通 ✅ |
| **Proxy** | agent_tunnel（本地 listener→SSH direct-tcpip） | 与 federation/external_proxy 同族，职责清晰 ✅ |
| **Singleton** | `node_api._MANAGER`、`agent_tunnel._pool` | 模块级单例，进程内唯一，可接受 |
| **缺失/机会** | 权限挂起等待（`tasks_runner._handleAcpPermission` 与 node 端 `_handlePermission`）各自手写了「decision dict + Event」的 Promise 式结构 | 两份同构实现；node 零依赖约束下不能共享代码，但可共享设计——抽 `PendingDecision` 概念注释成对（P2） |

**总评**：模式使用克制且服务意图，没有为模式而模式的痕迹。
本轮最值得肯定的两个决定：① `_sessionThread` 的 `clientFactory` hook 让
node 化以 17 行侵入完成；② 对拍测试把「双实现」从负债变成有护栏的资产。

---

## 四、正确性细节（逐条核对）

| # | 位置 | 发现 | 级别 |
|---|---|---|---|
| 1 | `agent_nodes.py:210` | 权限 POST 失败静默（见 P1-1） | P1 |
| 2 | `agent_node/acp_session.py:101` | `_request` 方法内 `import concurrent.futures`，移顶层 | P2 |
| 3 | `agent_nodes.py:171-174` | 404 分支 continue 正确跳过隧道重建 ✅ | — |
| 4 | `agent_nodes.py:180` | 重连时 `self._node()` 若配置已删会再抛，被外层 except 吞 → 无限退避重试直至 close | P2（配置删除是管理动作，任务卡可见，可接受） |
| 5 | `tasks_api.py` taskEventStream | `nonlocal since` 已修；events 合并语义见 P0-1 | — |
| 6 | 心跳/超时参数 | node 心跳 25s、tasks 心跳 18s、客户端读超时 45s——数值均安全但三处分散，建议集中常量 | P2 |
| 7 | `AcpSession._loading` | `__init__` 已初始化（拆分时修正），resume 重放标记正确 ✅ | — |
| 8 | `Ring.waitSince` | Condition wait_for 谓词正确，无丢通知窗口 ✅ | — |
| 9 | `submitDecision` 后 `event.set()` | 与 `_handlePermission` 的等待对象统一（早期 bug 已修）✅ | — |

## 五、测试覆盖评估

- **对拍测试**（`test_acp_parity.py`）：update 路由、权限回写已覆盖；
  **缺**：resume 重放标记、set_mode、cancel、长文本合并场景（P0-1 修复后补）。
- **SSE 端点**：node 与 tasks 两处均只有手动冒烟，无自动化。建议各补一个
  test_client 流读取测试（web 侧易；node 侧需起线程服务，中等）。
- 已知环境坑（供后任）：pytest 必须用 `.venv/Scripts/python.exe`；
  ai-xmyz 远程操作 kill/pgrep 必须 `[a]gent_node` 防自匹配，启动必须 setsid。

## 六、结论

架构方向（会话托管服务化、传输统一 node、双实现对拍护栏）经审计成立。
**需要行动的只有 P0-1 和 P1-1**，其余为观察项与预防性拆分建议。
