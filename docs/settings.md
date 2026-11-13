# 设置 — 密码、收件箱、品牌、登录设备与上传目标

单用户系统，设置页负责修改访问密码、收件箱扫描开关、品牌名称，以及管理登录设备和手机上传目标，不做多账号/权限。

## 页面

- 入口：顶栏右侧「设置」（所有页面可达）。
- `/settings`：
  - 修改密码：当前密码、新密码（≥6 位）、确认新密码；成功后提示并跳转 `/login`，所有已登录设备立即失效。
  - 收件箱扫描：默认关闭；开启后可添加/移除观察目录。目录留空时使用软件默认目录（下载/桌面/Documents 下的微信/QQ）。
  - 品牌名称：显示在顶栏、登录页与 PWA 名称；默认 `Deskport`，保存后刷新页面生效。
  - 登录设备：列出 App 用网页密码换到的设备令牌（名称 / 签发时间 / 最后使用 / 最后 IP），可逐个吊销。令牌只在签发时回显一次，列表不含哈希；吊销后该设备需重新登录。
  - 上传目标：内置「手机上传」（= `phoneUploadDir`，只读、不可删）+ 自定义目标增删改。自定义目标可改显示名与目录，`id` 由服务端生成、编辑时保持不变（App 已保存的目标不会失效）；保存即全量替换自定义列表。
  - 多机联邦：本机联邦令牌（留空 = 关闭联邦，别的机器再也进不来）+ 远端列表（id / 名称 / 地址 / 对方联邦令牌 / 接收策略：`terminal` 合并终端会话、`files` 只读浏览成员文件、`filesWrite` 允许写成员文件、`apps` 外接应用白名单）。

## API

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/auth/password` | `{current, new}` | `{ok: true, relogin: true}` |
| GET | `/api/settings/inbox` | — | `{enabled, dirs, defaults, home}` |
| POST | `/api/settings/inbox` | `{enabled, dirs[]}` | `{ok: true, enabled, dirs}` |
| GET | `/api/settings/brand` | — | `{name}` |
| POST | `/api/settings/brand` | `{name}` | `{ok: true, name}` |
| GET | `/api/settings/federation` | — | `{token, remotes[], machines[], fileMachines[], discoveredApps[]}` |
| POST | `/api/settings/federation/token` | `{token}` | `{ok, token}`；空串即关闭联邦 |
| POST | `/api/settings/federation/remotes` | `{remotes: [{id, name, base, token?, terminal?, files?, filesWrite?, apps?}]}` | `{ok, remotes[], machines[]}` |
| GET | `/api/settings/devices` | — | `{devices: [{id, name, scopes, createdAt, lastUsedAt, lastIp}]}` |
| POST | `/api/settings/devices/revoke` | `{id}` | `{ok: true, devices[]}`；不存在 404 |
| GET | `/api/settings/uploadTargets` | — | `{targets: [{id, name, path}], builtinId: "phone"}` |
| POST | `/api/settings/uploadTargets` | `{targets: [{id?, name, path}]}` | `{ok: true, targets[], builtinId}` |

错误：密码接口 `400` 太短/过长、`403` 当前密码错误、`401` 未登录；收件箱接口目录不存在返回 `400`，最多 20 个目录。

联邦设置的校验：令牌需 8-64 位字母/数字/`-`/`_`；远端最多 20 台；`id` 需 1-20 位小写字母/数字/`-`/`_` 且不重复；名称 1-24 字符；`base` 须以 `http://` 或 `https://` 开头；`token` ≤128 字符；`terminal`/`files`/`filesWrite` 须布尔；`apps` ≤20 个非空短字符串（`["*"]` 表示全收）。

上传目标的校验（`modules/settings.py`）：`targets` 非数组 400；自定义超过 19 个（内置 1 + 自定义 19 = 上限 20）400；`id` 非 `^[a-z0-9_-]{1,20}$` 或重复 400；名称非 1-24 字符、含 `< > " '` 或换行、重名 400；目录须为存在的绝对路径且 ≤500 字符 400。载荷里 `id=phone` 的条目被忽略（内置目标由服务端提供）。这些接口只认网页会话，设备令牌/智能体令牌不可用。

## 实现

- 密码存放在目标机 `config.json` 的 `password` 字段；`config.setPassword()` 原子写回。
- 收件箱开关为 `inboxEnabled`（缺省 `false`），目录为 `inboxDirs`；`config.setInboxSettings()` 原子写回并失效配置缓存。
- 品牌名为 `brandName`（缺省 `Deskport`）；`config.setBrandName()` 原子写回，页面渲染时注入 `<meta name="wp-brand">`，manifest 动态生成。
- 登录设备在 `modules/device_auth.py`：令牌只存 SHA-256，落在 `%APPDATA%\<stateDir>\devices.json`（**不进** `config.json`），mtime 缓存热读 + 原子写；与 `agentTokens` 同走 `agentFromRequest` 一个鉴权入口，设备令牌默认只有 `upload` scope。同设备名重登 = 换新令牌、旧令牌即刻失效；设备上限 20 台。`lastUsedAt`/`lastIp` 同秒节流回写，不逐次写盘。签发、自吊销、管理面吊销都落审计（`device.login` / `device.logout` / `device.revoke`）。
- 上传目标：内置 `phone` 读 `phoneUploadDir`（缺省 `<fallbackDir>\手机上传`，并自动并入 `inboxDirs`）；自定义存 `config.json` 的 `uploadTargets[]`，`config.getUploadTargets()` 合并后返回（非法条目静默跳过）。`path` 只留服务端——下发 App 的 `GET /api/agent/upload/targets` 与 `device/login` 响应只给 `id` + `name`，路径不出网。
- 多机联邦：`config.getFederationToken()` / `setFederationToken()`、`config.getRemotes()` / `setRemotes()`；`remotes[].token` 必须等于远端 `config.json` 的 `federationToken`。联邦请求带 `X-Deskport-Token`，`authGuard` 第一道就放行（不依赖 cookie，因此没有 CSRF 面）。`terminal` 缺省 `true`、`files` 缺省 `false`（浏览成员文件比接终端更敏感）、`filesWrite` 缺省 `false`（写成员文件再敏感一层；`files=true` 仍只读）、`apps` 缺省 `[]`（`["*"]` 全收）。`GET` 响应里的 `machines` / `fileMachines` / `discoveredApps` 是给设置页下拉与白名单提示用的派生视图。成员端的 `federationRoots`（写根白名单，空 = 联邦只读）与 `federationUploadMaxMB`（缺省 2048）直接写在成员机 `config.json`，没有设置页入口；详见 [federation-files.md](federation-files.md)。
- `getInboxDirs()`：开关关闭返回空；开启且显式配置目录时只返回存在的目录；开启且目录为空时调用 `defaultInboxDirs()` 返回软件默认目录。
- Flask `secret_key` 由密码派生；改密时同步更新，旧会话签名立即失效。

## 安全说明

- 单用户，不提供用户名；登录失败有 5 次/60 秒限速（`auth.py`）。
- 设备登录同样限速（`device_auth.py`）：每 IP 60 秒内失败 ≥5 次拒绝到窗口滚出，全局 10 分钟内失败 ≥20 次锁定 10 分钟（锁定期统一 429，落审计 `device.lockout`）；网页登录计数独立，不受设备登录锁定影响。
- 密码只在本机校验，不出网。
- 通过 HTTP 明文传输时密码可被同网段嗅探；如需更强，请在前面加 HTTPS 反向代理。