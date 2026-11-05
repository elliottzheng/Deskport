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

本轮存量例外已清零；新增文件必须直接符合预算。

已完成：`modules/tasks.py`（988）已拆为 `tasks_store` 281 / `tasks_runner` 308 / `tasks_api` 328 / `tasks_legacy` 112，从例外表移除。
已完成：前端 `common.js` 统一 `fmtSize` / `fmtTime` / `fmtAgo` / `escapeHtml`，页面不再各自实现。
已完成：`term-client.js`（682）拆为 term-client-env 28 / term-session-ui 106 / term-controls 85 / term-client 499。
已完成：`terminal.css`（577）拆为 terminal 213 / terminal-controls 232 / terminal-mobile 132。
已完成：TaskRepository（tasks_store）/ HostRepository + ConnectionRegistry（ssh_hosts）/ SessionRegistry（termcore）；StreamingZipJob（files_archive）；SSHHostStore Observer（前端）。
已完成：前端消息契约 `messages.js`（`WP_MSG`，壳/iframe 不再裸写字符串）。
已完成：任务状态机 `task_state`、引擎策略 `task_engines`、缩略图 `media`、共享跳过目录 `fsutil`；`files_browse→pins`、`inbox_scan→files/decision` 横向依赖已移除。
已完成：`modules/control.py`（459）已拆为 `control_base` 25 / `control_input` 144 / `control` 309。
已完成：`frontend/assets/js/ssh-ui.js`（780）已拆为 `ssh-ui-ctx` 19 / `ssh-sftp-ui` 478 / `ssh-ui` 306。
已完成：`modules/aihandoff.py`（536）已拆为 `inbox_base` 8 / `inbox_scan` 298 / `aihandoff` 247。
已完成：`frontend/assets/js/files.js`（1459）已拆为 `files-env` 30 / `files-methods-core` 462 / `-ops` 201 / `-ui` 163 / `-ai` 368 / `files.js` 247。
已完成：`modules/files.py`（731）已拆为 `files_base` 12 / `files_browse` 252 / `files_transfer` 209 / `files_archive` 248 / `files_pins` 84 / `files` 7。
已完成：`modules/ssh.py`（773）已拆为 `ssh_base` 19 / `ssh_hosts` 283 / `ssh_terminal` 149 / `ssh_sftp` 336 / `ssh` 23。

## 四、共享与去重

- 前端格式化与 DOM 工具只允许一份：`frontend/assets/js/common.js`（`fmtSize`、`fmtTime`、`escapeHtml`、`toast`、`fetchJson`）。
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
- 扫描功能模块之间的横向 import；
- pyflakes 覆盖 `app.py` / `auth.py` / `config.py` / `sessiond.py` / `watchdog.py`
  与 `modules/`、`tools/`、`tests/`：未定义名是错误，未使用 import 是 warning
  （有意为之的副作用 import 用 `# noqa: F401` 标注）。

`tools/check_frontend.py`（前端，静态检查抓不到的前后端漂移）：

- `node --check` 全量 JS、CSS 花括号配平；
- HTML 引用的 `/assets/...` 是否真实存在；
- JS 里 `getElementById` / `querySelector('#x')` 的 id 是否在 HTML 或 JS 里定义；
- JS 调用的函数名是否有声明（共享顶层作用域，含 HTML 内联脚本）；
- JS 请求的 `/api/...` 是否在后端登记了路由。

`deploy.ps1` 的预检会调用结构门禁；本地提交前两个都应运行。

## 七、例外登记

| 日期 | 文件 | 违反哪条 | 原因 | 复核 |
|---|---|---|---|---|
| 2026-10-23 | 存量 god files | 文件预算 | v2 快速迭代遗留 | 按上表逐个拆 |
| 2026-11-05 | `modules/termcore.py` 568 | 文件预算 / 高内聚 | 混了 backend、注册表、生命周期、Win32 探测、WS 帧协议 5 个变化理由；`read_lines` 空转导致从未受检 | 拆为 wsproto / termprobe / termcore，见 `docs/design-review.md` P1-1 |
| 2026-11-05 | `modules/ssh_sftp.py` 503 | 文件预算 / 高内聚 | CRUD 与批量传输打包两个变化理由未按三段制拆 | 拆为 ssh_sftp_base / _ops / _transfer，见 P1-2 |
| 2026-11-05 | `modules/plugins.py` 439 | 文件预算 / 高内聚 | 发现、动态导入、导航推导、登记查询 4 个变化理由 | 拆为 plugins_base / _discover / _load / _nav，见 P1-3 |
| 2026-11-05 | `frontend/assets/js/ssh-sftp-ui.js` 618 | 文件预算 | 选中/框选、快速访问、导航、传输混在一起 | 见 P1-2 同批 |
| 2026-11-05 | `frontend/assets/js/files-methods-core.js` 523 | 文件预算 | 文件方法核心，浏览/选择/动作待再分 | 随 files 系列一起复核 |
| 2026-11-05 | `frontend/assets/js/term-features.js` 467 | 存量只减不增 | 原上限 464，实际已 467（门禁空转未被发现） | 随 termcore 拆分一起收 |

新增例外必须写进本表，否则视为违规。