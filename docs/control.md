# 控制台（整机操作）

首页 `/` 从"系统信息"升级为控制台：电源、进程、窗口、应用、文本、审计。
面向手机远控——不想开 RDP 完成的小操作，在这里一次点击完成。

## 一、页面能力

| 区块 | 内容 |
|---|---|
| 状态条 | 主机/用户、运行时长、CPU、内存、各盘可用空间 |
| 快动作 | 音量 ± / 静音 / 播放暂停 / 锁屏 / 关显示器 / 睡眠 / 看屏幕 |
| 电源 | 重启、关机；支持立即 / 1 分钟 / 5 分钟，计划后可取消 |
| 进程 | 按内存或 CPU 排序、名称搜索、结束（二次确认，可强制） |
| 窗口 | 可见窗口列表，切换 / 关闭（WM_CLOSE，应用可弹保存提示） |
| 常用应用 | `pinnedApps` 固定项 + 开始菜单搜索启动 |
| 发送文本 | 选择目标窗口 → 粘贴文字 → 可选回车发送（微信回复用） |
| 最近操作 | 审计日志最近 20 条 |

桌面三列、移动单列；触控目标高度 ≥44px。

## 二、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/control/summary` | 主机/CPU/内存/磁盘/固定应用/开始菜单应用/审计 |
| GET | `/api/control/processes?sort=mem\|cpu&q=` | 进程前 60 |
| POST | `/api/control/process/close` | `{pid, force}` |
| GET | `/api/control/windows` | 可见窗口（标题/进程/PID/hwnd） |
| POST | `/api/control/window` | `{hwnd, action: focus\|minimize\|close}` |
| POST | `/api/control/launch` | `{path}`，只允许开始菜单或 pinnedApps 内路径 |
| POST | `/api/control/power` | `{action: lock\|sleep\|display_off\|shutdown\|restart\|abort, delay, dryRun}` |
| POST | `/api/control/keys` | `{action: volume_up\|volume_down\|mute\|play_pause\|next\|prev, repeat}` |
| POST | `/api/control/text` | `{text, submit, hwnd}`，复制到剪贴板后 Ctrl+V，可回车 |
| GET | `/api/control/audit` | 审计日志 |

所有写操作支持 `dryRun: true` 只校验不执行（自动化测试用）。

## 三、配置

```json
{
  "pinnedApps": [
    "C:\\\\Program Files\\\\Example\\\\App.exe",
    "File Explorer",
    "Notepad3"
  ]
}
```

- 数组元素可以是**开始菜单显示名**，也可以是**直接的 .exe/.lnk 路径**。
- 开始菜单应用自动扫描（ProgramData + 用户 Start Menu），结果缓存 120 秒。

## 四、安全边界

- 结束进程：平台自身与 `csrss/winlogon/lsass/services/explorer` 等关键进程拒绝；
  非强制先 `terminate()`，3 秒不退再 `kill()`。
- 关机/重启：前端二次确认并选择延迟；服务端限 0–3600 秒。
- 启动应用：路径必须在开始菜单索引或 `pinnedApps` 白名单内。
- 窗口关闭用 `WM_CLOSE`，不是强杀，应用可弹保存确认。
- 所有动作写 `%APPDATA%\<stateDir>\control_audit.jsonl`（时间/动作/对象/结果/IP）。
- 重启/关机后平台需要有人登录才会自启，页面已明示。

## 五、已知限制

- 微信回复依赖窗口焦点与键盘注入，没有消息级 API；发送前请确认目标窗口。
- 亮度未纳入（台式屏无统一接口）；音量走媒体键，粒度为系统步进。
- 开始菜单扫描不覆盖 Store 应用（UWP）。
- 关机/重启不做真实自动化测试，只用 `dryRun` 校验命令。