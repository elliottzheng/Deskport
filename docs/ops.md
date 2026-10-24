# 发布与运维

面向单用户、Windows 本机的运维手册：发布、回滚、版本、诊断、守护与日志。

## 一、发布：`deploy.ps1`

`deploy.ps1` 假定源目录（当前目录）与目标目录是同级兄弟目录，目标目录名固定为 `web-platform`。按需修改脚本顶部的 `$dst` 即可。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy.ps1
```

流程：

1. **预检**：git 工作区（脏只警告）、`py_compile`、`node --check`、`tools/check_structure.py`（含 pyflakes）。
2. **备份**：`.deploy-backup/<时间戳>/` 保存目标 `config.json`、状态目录顶层文件、目标源码副本。
3. **同步**：robocopy 源 → 目标，排除 `config.json`、日志、`.venv`、`.git`、`.run`、`.deploy-backup`。
4. **按需重启**：守护进程代码有变化才重启对应 sessiond；web 重启；watchdog 兜底。
5. **冒烟**：登录、`/api/version`、控制台 summary、inbox、终端会话列表、守护进程健康。
6. **失败回滚**：恢复源码与 config，重启受影响进程，退出码 1。

参数：`-DryRun`（只预检）、`-SkipWebRestart`、`-SkipSmoke`、`-SkipDaemonRestart`、`-ForceDaemonRestart`。

注意：web 重启不杀终端/SSH 会话；守护进程代码变化才会中断对应会话。

## 二、版本与诊断

| 接口 | 内容 |
|---|---|
| `GET /api/version` | build、启动时间、Python、协议版本、两个守护进程健康 |
| `GET /api/diagnostics` | 版本、PID、内存、磁盘、监听端口、会话数、守护进程 PID、日志大小与最近错误、状态目录 |

控制台的「诊断」按钮展示 diagnostics JSON；状态条显示三个服务的正常/离线与会话数。

## 三、守护与日志

- `watchdog.py`：每 30 秒检查 web/locald/sshd，连续 2 次失败用对应 vbs 拉起；文件锁保证单实例；日志 `watchdog.log`。
- `start-*.vbs` 在启动时把对应日志按 10MB 轮转为 `.1`（Windows 下运行中的日志不能改名，因此只在启动时轮转）。
- 自启入口：启动文件夹快捷方式 → `start-hidden.vbs`。该脚本先拉起两个 sessiond 与 watchdog，再启动 web；每个脚本自带端口检查，重复调用无害。
- 源码目录里的日志（`run.log`、`locald.log`、`sshd.log`、`watchdog.log`）均被 `.gitignore` 排除。

## 四、静态资源缓存

- 页面里的 `__BUILD__` 在服务端替换为当前 build，资源 URL 形如 `app.css?v=dfde2fa`。
- 带 `?v=` 的资源返回 `Cache-Control: public, max-age=31536000, immutable`；不带版本回退 `no-cache`。
- 页面 HTML 返回 `Cache-Control: no-cache`。

## 五、状态与事件

- `modules/state.py`：`atomicWriteJson`（临时文件 + `os.replace`）与 `appendJsonl`（`.lock` 文件跨进程排它）。
- `modules/events.py`：统一事件流 `%APPDATA%\<stateDir>\control_audit.jsonl`，登录、危险操作、应用启动共用。
- 设置页「导出备份」：`GET /api/backup` 打包 `config.json` 与状态目录（跳过缩略图/锁文件/超大文件）。**压缩包内含密码与令牌，勿外传。**

## 六、测试

```bash
# 单元测试（无需服务）
.venv\Scripts\python.exe -m pytest tests -q

# 结构门禁
.venv\Scripts\python.exe tools/check_structure.py

# 冒烟（需服务已启动；默认读取 config.json 的端口与密码）
.venv\Scripts\python.exe tests\smoke.py

# 协议契约（需服务已启动）
set WP_RUN_PROTOCOL=1
.venv\Scripts\python.exe -m pytest tests\test_protocol.py -q
```

## 七、已知边界

- 默认 HTTP、无 TLS/TOTP/CSRF；远程访问请置于 HTTPS 反向代理或 VPN 之后。
- 状态使用 JSON + 原子写，适合单用户规模；未引入数据库。
- 前端为 Vue 全局构建，无打包链；修改后刷新即可，发布时依赖 `?v=` 缓存戳。
