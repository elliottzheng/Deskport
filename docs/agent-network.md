# 智能体网络运作方式（架构总览）

> 状态：现行架构。本文描述 Deskport 智能体网络的节点角色、通信通道、
> 任务生命周期、信任模型与扩展方式。配套文档：`agent-api.md`（接口参考）、
> `audit-agent-network.md`（质量审计）、`agent_node/README.md`（node 部署）。

一句话概括：**一个中枢（Deskport）、两种执行点（本机/远端 node）、N 个持
钥匙的指挥者（人和外部智能体），一切交互收敛到任务卡这一张监督面。**

## 一、角色与拓扑（星型）

```
                人（浏览器，cookie）
                      │ 任务卡：派活/看事件/批权限/插话/接手
                      ▼
   外部智能体 ──token──▶ Deskport 中枢（6789）──┬──▶ 本机 node（8622）──▶ 本机 Kimi
  （ai-xmyz 的 kimi 等）                        └──SSH隧道──▶ ai-xmyz node（8622）──▶ 远端 Kimi
```

- **中枢（Deskport web）**：唯一编排者。持有任务状态、审计流、权限审批、
  令牌簿（agentTokens）、节点簿（agentNodes + localAgentNodePort）。
- **agent node（每台执行机一个）**：纯粹的「会话托管者」——只认识
  `kimi acp` 协议和 HTTP token，不知道任务卡的存在。托管本机或远端的
  Kimi 会话（spawn、事件环形缓冲、权限挂起、空闲回收、resume 存档）。
- **外部智能体**：任何持有 `X-Agent-Token` 的 AI 程序。是「带钥匙的
  遥控器」，**一切指挥都经过中枢**——这是有意选择：监督面单一，
  审计与审批不旁路。（节点间理论可点对点直连，见「六、取舍」。）
- **人**：cookie 登录，拥有与智能体完全相同的能力面（派活/观察/审批/
  续聊/接手），且任务卡上所有动作可视化。

## 二、通信通道

| 链路 | 协议 | 传输 |
|---|---|---|
| 人 ↔ 中枢 | cookie + JSON / SSE | 浏览器任务卡 |
| 外部智能体 ↔ 中枢 | `X-Agent-Token` + JSON / SSE | 同一套任务 API |
| 中枢 ↔ node | `X-Agent-Token` + JSON / SSE | 直连或 SSH direct-tcpip 隧道 |
| node ↔ kimi | ACP JSON-RPC over stdio | 进程内管道 |

原则：**外部世界只看到 REST + SSE；ACP 协议只存在于 node 与 kimi 之间**。
防火墙不可达时，SSH 只承载字节流（隧道），语义仍是 HTTP + token，
零防火墙改动。

## 三、任务生命周期

1. **派活** `POST /api/tasks` → `clientFactoryFor` 选执行点
   （engine=acp → 本机 node；engine=remote → 指定节点 + 远端 cwd；
   node 未启用 → 回退 web 内 stdio 托管）→ `_sessionThread` 建常驻会话。
2. **执行** node 侧 spawn `kimi acp`（或 `session/load` 接续存档会话）；
   中枢与 node 之间一条 **SSE 长连接**——update 到达即回调进任务卡。
   回答的 chunk 只累积缓冲（log 逐 chunk 流水可追溯），回合结束以
   **单条完整 assistant 事件**落卡——按 seq 去重的订阅者拿到的永远是终版。
3. **监督** `mode=default` 时每个工具调用停下发 `permission_request`；
   人点任务卡、或外部智能体 POST permission，决策经原路送回。
   决策提交失败会留痕（stderr + warning），node 端 900s 兜底超时。
   `mode=auto/plan/yolo` 为不同放行策略。
4. **续聊/接手** `status=waiting` 表示本轮完成；任何一方 `message` 续聊
   自动接续原上下文。多端可同时挂在同一会话上（node 按 kimiSessionId
   复用存活进程）。人可随时插话、抢权限。
5. **终局** `close` 后事件流发 `end` 关闭；任务记录持久化
   （tasks.json + log jsonl 全量流水）。

### 3.1 工作线：会话之外的那根接力棒（R5-1a）

一条工作线（如"第四轮开发"）会换很多个会话：上下文涨满、锁死、重启。
所以工作线的状态不放会话里，放三处：接力棒正文（中枢的
`worklines/<id>.md` + 任务工作区的 `WORKLINE.md` 副本）、git、规格文件。

- `POST /api/worklines` 建线；`POST /api/worklines/<id>/tasks` 派活时
  服务端把接力棒注入**首条消息**（超 4000 字则降级为摘要 + "先读
  WORKLINE.md"），并自动带上"不要使用后台任务"与 policy 摘要。
- 执行方每轮按约定把更新后的接力棒整篇写回 `WORKLINE.md`；轮末中枢比对
  hash 后落库（主控轮中改过则跳过本轮写回，单写者不覆盖）。
- 上下文超过阈值（默认 75%）落一条 status 事件，`GET /api/worklines/<id>`
  的 `nextAction=rotate` 提示；`POST .../rotate` 换新会话接棒、旧任务 close。
  默认不自动轮换（轮中切会丢半截工作）。
- 远端写回（engine=remote / 联邦）留到 R5-1b；联邦执行点暂不接受工作线。

## 四、韧性模型（会话为什么是「永生」的）

关键决策：**会话托管与编排分离**——web 进程不持有任何智能体会话。

| 故障 | 后果 | 恢复 |
|---|---|---|
| web 崩溃/重启 | 无感（任务活在 node 里） | watchdog 自动拉起 web |
| node 进程崩溃 | 会话中断 | kimi 自身会话存档 + `session/load` 接续；sessions.json 存档 |
| SSH/NAT 断连 | 事件流断 | 隧道自动重建 + SSE `since` 补差，零丢失 |
| 空闲 45 分钟 | node 回收 kimi 进程（省内存） | kimiSessionId 已存档，resume 接续上下文 |
| 整机重启 | 各服务由 watchdog/vbs/cron 自愈 | 本次重启已实战验证 |

## 五、信任与经济模型

- **单一信任根**：token = 全部执行权（快命令、SFTP、任务派活），无细分
  权限档位。审计是唯一制衡：`agent.exec` / `agent.sftp.*` /
  `agent.task.create` 每次调用留痕，任务卡 log 逐 chunk 可追溯。
- **权限门**：`mode=default` 让人（或指挥智能体）逐步审批；危险操作
  永远停在审批点上，直到决策或 900s 超时拒绝。
- **token 成本随成员线性增长**（每加一个指挥者/执行点分发一次）。
  这是单用户系统的有意简化：用「全程可见 + 可审计」代替「权限细分」。
  未来若出现低信任巡检类智能体需求，再拆权限档位。

## 六、取舍记录（为什么不做成别的样子）

- **星型单中枢 = 单点**。中枢挂了执行点各自存活（会话不丢），但互指
  中断；恢复后任务卡追平，无数据损失。换取：监督面永远唯一。
  对单用户这是正确取舍。
- **协议实现有意双份**（node 的 AcpSession 零依赖单文件 vs Deskport 内
  KimiAcp 回退客户端）：部署形态（拷目录即跑）与回退能力决定。
  `AcpClient` Protocol + `tests/test_acp_parity.py` 对拍测试把「双份」
  从负债变成有护栏的资产。
- **中枢内 SSE 对内 0.6s 轮询 task dict**：store 层无事件通知机制下的
  务实权衡（改造成本 > 收益）。对外/对 node 均已是推送。
- **不做多级权限 / 不做多用户**：单用户系统，负资产。

## 七、扩展方式（加成员的固定动作）

- **加一个执行点（机器）**：
  1. 目标机安装并登录 kimi CLI；
  2. 拷贝 `agent_node/` 目录；
  3. `python3 agent_node.py --port 8622 --token-file ~/.deskport-agent-node/token`
     （文件不存在自动生成）；自启：Linux 用 crontab `@reboot` + setsid，
     Windows 用 `start-agentnode.vbs` + watchdog；
  4. 中枢 `config.json` 的 `agentNodes[]` 登记一条（防火墙过滤时填
     `via` 走 SSH 隧道）。
- **加一个指挥者（智能体）**：分发 token，技能文档由中枢 URL 自举
  （`GET /api/agent/skills/deskport` → 写入智能体技能目录）。它立刻获得
  三级能力：快命令（exec）、持久终端（bash 会话）、任务派活（指挥其他
  智能体）；Deskport 升级后重拉即同步最新版。

## 八、协议实现位置索引

| 组件 | 位置 |
|---|---|
| node 协议托管（ACP 会话核心） | `agent_node/acp_session.py` |
| node HTTP 层 / 会话存档 | `agent_node/node_api.py` |
| node 入口（参数/自愈回收/信号） | `agent_node/agent_node.py` |
| SSH 隧道传输层 | `modules/agent_tunnel.py` |
| HTTP 客户端 + 引擎注册 + 节点 API | `modules/agent_nodes.py` |
| 任务编排（生命周期骨架 + factory hook） | `modules/tasks_runner.py` |
| 任务路由（含任务 SSE） | `modules/tasks_api.py` |
| 回退客户端（web 内 stdio） | `modules/kimi_acp.py` |
| 客户端契约 / 事件契约 | `modules/contracts.py`（AcpClient / TaskEvent） |
| 对拍护栏 | `tests/test_acp_parity.py` |
