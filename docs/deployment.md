# 部署与运维

本文覆盖：环境搭建（uv + Python 3.13）、`config.json`、启动方式、端口与防火墙、开机自启、依赖清单。
系统架构见 [architecture.md](architecture.md)，接口见 [api.md](api.md)。

## 环境要求

- **Windows 10/11**：终端依赖 pywinpty/ConPTY，抓屏依赖 `PIL.ImageGrab`，超时杀树依赖 `taskkill`，键鼠注入依赖 pynput。
- **uv**：包与 Python 版本管理（https://docs.astral.sh/uv/）。
- **Python 3.13**：`pyproject.toml` 声明 `requires-python >= 3.13`，`.python-version` 锁定 `3.13`；`uv sync` 首次运行会自动下载解释器。

## 安装与启动

```bash
uv sync
copy config.example.json config.json    # 填写密码
.venv\Scripts\python.exe app.py         # http://127.0.0.1:6789
```

启动行为：绑定 `0.0.0.0:<port>`，`debug=False`（无 reloader）、`threaded=True`（MJPEG 长响应与 WebSocket 依赖该模式）。使用 Flask 内置 werkzeug 服务器。

## config.json

最小配置：

```json
{"port": 6789, "password": "在这里填写你的访问密码"}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` | `6789` | 服务端口；仅启动时读取，改后重启 |
| `password` | `""` | 访问密码，明文存储；登录时实时读取，改后新登录立即生效 |
| `stateDirName` | `"web-platform"` | `%APPDATA%` 下运行时状态目录名；多实例时用不同名字隔离 |
| `terminalIdleTimeoutHours` | `0` | 无人附着的终端/SSH 会话回收宽限期；0 = 永不回收 |
| `workDirs` | 用户主目录 + `Documents` | AI 落点候选目录 |
| `fallbackDir` | `<第一个 workDir>\默认工作空间` | 低置信度兜底目录 |
| `inboxEnabled` | `false` | 收件箱扫描开关；关闭时 `inboxDirs` 不生效，可在设置页开启 |
| `inboxDirs` | 软件默认目录 | 收件箱观察目录；开启且为空 = 用户主目录下的下载/桌面/Documents 微信/QQ 目录 |
| `brandName` | `"Deskport"` | 顶栏、登录页与 PWA 名称；也可在设置页修改 |
| `aiCommand` | 自动 | 终端里启动的 AI CLI；空 = 探测 `omp` / `kimi`，都无则 `pwsh.exe` |
| `decisionApiKey` | 空 | SiliconFlow Key；空则使用本地词面匹配 |
| `pinnedApps` | `[]` | 控制台固定应用：开始菜单名或 `.exe/.lnk` 路径 |
| `sessiondUrl` / `sshSessiondUrl` | 空 | 会话守护进程地址；空 = 留在 web 进程内 |

改密细节：

- **密码为空 = 无人能登录**：`auth.py` 的比对是 `if expect and compare_digest(...)`，空密码时条件恒假。
- **改密后旧会话不立即失效**：`secret_key` 由密码派生，但只在进程启动时计算一次；需要踢掉旧会话时重启进程。
- 配置文件缺失或损坏时回退默认值，服务可启动但处于空密码状态；排查登录问题先确认 `config.json` 可读。

## 端口与防火墙

- 默认 **6789**，绑定 `0.0.0.0`，局域网设备可经 `http://<本机IP>:6789` 访问。
- 页面、API、MJPEG、终端 WebSocket 共用同一端口，无需单独放行 WS。
- Windows 防火墙首次启动可能弹授权；也可手工放行 TCP 6789。
- 服务具备终端/命令/文件/键鼠能力，**不要直接暴露公网**；建议内网或 HTTPS 反向代理/VPN 之后使用。

## 开机自启（启动文件夹）

创建快捷方式：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\web-platform.lnk
  → wscript.exe "<仓库路径>\start-hidden.vbs"
```

`start-hidden.vbs` 会：

- 以隐藏窗口（`WshShell.Run` 样式 0）启动 `app.py`，`cmd >> run.log` 追加日志；
- 依次拉起 `start-locald.vbs`、`start-sshd.vbs`、`start-watchdog.vbs`；
- 探测 `http://127.0.0.1:<port>/login`，已有实例时直接退出，避免双开。

**不要改用 `WshShell.Exec`**：Exec 会给子进程分配可见控制台窗口。

取消自启：删除 Startup 目录中的快捷方式即可。重建：

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$([Environment]::GetFolderPath('Startup'))\web-platform.lnk")
$lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments  = '"<仓库路径>\start-hidden.vbs"'
$lnk.WorkingDirectory = '<仓库路径>'
$lnk.Save()
```

## 多实例（开发/测试）

复制一份目录，复制 `config.example.json` 为 `config.json`，改 `port` 与 `stateDirName`：

```json
{"port": 6788, "password": "dev-password", "stateDirName": "web-platform-dev",
 "sessiondUrl": "http://127.0.0.1:6891", "sshSessiondUrl": "http://127.0.0.1:6892"}
```

两个实例的状态目录、端口、会话守护进程互不影响。`deploy.ps1` 假定源目录与目标目录是同级兄弟目录，按需使用或忽略。

## 依赖清单

直接依赖（`pyproject.toml`，版本以 `uv.lock` 为准）：

| 包 | 用途 |
|---|---|
| flask / flask-sock / simple-websocket | Web 与 WebSocket |
| psutil | 系统信息与进程管理 |
| pillow | 抓屏与 JPEG/缩略图 |
| pynput | 键鼠注入 |
| pywinpty | ConPTY 终端 |
| requests | 远程下载与内部健康检查 |
| paramiko | SSH/SFTP |

测试依赖（dev 组）：pytest、websocket-client、pyflakes。

## 升级

改 `pyproject.toml` 后执行 `uv sync`，`uv.lock` 随之更新并入库。运行 `python -m pytest tests -q` 与 `python tools/check_structure.py` 后再发布。
