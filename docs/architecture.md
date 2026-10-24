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
   ├─ modules/files.py     /api/files*（browse/transfer/archive/pins）
   ├─ modules/control.py   控制台（进程/窗口/电源/审计）+ control_input
   ├─ modules/terminal.py  本机终端 WS（或经 sessionproxy 转发到 locald）
   ├─ modules/ssh.py       SSH/SFTP（或经 sessionproxy 转发到 sshd）
   └─ modules/tasks.py     任务/AI 交接 + aihandoff
```

服务运行方式：`app.run(host='0.0.0.0', port=<config>, debug=False, threaded=True)`。即 Flask 内置 werkzeug 服务器，每请求一线程；无 debug、无 reloader。

## 模块注册约定

入口 `app.py` 对业务模块不做逐个 import 配线，而是按约定遍历注册（`app.py:49-60`）：

```python
# app.py: 显式注册表（不再用 hasattr 猜）
BLUEPRINT_MODULES = (filesModule, commandModule, screenModule, aiModule,
                     controlModule, tasksModule)
REGISTER_MODULES = (systemModule, metaModule, tasksModule)

for module in REGISTER_MODULES:
    module.register(app)
for module in BLUEPRINT_MODULES:
    app.register_blueprint(module.blueprint)
```

`create_app()` 负责装配并返回 `(app, sock)`；模块级 `app, sock = create_app()` 只为兼容既有入口。新增业务模块时，在 `app.py` 的两个元组之一登记，并按约定暴露 `blueprint` 或 `register(app)`。

当前各模块的实际注册方式：

| 模块 | 注册方式 | 说明 |
|---|---|---|
| `auth.py` | `register(app)` | `/login`、`/logout`、`/api/auth/status` 与 `before_request` 守卫 |
| `modules/system.py` | `register(app)` | 单端点模块 |
| `modules/meta.py` | `register(app)` | `/api/version`、`/api/diagnostics`、`/api/backup` |
| `modules/tasks.py` | `blueprint` + `register(app)` | 蓝图注册路由，`register` 建任务根目录并加载仓储 |
| `modules/files.py` | `blueprint` | 门面导出 `files_base.blueprint`，子模块挂路由 |
| `modules/command.py` | `blueprint` | — |
| `modules/screen.py` | `blueprint` | MJPEG 流与键鼠控制 |
| `modules/aihandoff.py` | `blueprint` | 与 inbox_scan 共用 `inbox_base.blueprint` |
| `modules/control.py` | `blueprint` | 控制台；`control_input` 导入时挂子路由 |
| `modules/ssh.py` | `register(app, sock)` | 需要统一 `Sock` 注册 WS，并按配置决定是否代理到 sessiond |
| `modules/terminal.py` | `register(app, sock)` | 同上，本机终端 |

## 页面路由表与静态资源

页面是 `frontend/` 下的静态 HTML，由守卫保护。`/app` 是唯一壳入口（`app.html`），其余旧深链 302 到 `/app?view=...`，仅 `?embed=1` 时为壳内 iframe 直出：

| 路由 | 文件 | 行为 |
|---|---|---|
| `/app` | `frontend/app.html` | 壳入口 |
| `/tasks` | `frontend/tasks.html` | 任务深链 |
| `/`、`/terminal`、`/files`、`/screen`、`/command`、`/settings` | 旧页面 | 302 到 `/app?view=...` |
| `/inbox` | — | 302 到 `/app?view=files&loc=inbox` |

静态资源两条路径：

- `/assets/<path:filename>` → `frontend/assets/`，文本资源带 gzip（`gzipText`）；带 `?v=` 时 `immutable` 长缓存，否则 `no-cache`；
- 页面 HTML 由 `_makePageHandler` 读入并把 `__BUILD__` 替换为构建号，`Cache-Control: no-cache`。

## 认证守卫

实现在 `auth.py`，核心是 `before_request` 钩子 `authGuard`，对**所有**路由生效，包括 WebSocket 升级请求。已登录（`session['authenticated']` 为真）直接放行；未登录时按请求形态分流：

| 请求形态 | 行为 |
|---|---|
| 豁免前缀（`EXEMPT_PREFIXES`：`/login`、`/assets/css/tokens.css`、`/assets/css/login.css`、`/favicon`） | 放行——登录页及其自身依赖的资源必须免认证可达 |
| WebSocket 升级（`Upgrade: websocket` 头） | 返回 `401 Unauthorized`（握手失败，浏览器侧表现为 close code 1006） |
| `/api/*` | 返回 JSON `{"error": "unauthorized"}` + 401 |
| 其余（页面） | 302 重定向到 `/login?next=<原路径含查询串>` |

登录实现要点（`auth.py`）：

- 密码比对用 `hmac.compare_digest`，防时序侧信道；密码来自 `config.getPassword()`。
- 登录限速：按客户端 IP 滚动窗口计数，60 秒内 5 次失败后拒绝（`LOGIN_MAX_ATTEMPTS = 5`、`LOGIN_WINDOW_SECONDS = 60`，`auth.py:11-12`），超限提示"尝试次数过多"。
- `next` 参数经 `safeNextTarget` 校验：必须以 `/` 开头且不以 `//` 开头，否则回退 `/`，防开放重定向。
- 会话 Cookie：`HTTPONLY` + `SameSite=Lax`；`secret_key` 由密码派生（`HMAC(密码, "session-key-v1", sha256)`，`auth.py:58-60`），因此**改密码需重启才会使既有会话失效**（密钥只在启动时派生一次），但新登录即时使用新密码（`getPassword()` 每次读盘）。
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

代码：`modules/terminal.py`。会话对象 `TermSession` 与 WS 连接解耦——**进程在 WS 断开后继续存活**，这是断线重连与回放的基础。涉及三类线程：

| 线程 | 数量 | 职责 |
|---|---|---|
| reader 线程 | 每会话 1 条 | **同线程**内 `PtyProcess.spawn` 并持续 `proc.read()`；输出经 `enqueueOut` 入 `outQueue`（同时写入回放缓冲 `replayBuf`）。绝不调用 `ws.send` |
| handler 线程 | 每 WS 连接 1 条 | `wsTerminal` 主循环：排空 `outQueue` 并 `ws.send` 转发、带 0.05s 超时收输入帧写入 pty、做附着权检查 |
| reaper 线程 | 全局 1 条 | 每 10 秒扫描（`REAPER_INTERVAL`），回收"存活 + 无人附着 + 空闲超 `terminalIdleTimeoutHours`"的会话；该配置缺省为 0 = 永不回收 |

关键同步设计：

- `sessionMapLock` 保护全局 `sessionMap`；每会话 `sessionLock` 保护 `proc` / `alive` / `attached` 三个字段。
- `spawned` 事件：handler 等 spawn 就绪（最多 15 秒）才开始收输入，避免丢字。
- `outQueue` 封顶 `MAX_BACKLOG = 20000` 条，入队时丢弃最旧项。
- `replayBuf` 仅 reader 线程写，依赖单线程访问，不加锁（详见 [terminal-protocol.md](terminal-protocol.md)）。
- `ws.send` 全部收敛在附着的主循环线程；handler 退出时 `sealSocketAfterClose` 封锁 socket 写方向，阻断 werkzeug teardown 向已升级 socket 补写 HTTP 响应（"Invalid frame header" 根因修复）。

协议帧格式、回放顺序、抢占语义等对外行为见 [terminal-protocol.md](terminal-protocol.md)，该文是改动此模块前的必读文档。

## 前端组织方式

零构建链：不引 SFC/Vite/打包器，改文件刷新即生效；Vue 使用**全局构建**（`frontend/assets/vendor/vue.global.prod.js`，含运行时模板编译器）。所有三方件本地 vendored，无任何运行时 CDN 依赖。

```
frontend/
├── index.html / terminal.html / screen.html / files.html / command.html / login.html
└── assets/
    ├── css/
    │   ├── tokens.css        设计令牌 (全站唯一样式来源, 登录页也引用)
    │   ├── app.css           基础样式 + 共享组件类
    │   ├── login.css         登录页专属 (免认证白名单内, 故独立于 app.css)
    │   ├── terminal.css      终端工作台布局
    │   ├── screen.css        屏幕页专属
    │   └── xterm.min.css     vendored
    ├── js/                   每页一个 JS, 与页面同名 (dashboard/command/files/screen/terminal)
    └── vendor/               vue.global.prod.js / xterm.min.js / xterm-addon-fit.min.js
```

每页固定模式：HTML 壳内联顶栏/底栏结构 + `<div id="app" v-cloak>` 容器，`createApp({...}).mount('#app')` 由同名页面 JS 完成；键盘输入等高频实时数据（xterm 写入、MJPEG）绕过 Vue 响应式，直接命令式操作 DOM。样式分层与组件类清单见 [design-system.md](design-system.md)。
