# 代码结构原则（高内聚 / 低耦合）

日期：2026-10-23
适用范围：`modules/`、`app.py`、`frontend/assets/js/`、`frontend/*.html`

本文件是 `docs/principles.md` 的代码层补充：那份管产品与 UX，这份管模块边界与依赖方向。
两者冲突时，先改原则再写代码；例外必须登记在文末。

## 一、分层与依赖方向

```
app.py / sessiond.py           入口与装配，只做注册与启动
        │
        ▼
modules/*_api.py               路由层：解析请求、鉴权、调用 service、返回 JSON
modules/*_runner.py            编排层：线程/进程/会话生命周期
modules/*_store.py             状态层：持久化、事件、文件系统
        │
        ▼
config.py  modules/state.py  modules/internal_api.py  modules/events.py
```

规则：

1. 依赖只能向下，不能同层横向 import，更不能反向。
2. 功能模块之间禁止互相 import；共享能力下沉到 `state` / `internal_api` / `config`。
   - 现有例外：`aihandoff → files`（缩略图），登记为待拆。
3. 路由层不写业务循环、不直接操作进程；编排层不解析 HTTP；状态层不 import Flask。
4. 前端同理：页面脚本 → `common.js` / 领域模块；领域模块之间通过显式函数接口，不用 `window.*`。

## 二、高内聚的判定

一个模块/文件只应该有**一个变化理由**。判定问题：

- 改存储格式时，是否要动 HTTP 路由？
- 改 ACP 事件映射时，是否要动任务列表接口？
- 改 SFTP 权限时，是否要动主机凭据存储？

任一为“是”，说明职责混在一起。

推荐按 **store / runner / api** 三段切一个功能：

| 层 | 只做 | 禁止 |
|---|---|---|
| store | 读写状态、事件、文件系统 | Flask、线程、子进程 |
| runner | 线程/进程/会话编排 | Flask request、JSON 解析 |
| api | 路由与参数校验 | 线程循环、直接改全局字典 |

## 三、文件预算

| 类型 | 预算 | 说明 |
|---|---:|---|
| Python 模块 | ≤ 400 行 | 含注释；超出先拆 |
| 前端 JS | ≤ 500 行 | 页面脚本；工具进 `common.js` |
| CSS | ≤ 500 行/页 | 设计令牌在 `tokens.css`；样式内聚，不按模块耦合计算 |
| HTML | 不写业务逻辑 | 只保留模板与绑定 |

达到预算 80% 时，下一次改动必须先拆再加功能。

### 存量例外（只减不增）

**例外表当前登记 14 个文件**（清单见第七节），上限 = 登记当天行数，此后只能变短。
未在表内的文件必须直接符合预算；**新文件不允许再登记例外**——唯一例外是「对外分发
的单文件产物」（`.agents/skills/deskport/deskport.py`、`agent_node/acp_session.py` 这类：拷一个文件就要能
跑，拆开等于废掉它），登记时必须在第七节写清单文件理由。

2026-11-05：`tools/check_structure.py` 的 `read_lines()` 先 `f.read()` 再迭代同一句柄，行数恒为 0，**预算检查自写成以来从未生效**。修复后一次性暴露 7 个超预算文件，已全部拆完（过程见 `docs/design-review.md`）。这一条值得记住：门禁本身失效时，它报的"干净"没有任何意义。

2026-11-12：R4/R5 两轮把 `tasks_*`、`agent_*` 等重新写回超预算，且没登记，门禁因此红了数日——
这段时间 `deploy.ps1` 的预检会 `exit 1`，实际发布改走人工/智能体侧的同步流程（发布戳带
`-rNN` 后缀，是那段时间手工分批上线的痕迹）。
决策是**先认清现状再逐步收敛**：把现有 13 个超预算文件按当前行数登记回 `LEGACY`，门禁恢复绿，
之后这些文件只允许变短。

已完成（后已回退）：`modules/tasks.py`（988）曾拆为 `tasks_store` 281 / `tasks_runner` 308 / `tasks_api` 328 / `tasks_legacy` 112；
`tasks_legacy` 在 2026-11-10 随「API 不做跨版本兼容」删除（02ad9dd），另外三个文件在 R4/R5 后长到
433 / 689 / 961，已在第七节重新登记。
已完成：前端 `common.js` 统一 `fmtSize` / `fmtTime` / `fmtAgo` / `escapeHtml`，页面不再各自实现。
已完成：`term-client.js`（682）拆为 term-client-env 28 / term-session-ui 106 / term-controls 85 / term-client 499。
已完成：`terminal.css`（577）拆为 terminal 213 / terminal-controls 232 / terminal-mobile 132。
已完成：TaskRepository（tasks_store）/ HostRepository + ConnectionRegistry（ssh_hosts）/ SessionRegistry（termcore）；StreamingZipJob（files_archive）；SSHHostStore Observer（前端）。
已完成：前端消息契约 `messages.js`（`WP_MSG`，壳/iframe 不再裸写字符串）。
已完成：任务状态机 `task_state`、执行点分派 `task_engines`（按 local/agent/federation）、缩略图 `media`、共享跳过目录 `fsutil`；`files_browse→pins`、`inbox_scan→files/decision` 横向依赖已移除。
已完成：`modules/control.py`（459）已拆为 `control_base` 25 / `control_input` 144 / `control` 309。
已完成：`frontend/assets/js/ssh-ui.js`（780）已拆为 `ssh-ui-ctx` 19 / `ssh-sftp-ui` 478 / `ssh-ui` 306。
已完成：`modules/aihandoff.py`（536）已拆为 `inbox_base` 8 / `inbox_scan` 298 / `aihandoff` 247。
已完成：`frontend/assets/js/files.js`（1459）已拆为 `files-env` 30 / `files-methods-core` 462 / `-ops` 201 / `-ui` 163 / `-ai` 368 / `files.js` 247。
已完成：`modules/files.py`（731）已拆为 `files_base` 12 / `files_browse` 252 / `files_transfer` 209 / `files_archive` 248 / `files_pins` 84 / `files` 7。
已完成：`modules/ssh.py`（773）已拆为 `ssh_base` 19 / `ssh_hosts` 283 / `ssh_terminal` 149 / `ssh_sftp` 336 / `ssh` 23。
已完成（2026-11-05）：`modules/termcore.py`（568，5 个变化理由）→ `termprobe` 72 / `wsproto` 231 / `termcore` 299。
已完成（2026-11-05）：`modules/ssh_sftp.py`（503，CRUD 与批量传输混在一起）→ `ssh_sftp_base` 85 / `_ops` 145 / `_transfer` 309 / `ssh_sftp` 21（装配）。
已完成（2026-11-05）：`modules/plugins.py`（439，4 个变化理由）→ `plugins_base` 71 / `plugins_discover` 92 / `plugins_load` 207 / `plugins_nav` 96 / `plugins` 25（装配）。
已完成（2026-11-05）：前端 `ssh-sftp-ui.js`（618）→ `state` 61 / `menu` 71 / `select` 124 / `transfer` 129 / `ui` 363。
已完成（2026-11-05）：前端 `files-methods-core.js`（523）→ `core` 364 / `select` 167（按对象混入拆块）。
已完成（2026-11-05）：前端 `term-features.js`（467，超存量上限）→ `features` 421 / `term-sidebar` 71。
已完成（2026-11-05）：前端 `term-client.js`（517）→ `client` 400 / `term-geometry` 166。
已完成（2026-11-05）：前端导航配置收敛到 `nav.js`（`window.WP_NAV`），`app-shell.js` 与 `shell.js` 只做派生——原先两份定义已漂移（新壳有 tasks 旧壳没有）。
已完成（2026-11-05）：`modules/inbox_scan.py` 的 5 个模块级裸缓存全局封装为 `_DigestCache` / `_FingerprintCache`（与 `tasks_store._LazyRepo` 同一做法）。
已完成（2026-11-05）：`common.js` 增 `WPCommon.confirm()`，替换 6 处原生 `confirm()`。

## 四、共享与去重

- 前端格式化与 DOM 工具只允许一份：`frontend/assets/js/common.js`（`fmtSize`、`fmtTime`、`escapeHtml`、`toast`、`fetchJson`）。
- 前端纯逻辑（构造请求体、判断分支）抽成可 import 的纯函数模块：挂 `window.X` 供页面调用，同时 `module.exports` 供 Node 引入；测试放 `tests/js/` 用 `node --test` 跑（`tools/check_frontend.py` 门禁含这一步，红即失败）。
- 后端状态读写只允许一份：`modules/state.py`（原子写）与 `modules/events.py`（审计）。
- 禁止复制动作表、格式化函数（`principles.md` 已否决）。
- 禁止新增 `window.*` 跨页契约；已有契约登记在 `frontend/assets/js/common.js` 顶部注释，新增需评审。

## 五、改动纪律

1. 重构提交必须**行为不变**，单独提交，不与新功能混在一起。
2. 拆分顺序：先补测试/冒烟 → 移动代码 → 改 import → 跑 `py_compile` / `node --check` / 冒烟 → 提交。
3. 每次拆分后运行 `tools/check_structure.py`；存量文件只能变短。
4. 新功能准入：先回答“放在哪个视图/哪个模块”，再写代码；禁止在路由函数里直接堆业务。

## 六、门禁

`tools/check_structure.py`（后端）：

- 检查 Python / JS 行数预算；
- 检查存量例外是否变长；
- 扫描重复的 `fmtSize` / `fmtTime` / `escapeHtml`；
- 扫描新增 `window.*` 赋值；
- 扫描功能模块之间的横向 import（未登记 `ALLOWED_FEATURE_IMPORTS` 的方向即报错，每条登记项附方向理由）；
- 扫描跨模块 import 的单下划线私有名（`scan_private_seams`：引私有名、公有名起私有别名都拦；存量同包内部接缝登记在 `PRIVATE_SEAM_ALLOWED`，每条附理由、只减不增）；
- pyflakes 覆盖 `app.py` / `auth.py` / `config.py` / `sessiond.py` / `watchdog.py`
  与 `modules/`、`tools/`、`tests/`：未定义名是错误，未使用 import 是 warning
  （有意为之的副作用 import 用 `# noqa: F401` 标注）。

`tools/check_frontend.py`（前端，静态检查抓不到的前后端漂移）：

- `node --check` 全量 JS、CSS 花括号配平；
- HTML 引用的 `/assets/...` 是否真实存在；
- JS 里 `getElementById` / `querySelector('#x')` 的 id 是否在 HTML 或 JS 里定义；
- JS 调用的函数名是否有声明（共享顶层作用域，含 HTML 内联脚本）；
- JS 请求的 `/api/...` 是否在后端登记了路由；
- `tests/js/` 的前端纯逻辑单测（`node --test`，找不到 node 时打印跳过、不算失败）。

`deploy.ps1` 的预检会调用结构门禁；本地提交前两个都应运行。

## 七、例外登记

| 日期 | 文件（行数上限） | 违反哪条 | 原因 | 复核 |
|---|---|---|---|---|
| 2026-10-23 | 存量 god files | 文件预算 | v2 快速迭代遗留 | 已按上表逐个拆完并移出本表 |
| 2026-11-12 | `modules/tasks_api.py`（961） | 文件预算 | R4/R5 连续追加：阻塞 wait、SSE、审批、轮中插话、工作线路由 | 按 wait/events · messages · nodes · 审计 四块拆 |
| 2026-11-12 | `modules/tasks_runner.py`（689） | 文件预算 | 会话生命周期、引擎工厂、接力棒写回混在一处 | 拆出生命周期与 workline 写回 |
| 2026-11-12 | `modules/tasks_store.py`（433） | 文件预算 | 仓储 + 排队消息 + 事件缓冲 | 拆出排队消息 |
| 2026-11-12 | `modules/agent_nodes.py`（580） | 文件预算 | HTTP 客户端 + 引擎注册 + 节点 API + 联邦转发 | 拆出引擎注册 |
| 2026-11-12 | `modules/agent_local.py`（491） | 文件预算 | 本机执行点适配（host/exec/sftp 别名） | 与 `agent_sftp` 对齐后拆 |
| 2026-11-12 | `modules/agent_sessions.py`（443） | 文件预算 | 持久 shell 的哨兵/超时/回收协议与端点同处 | 协议部分独立 |
| 2026-11-12 | `config.py`（516） | 文件预算 | 配置 getter 平铺，每字段一个函数 | 按域拆分 |
| 2026-11-12 | `agent_node/acp_session.py`（454） | 文件预算 | 远端 node「拷目录即跑」的零依赖单文件约束 | 与部署形态绑定，暂留 |
| 2026-11-12 | `.agents/skills/deskport/deskport.py`（605） | 文件预算 | 技能包里的客户端：单文件、零依赖、可直接当 CLI 跑，拆开就没法「拷一个文件就能用」 | 与分发形态绑定，暂留 |
| 2026-11-12 | `agent_node/node_api.py`（417） | 文件预算 | node HTTP 层（路由 + 会话存档 + 探测） | 同上 |
| 2026-11-12 | `frontend/assets/css/files.css`（522） | CSS 预算 | 文件页样式内聚，按页计 | 拆出移动端部分 |
| 2026-11-12 | `tests/test_tasks_api.py`（1539）、`tests/test_agent_api.py`（611）、`tests/test_r47_remote_adopt.py`（481） | 文件预算 | 测试按端点聚合，尚未按域拆分 | 随对应模块拆分时同步拆 |

（2026-11-05 因 `read_lines` 空转而暴露的 7 个超预算文件，已全部拆完并移出本表。
2026-11-12：R4/R5 期间 `tasks_*`、`agent_*` 重新变长且未登记，门禁红了数日、发布因此绕过了
`deploy.ps1`。按「先认清现状再收敛」重新登记上面 12 行（14 个文件），上限 = 登记当天行数，
此后只能变短。其中 `.agents/skills/deskport/deskport.py` 是新增的对外分发单文件，理由见上一行与第六节。）

新增例外必须写进本表，否则视为违规。新文件不得登记例外。