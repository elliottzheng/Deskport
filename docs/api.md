# API 参考

本文覆盖：当前全部业务端点（auth、system、command、screen、terminal）的方法、路径、参数、响应与错误码。
本文不覆盖：`/api/files*` 文件管理 API（已独立成篇，见 [api-files.md](api-files.md)）、终端 WS 帧语义（见 [terminal-protocol.md](terminal-protocol.md)）。

## 通用约定

- 除 `/login`、`/logout`、`/api/auth/status` 及豁免静态资源外，所有端点要求已登录会话（Cookie）。未登录访问 `/api/*` 统一返回 `401 {"error": "unauthorized"}`；页面请求被 302 到 `/login?next=...`；WS 握手被 401 拒绝。
- 错误响应统一为 JSON 对象，业务错误放 `error` 字段，HTTP 状态码表达类别（400 参数错误 / 401 未认证 / 404 不存在 / 500 执行失败 / 502 上游失败 / 504 超时）。
- 服务绑定 `0.0.0.0:6789`（可配置，见 [deployment.md](deployment.md)），以下示例省略主机。

## 认证

### `GET/POST /login`

登录页与登录提交。已登录访问时直接 302 到 `next`。

| 参数 | 位置 | 说明 |
|---|---|---|
| `next` | query（GET）或 form（POST） | 登录后跳转目标；必须以 `/` 开头且不以 `//` 开头，否则回退 `/` |
| `password` | form（POST） | 访问密码，与 `config.json` 的 `password` 恒时比较 |

| 响应 | 条件 |
|---|---|
| `200` 登录页 HTML（含 `<!--ERROR-->` 注入的错误提示） | GET，或 POST 密码错误 / 被限速 |
| `302 → next` | 密码正确（设置会话 Cookie） |
| `302 → next` | 已登录直接访问 |

限速：同一 IP 60 秒内 5 次密码错误后，登录被拒绝（提示"尝试次数过多"），需等最早的失败记录滑出窗口。

### `GET /logout`

清除会话，`302 → /login`。无参数。

### `GET /api/auth/status`

| 响应 | 条件 |
|---|---|
| `{"authenticated": true}` | 已登录 |
| `401 {"error": "unauthorized"}` | 未登录（守卫在路由前拦截，`{"authenticated": false}` 实际不可达） |

已知不一致（已于 2026-10-18 复核消除）：`/api/auth/status` 本身豁免守卫，未登录返回 200 `{"authenticated": false}`（实测确认），因此 `term-client.js` 的 `probeAuthAndRedirect` 分支可正常命中并跳转登录页；重构后前端额外把非 2xx 响应也视为未登录，双保险。

## 系统信息

### `GET /api/system`

psutil 实时快照。无参数。

```json
{
  "pcName": "DESKTOP-XXX",
  "user": "alice",
  "os": "Windows 11",
  "python": "3.13.16",
  "memory": {"percent": 61.2, "used": 13000000000, "total": 34000000000, "available": 21000000000},
  "uptimeSeconds": 86240,
  "interfaces": [{"name": "以太网", "ip": "192.168.1.10", "linkLocal": false}]
}
```

- `user`：取 `psutil.users()` 最后一个登录用户，无登录用户时为 `"-"`。
- `interfaces`：全部网卡的 IPv4；`169.*` 链路本地地址 `linkLocal` 为 `true`，前端降序展示且置灰。
- `memory.*` 单位字节；`uptimeSeconds` 基于进程启动时缓存的 `psutil.boot_time()`。

## 命令执行

### `POST /api/command`

Content-Type: `application/json`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cmd` | string | 是 | 经 `shell=True`（cmd.exe）执行的单条命令；空/纯空白返回 400 |

成功 `200`：

```json
{"exitCode": 0, "stdout": "...", "stderr": "..."}
```

超时 `504`（120 秒，`TIMEOUT_SECONDS`）：先 `taskkill /F /T /PID` 杀整棵进程树（直接 kill cmd.exe 不终止孙进程、管道挂死），再返回**已捕获的部分输出**：

```json
{"error": "执行超时 (120s), 已终止进程树", "exitCode": null, "stdout": "...", "stderr": "..."}
```

输出解码：子进程字节流依次尝试 UTF-8、GBK，均失败则以 UTF-8 `replace` 容错解码（`modules/command.py` `_decode`）。

## 实时屏幕

### `GET /api/screen/stream`

MJPEG 长响应，`Content-Type: multipart/x-mixed-replace; boundary=frame`。浏览器直接作为 `<img src>` 消费，无参数、无中间 JSON。服务端单捕获线程 10 FPS 上限、JPEG 质量 80；第一个客户端接入才开始抓屏，全部断开后停止。连接断开（页面关闭/网络中断）即释放配额。

### `GET /api/screen/control`

键鼠注入，全部参数走 query string（GET）。

`controlType=mouse`：

| 参数 | 取值 | 说明 |
|---|---|---|
| `x`、`y` | 整数 | 目标屏幕坐标（像素，左上原点）；缺失 400 `缺少坐标`，非整数 400 `坐标无效` |
| `action` | `move` 或省略 | `move` 仅移动光标；其余值（含省略）为移动 + 单击 |
| `button` | `left`（默认）/ `right` | 单击键位 |

成功 `{"ok": true}`；pynput 注入异常 500。

`controlType=keyboard`：

| 参数 | 取值 | 说明 |
|---|---|---|
| `key` | 组合键串，`+` 分隔 | 如 `ctrl+c`、`shift`、`enter`、`f5`；缺失 400 `缺少按键` |

- 修饰键：`ctrl` / `alt` / `shift` / `cmd`——按下后保持，最后按逆序释放。
- 命名键：`enter` `space` `tab` `backspace` `delete` `esc` `up` `down` `left` `right` `home` `end` `page_up` `page_down` `insert` `menu` `pause` `print_screen` `caps_lock` `num_lock` `scroll_lock` 及 `f1`-`f24`。
- 单字符按键直接注入；未知名称 400 `未知按键: <名>`。
- 修饰键按下后若中途异常，兜底尝试全部释放并返回 500。

其他 `controlType` 值：400 `无效操作`。

## 终端

### `GET /api/terminal/sessions`

存活会话列表，供页面刷新后恢复。无参数。

```json
{"sessions": [{"id": "8f3...hex", "cmd": "pwsh.exe", "attached": false}]}
```

`id` 为 32 位十六进制会话 ID（`uuid4().hex`）；仅返回 `alive` 的会话；`attached` 表示当前是否有 WS 附着（被占用的会话前端恢复时会避开）。

### `WS /api/terminal/ws`

终端通道。连接参数（query string）：

| 参数 | 说明 |
|---|---|
| `cmd` | 终端程序，如 `cmd.exe`、`powershell.exe`、`pwsh.exe`；缺省为 `pwsh.exe`（PATH 上无则 `cmd.exe`） |
| `cwd` | 初始工作目录，缺省当前用户主目录 |
| `session` | 既有会话 ID，命中且存活则附着复用（断线重连）；否则新开会话 |
| `replay` | `=1` 且为复用时，先回放历史输出再接实时流 |

帧格式、hello/exit/error/resize/ping/close 语义、回放顺序保证、抢占接管与硬约束见 [terminal-protocol.md](terminal-protocol.md)，该文为协议的唯一权威描述。

## 文件管理

`/api/files*` 全部端点（浏览/下载/上传/远程下载/启动/重命名/新建/回收站与永久删除/打包下载/解压/压缩/搜索）见 [api-files.md](api-files.md)。
