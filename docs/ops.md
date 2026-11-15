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
| `GET /api/diagnostics` | 版本、PID、内存、磁盘、监听端口、会话数、守护进程 PID、日志大小与最近错误、状态目录、`taskLogWriteFailures`（任务流水写失败计数，见下） |

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
- 状态目录所有者锁（批次 L L6，`modules/state_owner.py`）：web 启动即在状态目录取独占锁（`owner.lock`：锁文件 + 进程 id，OS 级字节区排它）。其它进程（探针脚本 / locald / sshd / 第二个 web）再碰 TaskRepository / feedback / leads / worklines 等存储时进入**只读模式**：读放行，任何 save/append 抛「状态目录 \<path\> 属于进程 \<pid\>，本进程只读」并记日志。进程退出（含被 kill）即释放锁——watchdog 重启时旧 web 一走锁就没，新 web 最多等 15s 接过；线上启动顺序不变。

## 六、任务记录的存储形态（批次 PS1 起）

- `agent_tasks.json` 只存任务元数据：`save()` 序列化时剥掉每个任务的 `events` 键与所有 `_` 开头运行时缓存键（`_toolArgText` / `_toolArgs` / `_lastToolCall` / `_assistantBuf` 等，只对当前进程有意义，重启后本就无用）。任务卡从 ~1.7KB/任务 的元数据计，写盘量降约 95 倍。
- 事件的唯一真相是 `agent_logs/<任务id>.jsonl`（每个事件一行，`assistant_chunk` 也在里面）；内存只保留最近 2000 条窗口，任务首次被 `get()` / `list()` 访问时从 jsonl 尾部惰性回填（启动不做全量回填），`seq` 与 jsonl 里的最大 seq 取大校正——"jsonl 写了、快照没写"的崩溃中间态不会产生重复 seq。
- 回填窗口与重启前内存窗口逐字段相等（写入端与回填共用同一套截断规则：assistant / tool args / plan 8000 字、tool_result 4000 字、user 不截）。窗口外的旧事件今天本来就不从 `/events` 返回，行为不变。
- 已知行为变化：重启后进行中工具调用的审批可能从自动放行退回询问——policy 的路径判定要回查流式参数（`_toolArgs` / `_toolArgText`），这些运行时缓存不落盘，重启后查不到只能按缺省 `ask` 挂起等人工。方向上更保守（不会更不安全），详见 `docs/agent-api.md`「重启语义」。
- `GET /api/diagnostics` 的 `taskLogWriteFailures`：jsonl 写失败计数（重试 3 次仍败才计一次，事件仍留在当进程内存窗口里）。非 0 说明有事件只进了内存、重启后丢失，请查磁盘空间与权限。

## 七、任务记录回滚：把 .bak 改回 agent_tasks.json 再回滚代码

首次用新代码加载旧格式 `agent_tasks.json`（任务卡带 `events` 键）时，进程会在第一次写盘之前把原文件复制为同目录的 `agent_tasks.json.pre-migration-<YYYYMMDD-HHMMSS>.bak`（已存在同名则不覆盖；复制失败则本进程不剥离、按旧格式继续写——宁可不瘦身也不丢原件）。回滚步骤：

1. 停 web（按发布流程停对应进程，确保没有进程在写状态目录）。
2. 把最新的 `.pre-migration-*.bak` 复制/改回 `agent_tasks.json`（想留新格式文件就先另存）。
3. 回滚代码到批次 PS1 之前的版本。
4. 起服务验证：任务列表、详情历史事件可见、审批可批、续聊正常。

注意：新代码直接读旧格式文件也完全兼容（`events` 在字典里，照常工作），所以回滚不要求先回滚代码；但**旧代码读新格式文件时历史事件不可见**（内存窗口为空，SSE/`/events` 返回空历史），要完全恢复旧体验必须连 `.bak` 一起恢复。

## 八、测试

```bash
# 单元测试（无需服务）
.venv\Scripts\python.exe -m pytest tests -q

# 结构门禁
.venv\Scripts\python.exe tools/check_structure.py
# 前端静态门禁（资源/id/调用目标/API 端点）
.venv\Scripts\python.exe tools/check_frontend.py

# 冒烟（需服务已启动；默认读取 config.json 的端口与密码）
.venv\Scripts\python.exe tests\smoke.py

# 协议契约（需服务已启动）
set WP_RUN_PROTOCOL=1
.venv\Scripts\python.exe -m pytest tests\test_protocol.py -q
```

## 九、已知边界

- 默认 HTTP、无 TLS/TOTP/CSRF；远程访问请置于 HTTPS 反向代理或 VPN 之后。
- 状态使用 JSON + 原子写，适合单用户规模；未引入数据库。
- 前端为 Vue 全局构建，无打包链；修改后刷新即可，发布时依赖 `?v=` 缓存戳。
