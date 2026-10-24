# web-platform

Windows 远程控制台：在浏览器/手机上管理本机终端、SSH、文件、屏幕与常用操作。

单用户、单机部署，Flask + 原生前端（Vue 3 全局构建，无前端构建链），支持 PWA 安装到手机桌面。

> 本项目受 [mc-lhz/RemoteConnecter](https://github.com/mc-lhz/RemoteConnecter) 启发，并大量借鉴和沿用了其代码与实现思路；详见文末[致谢与代码来源](#致谢与代码来源)。

## 功能

- **本机终端**：ConPTY 多会话、断线重连、回放、抢占接管；会话常驻，刷新页面不丢
- **SSH**：密码/密钥认证、keyboard-interactive（2FA）、known_hosts TOFU、SFTP 浏览/上传/下载/编辑
- **文件管理**：磁盘浏览、快速访问、缩略图、文本预览（保留原编码）、回收站删除、压缩/解压、搜索、打包下载
- **收件箱**：默认关闭，可在设置页开启并维护扫描目录；内置下载/桌面/Documents 微信/QQ 默认目录
- **实时屏幕**：MJPEG 单捕获广播（多客户端不降帧），鼠标/键盘控制
- **控制台**：进程/窗口/应用管理，关机/重启/锁屏/睡眠，音量键，向指定窗口粘贴文本
- **任务与 AI 交接**：把文件/文件夹交给终端 AI CLI，任务常驻、可继续对话、权限请求可见
- **PWA**：手机可安装为独立应用，记住视图状态

## 环境要求

- Windows 10/11（终端、抓屏、键鼠注入依赖 Windows API）
- [uv](https://docs.astral.sh/uv/)（自动下载 Python 3.13 与依赖）
- 可选：PowerShell 7（`pwsh.exe`），未安装时回退 Windows PowerShell 5.1

## 快速开始

```bash
uv sync
copy config.example.json config.json   # 设置 password
.venv\Scripts\python.exe app.py        # http://127.0.0.1:6789
```

`config.json` 不入库。最小配置：

```json
{
  "port": 6789,
  "password": "换成一个强密码"
}
```

更多字段见 [docs/deployment.md](docs/deployment.md)。密码为空时无人能登录，这是有意的 fail-closed 行为。

### 开机自启（可选）

创建启动文件夹快捷方式：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\web-platform.lnk
  → wscript.exe "<仓库路径>\start-hidden.vbs"
```

`start-hidden.vbs` 会以隐藏窗口启动 web、两个会话守护进程和 watchdog，并写 `run.log`。
详细说明（防双开、日志轮转、取消自启）见 [docs/deployment.md](docs/deployment.md)。

## 测试

```bash
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe tools/check_structure.py
```

终端/SSH 的端到端验证需要先启动服务（`app.py` + 两个 `sessiond.py`），见 [docs/ops.md](docs/ops.md)。

## 架构

```
浏览器 ──HTTP/WS──> web(6789, 认证/UI/文件/控制台)
                        ├──WS 代理──> locald(6791, ConPTY 终端)
                        └──WS 代理──> sshd  (6792, paramiko SSH)
```

- web 重启不影响终端/SSH 会话
- 状态与凭据存放在 `%APPDATA%\<stateDirName>`，不进仓库
- 模块分层、依赖方向与文件预算见 [docs/code-structure.md](docs/code-structure.md)

## 安全说明

- 单用户、单密码；密码以明文存于本机 `config.json`
- 默认 HTTP、无 TLS/TOTP；**不要直接暴露到公网**，如需远程访问请置于 HTTPS 反向代理/VPN 之后
- 服务具备完整本机控制能力，请使用强密码并限制网络入口

## 文档

见 [docs/README.md](docs/README.md)。

## 许可证

[MIT](LICENSE)。第三方依赖的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 致谢与代码来源

本项目受到 [mc-lhz/RemoteConnecter](https://github.com/mc-lhz/RemoteConnecter) 的启发，并**大量借鉴和沿用了该项目的代码与实现思路**。终端 WebSocket 协议、ConPTY 会话管理、MJPEG 单捕获广播、认证守卫、文件管理、SSH/SFTP 等模块都能看到原项目的影子；v2 在此基础上做了重构、补强与扩展。

原项目版权归其作者所有。上游仓库当前未声明开源许可证，使用、修改或分发本项目时，请同时确认上游代码的授权情况；如果你是上游作者或已获得授权，请在本仓库补充相应说明。
