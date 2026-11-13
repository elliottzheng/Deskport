# 系统架构

本文覆盖：Flask 入口与模块注册约定、页面路由表、认证守卫、静态资源服务、MJPEG 单捕获广播架构、终端模块线程模型、前端组织方式。
本文不覆盖：各 API 端点的请求/响应细节（见 [api.md](api.md)）、终端 WS 帧协议（见 [terminal-protocol.md](terminal-protocol.md)）、设计令牌与组件（见 [design-system.md](design-system.md)）、部署步骤（见 [deployment.md](deployment.md)）。

## 总览

```
浏览器 (零构建: Vue 全局构建 + 页面 JS + CSS 令牌)
   │  HTTP 页面/JSON API                    MJPEG 长响应            WebSocket
   ▼                                          ▼                      ▼
Flask app (app.py, 0.0.0.0:6789, threaded=True)
   ├─ auth.py              登录守卫 / 改密 (before_request, 全路由含 WS)
   ├─ modules/system.py    GET /api/system
   ├─ modules/command.py   POST /api/command
   ├─ modules/screen.py    MJPEG 流 + 键鼠控制
   ├─ modules/files.py     /api/files*（browse/transfer/archive/pins）+ files_federation
   ├─ modules/control.py   控制台（进程/窗口/电源/审计）+ control_input
   ├─ modules/terminal.py  本机终端 WS（或经 sessionproxy 转发到 locald）
   ├─ modules/ssh.py       SSH/SFTP（或经 sessionproxy 转发到 sshd）
   ├─ modules/tasks.py     任务/AI 交接 + aihandoff
   ├─ modules/settings.py  密码/收件箱/品牌/登录设备/上传目标
   ├─ modules/feedback.py  用户反馈（提交/附件/检索/状态）+ feedback_store
   ├─ modules/plugins.py   插件发现与加载（原生 + legacy）+ 导航
   └─ 智能体面: agent_api(+sessions/sftp/upload/device_auth) / agent_nodes /
                tasks_api(+runner/store/policy) / worklines / federation
```

智能体面自成一个子系统：中枢（本进程）通过 `agent_tunnel` + `agent_nodes`
把任务派给本机或远端 `agent_node/`（独立进程，托管 `kimi acp` 会话），
外部智能体用 `X-Agent-Token` 走同一套任务 API。角色与协议见
[agent-network.md](agent-network.md)，接口契约见 [agent-api.md](agent-api.md)，
本文只描述它在 Flask 里的装配位置。

服务运行方式：`app.run(host='0.0.0.0', port=<config>, debug=False, threaded=True)`。即 Flask 内置 werkzeug 服务器，每请求一线程；无 debug、无 reloader。

## 模块注册约定

入口 `app.py` 对业务模块不做逐个 import 配线，而是按约定遍历注册（`app.py:209-247`）：

```python
# app.py: 显式注册表（不再用 hasattr 猜）
BLUEPRINT_MODULES = (filesModule, commandModule, screenModule, aiModule,
                     controlModule, tasksModule, settingsModule,
                     pluginsModule, agentApiModule, agentNodesModule,
                     tasksFederationModule, worklinesModule, feedbackModule)
REGISTER_MODULES = (systemModule, metaModule, tasksModule,
                    worklinesModule, filesFederationModule)

for module in REGISTER_MODULES:
    module.register(app)
for module in BLUEPRINT_MODULES:
    app.register_blueprint(module.blueprint)
```

`create_app()` 负责装配并返回 `(app, sock)`；模块级 `app, sock = create_app()` 只为兼容既有入口。新增业务模块时，在 `app.py` 的两个元组之一登记，并按约定暴露 `blueprint` 或 `register(app)`。少数模块不在元组里、由 `_registerModules()` 手工调用（`ssh` / `terminal` / `sessionproxy` / `plugins.load` / `external_proxy` / `federation.bindApp`）。

当前各模块的实际注册方式：

| 模块 | 注册方式 | 说明 |
|---|---|---|
| `auth.py` | `register(app)` | `/login`、`/logout`、`/api/auth/*` 与 `before_request` 守卫 |
| `modules/system.py` | `register(app)` | 单端点模块 |
| `modules/meta.py` | `register(app)` | `/api/version`、`/api/diagnostics`、`/api/backup` |
| `modules/tasks.py` | `blueprint` + `register(app)` | 兼容壳，转发 `tasks_api` 的 `blueprint`/`register`；`register` 建任务根目录并加载仓储 |
| `modules/worklines_api.py` | `blueprint` + `register(app)` | 路由在蓝图；`register` 只加载工作线仓储 |
| `modules/files.py` | `blueprint`（`files_base`） | 门面；`files_browse`/`_transfer`/`_archive`/`_pins` 挂同一蓝图 |
| `modules/files_federation.py` | `register(app)` | 装一个 `before_request`：`/api/files*` 未命中本地路由时转发给联邦成员 |
| `modules/command.py` | `blueprint` | — |
| `modules/screen.py` | `blueprint` | MJPEG 流与键鼠控制 |
| `modules/aihandoff.py` | `blueprint`（`inbox_base`） | 与 `inbox_scan` 共用蓝图 |
| `modules/control.py` | `blueprint`（`control_base`） | 控制台；`control_input` 导入时挂子路由 |
| `modules/settings.py` | `blueprint` | 密码、收件箱、品牌、登录设备、上传目标 |
| `modules/feedback.py` | `blueprint` | 用户反馈七个端点；存储/幂等/频控/状态机在 `feedback_store`（`<stateDir>/feedback.json` + `feedback/attachments/`） |
| `modules/plugins.py` | `blueprint`（`plugins_base`）+ `load(app)` | 蓝图给 `/api/plugins`；`load()` 由 `_registerModules` 末尾调用，扫插件目录 |
| `modules/agent_api.py` | `blueprint` | 智能体 API 主体；`agent_sessions` / `agent_sftp` / `agent_upload` / `device_auth` / `agent_local` 把路由挂到同一蓝图，随它一次注册 |
| `modules/share_web.py` | `blueprint`（同上，import 即注册） | 网页分享表单：`/api/agent/share/stage`（设备令牌）与 `/api/share/*`（Cookie）；票据、页面与验收见 [m-share.md](m-share.md) |
| `modules/share_core.py` | 无路由 | 分享交付内核：字段规则、三个去处、执行点/任务列表；`agent_share` 与 `share_web` 共用 |
| `modules/agent_nodes.py` | `blueprint`（当前无路由） | 远端节点 HTTP 客户端、执行点分派注册（local/agent/federation）、节点模型/健康探测；端点本身挂在 `agent_api` 与 `tasks_federation` |
| `modules/tasks_federation.py` | `blueprint` | `/api/tasks/nodes*`（执行点呈现、探测、健康） |
| `modules/federation.py` | 无路由 | `bindApp(app)` 交给刷新线程做成员广播与导航注入 |
| `modules/external_proxy.py` | `register(app, apps)` | 手工调用：挂反代前缀，`prefix` 与已有路由重合直接抛错拒绝装配 |
| `modules/ssh.py` | `register(app, sock, sessionRoutes)` | 注册 `Sock` 的 WS；按配置决定是否代理到 sshd |
| `modules/terminal.py` | `register(app, sock, sessionRoutes)` | 同上，本机终端 |
| `modules/sessionproxy.py` | `register(app, sock)` | 配了 `sessiondUrl` 时挂代理路由（见 [sessiond.md](sessiond.md)） |

### 跨特性共享内核（2026-11-05 明确，2026-11-12 同步门禁）

`termcore` 不属于 terminal，而是 local/ssh 共用的内核；`wsproto` 是它的协议层，`termprobe` 是纯查询探针。三者归「基础设施层」：可被任何特性依赖，但自己不得 import 特性层（`tools/check_structure.py` 的 `scan_infra_dependencies` 已把这条编码进门禁）。

```
基础设施层   config / state / internal_api / events / fsutil / textio / media /
             contracts / decision / termcore / termprobe / wsproto / winenv
特性层       terminal / ssh / files / tasks / control / screen / command / aihandoff / plugins
```

依赖方向：特性 → 基础设施；基础设施 → 基础设施。反向或横向即在门禁报 warning。

两份分类集合在 `tools/check_structure.py` 的 `INFRA` 与 `FEATURES`，上表以它们为准，新增共享内核要两边同步。两点现状：

- `sensitive_values` / `task_state` / `model_availability` 实际只依赖 `config` 和标准库，属事实上的基础设施，但**还没**进 `INFRA`；
- 智能体面（`agent_*` / `tasks_*` / `worklines_*` / `federation`）**还没**纳入 `FEATURES`，因此横向 import 检查对它们不生效（如 `tasks_federation → agent_nodes`）。改这一片时靠人工守分层：路由 `*_api` → 编排 `*_runner` → 状态 `*_store`。

## 页面路由表与静态资源

页面是 `frontend/` 下的静态 HTML，由守卫保护。`/app` 是唯一壳入口（`app.html`），其余旧深链 302 到 `/app?view=...`，仅 `?embed=1` 时为壳内 iframe 直出：

| 路由 | 文件 | 行为 |
|---|---|---|
| `/app` | `frontend/app.html` | 单壳入口，多视图 iframe 常驻（`shell.js`） |
| `/tasks` | `frontend/tasks.html` | 任务页直出（也可在壳内以 iframe 打开） |
| `/m/share` | `frontend/m-share.html` | 手机分享表单：`?ticket=` 读暂存文件，三选一提交（见 [m-share.md](m-share.md)） |
| `/`、`/terminal`、`/ssh`、`/files`、`/screen`、`/command`、`/settings` | 旧页面 | 302 到 `/app?view=...`（`/ssh` → `view=session&kind=ssh`） |
| `/inbox` | — | 302 到 `/app?view=files&loc=inbox` |
| `/manifest.webmanifest` | `frontend/manifest.webmanifest` | PWA 清单，运行时替换品牌名（`name` / `short_name`） |
| `/sw.js` | `frontend/sw.js` | Service Worker，响应带 `Service-Worker-Allowed: /` 且 `no-cache` |

静态资源两条路径：

- `/assets/<path:filename>` → `frontend/assets/`，文本资源带 gzip（`gzipText`）；带 `?v=` 时 `immutable` 长缓存，否则 `no-cache`；
- 页面 HTML 由 `_makePageHandler` 读入并把 `__BUILD__` 替换为构建号，`Cache-Control: no-cache`。

## 认证守卫

实现在 `auth.py`，核心是 `before_request` 钩子 `authGuard`（`auth.py:129-172`），对**所有**路由生效，包括 WebSocket 升级请求。已登录（`session['authenticated']` 为真）直接放行；联邦门面（`X-Deskport-Token` 命中 `federationToken`）也直接放行。其余按请求形态分流：

| 请求形态 | 行为 |
|---|---|
| 豁免前缀（`EXEMPT_PREFIXES`：`/login`、`/api/auth/status`、`/api/agent/device/login`、`/assets/css/tokens.css`、`/assets/css/login.css`、`/manifest.webmanifest`、`/favicon`） | 放行——登录页、掉线探测、设备登录与 PWA 清单必须免认证可达 |
| 智能体令牌通道（`/api/agent/*`、`/api/tasks*`、`/api/worklines`） | 校验 `X-Agent-Token`；命中即放行（任务与工作线前缀要求 `full` scope，受限令牌 403 带 `need`），`/api/agent/*` 未命中令牌返回 401 |
| WebSocket 升级（`Upgrade: websocket` 头） | 返回 `401 Unauthorized`（握手失败，浏览器侧表现为 close code 1006） |
| `/api/*`（含插件与外部应用 API） | 返回 JSON `{"error": "unauthorized"}` + 401 |
| 其余（页面） | 302 重定向到 `/login?next=<原路径含查询串>` |

登录实现要点（`auth.py`）：

- 密码比对用 `hmac.compare_digest`，防时序侧信道；密码来自 `config.getPassword()`。
- 登录限速：按客户端 IP 滚动窗口计数，60 秒内 5 次失败后拒绝（`LOGIN_MAX_ATTEMPTS = 5`、`LOGIN_WINDOW_SECONDS = 60`，`auth.py:12-13`），超限提示"尝试次数过多"。
- `next` 参数经 `safeNextTarget` 校验：必须以 `/` 开头且不以 `//` 开头，否则回退 `/`，防开放重定向。
- 会话 Cookie：`HTTPONLY` + `SameSite=Lax`；`secret_key` 由密码派生（`HMAC(密码, "session-key-v1", sha256)`，`auth.py:66-68`；改密时在 `auth.py:118-120` 重新派生），因此**改密码后既有会话立即失效**（改密接口会同时清 `session`）。
- 登录页模板 `frontend/login.html` 含 `<!--ERROR-->` 与 `<!--NEXT-->` 占位符，由 `renderLoginPage` 做字符串替换后返回，不走 Jinja。

## MJPEG 单捕获广播（FrameBroadcaster）

代码：`modules/screen.py` 的 `FrameBroadcaster` 类（`screen.py:38-98`）。

**为什么存在**：旧架构（v1）每个 MJPEG 客户端各自起一条"抓屏 + JPEG 编码"循环。实测单帧 1080p 抓屏+编码约 108ms，N 个客户端即 N 次独立捕获——帧率除以 N，且 Windows 抓屏被驱动串行化、互相拖慢。修复思路：**一条捕获线程服务所有客户端**，各客户端只排队取最新帧。

工作机制：

- 引用计数：客户端接入 `/api/screen/stream` 时 `acquire()`（计数 +1，计数从 0 变 1 时启动捕获线程），断开时 `release()`（计数归 0 时置停机事件）。
- 捕获循环：`ImageGrab.grab()` → JPEG 编码（质量 80）→ 持锁更新 `frame` 与自增 `seq` → `notify_all()`。目标 10 FPS（`CAPTURE_INTERVAL = 0.1`），自适应间隔：本轮捕获耗时长则不额外等待。
- 消费端：`getFrame(lastSeq, timeout)` 阻塞等待比 `lastSeq` 新的帧，返回 `(jpeg, seq)`；客户端只跳过错过帧号的旧帧，不做历史补发。
- 异常处理：抓屏抛 `OSError`（锁屏 / RDP 断开 / 桌面切换）时保留上一帧、记录 `lastError`、跳过本轮重试。

前端（`frontend/assets/js/screen.js`）把 `<img src="/api/screen/stream">` 交给浏览器原生 multipart 渲染，`onerror` 后 1 秒重连（带 `?t=` 时间戳绕缓存）。

## 终端模块线程模型

核心代码：`modules/termcore.py`（`TermSession`、`SessionRegistry`、ConPTY 后端、reader/reaper 线程）；`modules/wsproto.py` 是协议层（帧编解码与 `attachAndPump` 附着泵）；`modules/terminal.py` 只做本地 ConPTY 的装配与 HTTP 端点，SSH 侧复用同一套内核（`ssh_terminal.py` 提供 `SSHBackend`）。会话对象与 WS 连接解耦——**进程在 WS 断开后继续存活**，这是断线重连与回放的基础。涉及三类线程：

| 线程 | 数量 | 职责 |
|---|---|---|
| reader 线程 | 每会话 1 条 | **同线程**内 `backend.spawn()`（ConPTY 即 `PtyProcess.spawn`）并持续 `backend.read()`；输出经 `enqueueOut` 入 `outQueue`（同时写入回放缓冲 `replayBuf`）。绝不调用 `ws.send` |
| handler 线程 | 每 WS 连接 1 条 | `wsproto.attachAndPump` 主循环：排空 `outQueue` 并 `ws.send` 转发、带超时收输入帧写入后端、做附着权检查 |
| reaper 线程 | 全局 1 条 | 每 10 秒扫描（`termcore.REAPER_INTERVAL`），回收"存活 + 无人附着 + 空闲超 `terminalIdleTimeoutHours`"的会话；该配置缺省为 0 = 永不回收 |

关键同步设计：

- `SessionRegistry`（`termcore.registry`）一把锁保护全局会话表；每会话 `sessionLock` 保护 `proc` / `alive` / `attached` 三个字段。
- `spawned` 事件：handler 等 spawn 就绪（最多 15 秒）才开始收输入，避免丢字。
- `outQueue` 封顶 `MAX_BACKLOG = 20000` 条，入队时丢弃最旧项。
- `replayBuf` 仅 reader 线程写，依赖单线程访问，不加锁（详见 [terminal-protocol.md](terminal-protocol.md)）。
- `ws.send` 全部收敛在附着的主循环线程；handler 退出时 `sealSocketAfterClose` 封锁 socket 写方向，阻断 werkzeug teardown 向已升级 socket 补写 HTTP 响应（"Invalid frame header" 根因修复）。

协议帧格式、回放顺序、抢占语义等对外行为见 [terminal-protocol.md](terminal-protocol.md)，该文是改动此模块前的必读文档。

## 前端组织方式

零构建链：不引 SFC/Vite/打包器，改文件刷新即生效；Vue 使用**全局构建**（`frontend/assets/vendor/vue.global.prod.js`，含运行时模板编译器）。所有三方件本地 vendored，无任何运行时 CDN 依赖。

两层结构：

1. **壳**（`app.html` + `shell.js`）：单文档多视图，每个视图是一个 iframe，首次进入时创建、之后常驻（`shell.js:71-83`）；切视图不重载页面，iframe 的 src 是既有页面加 `?embed=1`（`app.py` 的 `_makeLegacyHandler` 据此直出而不跳转）。深链 `/app?view=files&loc=inbox` 由壳解析。
2. **页面**（`frontend/*.html`）：既可在壳内当 iframe，也可被旧深链直出。每页引入 `nav.js`（导航唯一来源 `WP_NAV`）+ `app-shell.js`（顶栏 / 底部四项 / 更多弹层，只做派生不做定义）+ 自己的页面 JS。

```
frontend/
├── app.html                 壳（唯一入口，加载 shell.js）
├── index/terminal/ssh/files/screen/command/settings/inbox/tasks/m-share/login.html
│                            各视图页；m-share.html 是手机分享表单（无框架），
│                            login.html 免认证，占位符由 auth.py 填充
├── manifest.webmanifest     PWA 清单（服务端替换品牌名）
├── sw.js                    Service Worker（scope=/，仅安全上下文注册）
└── assets/
    ├── css/   tokens.css（唯一样式来源）+ app.css / shell.css / login.css /
    │          dashboard.css / files.css / inbox.css / tasks.css / settings.css /
    │          screen.css / upload.css / terminal{,-controls,-mobile}.css / xterm.min.css
    ├── js/    common.js（格式化/DOM/toast/fetchJson）、nav.js、messages.js（WP_MSG）、
    │          icons.js、app-shell.js、shell.js
    │          页面：dashboard / command / files-env + files-methods-* + files /
    │                screen / settings / tasks / inbox
    │          终端：term-client(-env) / term-features / term-controls / term-geometry /
    │                term-session-ui / term-sidebar / terminal
    │          SSH：ssh-page / ssh-ui(-ctx) / ssh-host-store /
    │               ssh-sftp-{state,menu,select,transfer}
    └── vendor/ vue.global.prod.js、xterm.min.js、xterm-addon-fit.min.js、
                xterm-addon-search.min.js、markdown-it.min.js
```

Vue 页面（`createApp(...).mount('#app')`）：`dashboard` / `command` / `files` / `screen` / `settings` / `tasks` / `inbox`。终端与 SSH 页不用 Vue 渲染主区域——xterm 写入、MJPEG、大列表这类高频数据直接命令式操作 DOM，Vue 只负责外围面板。

跨页契约：`nav.js` 的 `window.WP_NAV`（导航唯一定义）、`messages.js` 的 `window.WP_MSG`（壳 ↔ iframe 消息类型）、`window.WP_BRAND`；插件导航由服务端注入 `window.__WP_PLUGIN_NAV__`。新增 `window.*` 契约要过 `tools/check_structure.py` 的白名单。样式分层与组件类清单见 [design-system.md](design-system.md)。
