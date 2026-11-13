# 设置 — 密码、收件箱、品牌、登录设备、智能体令牌与上传目标

单用户系统，设置页负责修改访问密码、收件箱扫描开关、品牌名称，以及管理登录设备和手机上传目标，不做多账号/权限。

## 页面

- 入口：顶栏右侧「设置」（所有页面可达）。
- `/settings`：
  - 修改密码：当前密码、新密码（≥6 位）、确认新密码；成功后提示并跳转 `/login`，所有已登录设备立即失效。
  - 收件箱扫描：默认关闭；开启后可添加/移除观察目录。目录留空时使用软件默认目录（下载/桌面/Documents 下的微信/QQ）。
  - 品牌名称：显示在顶栏、登录页与 PWA 名称；默认 `Deskport`，保存后刷新页面生效。
  - 登录设备：列出 App 用网页密码换到的设备令牌（名称 / 签发时间 / 最后使用 / 最后 IP），可逐个吊销。令牌只在签发时回显一次，列表不含哈希；吊销后该设备需重新登录。
  - 智能体令牌：签发 / 查看 / 吊销外部智能体用的长效令牌（名称 / 权限档位 / 签发时间）。三档权限：全权（可执行命令、读写文件、建任务，选它时页面有醒目警告）、上传与派活、仅派活。明文只在新建成功时于页面内显示一次（带复制按钮与「关闭后不再显示」提示），服务端只存 SHA-256；吊销即时生效，且不允许删掉最后一条全权令牌。
  - 上传目标：内置「手机上传」（= `phoneUploadDir`，只读、不可删）+ 自定义目标增删改。自定义目标可改显示名与目录，`id` 由服务端生成、编辑时保持不变（App 已保存的目标不会失效）；保存即全量替换自定义列表。
  - 多机联邦：本机联邦令牌（留空 = 关闭联邦，别的机器再也进不来）+ 远端列表（id / 名称 / 地址 / 对方联邦令牌 / 接收策略：`terminal` 合并终端会话、`files` 只读浏览成员文件、`filesWrite` 允许写成员文件、`apps` 外接应用白名单）。每台远端卡片上有一个「只读 / 可写 / 未接文件」标识，`filesWrite` 的勾选框在 `files` 未勾选时禁用。
  - 联邦可写目录：成员端写根白名单（见下节），可增删；列表为空时页面明确写出「带 `X-Deskport-From` 的联邦转发写请求一律 403」，加载中/加载失败时只说「没加载出来」，绝不下"一律拒绝"的安全结论。
  - 给外部智能体：只读卡片，显示 `http://<当前地址>/api/agent/handbook` 与复制按钮。这条 URL 是**公开**的接入手册（无需令牌，令牌另行发放）；`config.handbookEnabled=false` 时该端点连同公开工具箱 `GET /api/agent/tools`（手册「工具箱」一节指向的客户端/驱动下载地址）一起 404，卡片仍显示地址但不保证可用。

## 联邦写（一）：成员端可写目录

本机作为**成员**被别的 Deskport 写文件时，落点必须落在 `config.federationRoots` 的某一项之下。设置页「联邦可写目录」卡片就是这个列表，保存即全量替换、立即生效（无需重启）。

- 每项先经 `modules/federation_write.normalizePath`：展开 `~`、剥 `\\?\` 长路径前缀；本地路径再 `realpath` 解析软链与 `..`，UNC 只做文本归一（不触网，见 `federation_write.py` 模块说明）。再与落盘路径按 `normcase`+`normpath` 比前缀。相对路径、`..`、盘符相对路径（`C:foo`）、设备路径（`\\.\...`）一律 400，错误信息指明是第几项。
- 上限 `MAX_ROOTS = 20`；重复项自动去重（按归一化后的真实路径）。
- **空列表 = 带 `X-Deskport-From` 的联邦转发写一律 403**（不是「不校验」）；本机自己的文件操作、不带该头的请求、只读浏览都不受该列表约束。页面对空列表有专门的橙色提示，避免用户以为配错了。
- 存在性只做一半校验：路径**存在且不是目录** → 400（文件不能当根）；路径不存在仍放行（目录可能还没建、UNC 共享可能临时离线）。UNC 不做存在性探测——离线共享一次 `stat` 也会等网络超时。
- 写入走 `config.setFederationRoots()` → 原子写回 `config.json` 并失效配置缓存，因此 `federation_write.roots()` 下一次调用就拿到新值。

## 联邦写（二）：中枢端的「允许写」

本机作为**中枢**把写请求转发到某台远端时，需要该远端条目的 `remotes[].filesWrite = true`（缺省 `false`）。远端卡片上的「允许写它的文件」勾选框对应此字段：

- `files` 未勾选时写勾选框禁用；保存时若 `files=false`，服务端收到的 `filesWrite` 也强制为 `false`（`files=true` 仍只是只读）。归一发生在两处：`POST /api/settings/federation/remotes` 落盘前，以及 `config.getRemotes()` 读的时候——所以手改 `config.json` 写出 `files=false, filesWrite=true` 也会被读成只读，页面不会出现"勾着但灰掉 + 徽标说未接文件"的矛盾态。
- 卡片右上角的徽标：`未接文件`（`files=false`）/ `只读`（`files=true, filesWrite=false`）/ `可写`（两者都为 `true`）。关掉「允许写」并保存，下一个写请求立刻 403。
- 写是否真正成功还取决于成员端白名单（上一节）：**两边都要配**，缺一边就是只读。

## API

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/auth/password` | `{current, new}` | `{ok: true, relogin: true}` |
| GET | `/api/settings/inbox` | — | `{enabled, dirs, defaults, home}` |
| POST | `/api/settings/inbox` | `{enabled, dirs[]}` | `{ok: true, enabled, dirs}` |
| GET | `/api/settings/brand` | — | `{name}` |
| POST | `/api/settings/brand` | `{name}` | `{ok: true, name}` |
| GET | `/api/settings/federation` | — | `{token, remotes[], machines[], fileMachines[], discoveredApps[], roots[], maxRoots}` |
| POST | `/api/settings/federation/token` | `{token}` | `{ok, token}`；空串即关闭联邦 |
| POST | `/api/settings/federation/remotes` | `{remotes: [{id, name, base, token?, terminal?, files?, filesWrite?, apps?}]}` | `{ok, remotes[], machines[]}` |
| POST | `/api/settings/federation/roots` | `{roots: ["E:\\共享\\给中枢"]}` | `{ok, roots[], maxRoots}`；数组即全量替换，`[]` = 联邦转发写一律 403 |
| GET | `/api/settings/devices` | — | `{devices: [{id, name, scopes, createdAt, lastUsedAt, lastIp}]}` |
| POST | `/api/settings/devices/revoke` | `{id}` | `{ok: true, devices[]}`；不存在 404 |
| GET | `/api/settings/agent-tokens` | — | `{tokens: [{id, name, scopes, createdAt}]}`；绝不含 hash 或明文 |
| POST | `/api/settings/agent-tokens` | `{name, scopes?}` | `{id, token, name, scopes}`；`token` 明文只在这一次响应出现 |
| DELETE | `/api/settings/agent-tokens/<id>` | — | `{ok: true}`；不存在 404 |
| GET | `/api/settings/uploadTargets` | — | `{targets: [{id, name, path}], builtinId: "phone"}` |
| POST | `/api/settings/uploadTargets` | `{targets: [{id?, name, path}]}` | `{ok: true, targets[], builtinId}` |

错误：密码接口 `400` 太短/过长、`403` 当前密码错误、`401` 未登录；收件箱接口目录不存在返回 `400`，最多 20 个目录。

联邦设置的校验：令牌需 8-64 位字母/数字/`-`/`_`；远端最多 20 台；`id` 需 1-20 位小写字母/数字/`-`/`_` 且不重复；名称 1-24 字符；`base` 须以 `http://` 或 `https://` 开头；`token` ≤128 字符；`terminal`/`files`/`filesWrite` 须布尔；`apps` ≤20 个非空短字符串（`["*"]` 表示全收）。

可写目录的校验（`POST /api/settings/federation/roots`）：`roots` 非数组 400；超过 20 项 400（整个请求拒绝，不做部分写入）；每项须为非空字符串且 `normalizePath()` 能归一化出绝对路径，否则 400 并在 `error` 里指出「第 N 项」与原始值；路径存在但不是目录（文件当根）400；路径不存在放行；重复项按归一化结果去重。

上传目标的校验（`modules/settings.py`）：`targets` 非数组 400；自定义超过 19 个（内置 1 + 自定义 19 = 上限 20）400；`id` 非 `^[a-z0-9_-]{1,20}$` 或重复 400；名称非 1-24 字符、含 `< > " '` 或换行、重名 400；目录须为存在的绝对路径且 ≤500 字符 400。载荷里 `id=phone` 的条目被忽略（内置目标由服务端提供）。这些接口只认网页会话，设备令牌/智能体令牌不可用。

智能体令牌的校验（`modules/agent_tokens.py`）：`name` 非 1-24 字符、含 `< > " '` 或换行、与现有令牌重名 → 400；`scopes` 只接受省略（全权）/ `["upload","share"]` / `["share"]` 三档，其它组合 400 并在 `error` 里列出可选；总数上限 10 条，超出 400；吊销不存在的 id 404；**吊销最后一条全权令牌 400**（缺省 `scopes` 或显式含 `full` 即全权）。写操作落审计 `agent.token.created` / `agent.token.revoked`（记令牌名与权限档）；`config.setAgentTokens()` 原子写回并失效缓存，吊销/签发下一个请求即热生效。

## 实现

- 密码存放在目标机 `config.json` 的 `password` 字段；`config.setPassword()` 原子写回。
- 收件箱开关为 `inboxEnabled`（缺省 `false`），目录为 `inboxDirs`；`config.setInboxSettings()` 原子写回并失效配置缓存。
- 品牌名为 `brandName`（缺省 `Deskport`）；`config.setBrandName()` 原子写回，页面渲染时注入 `<meta name="wp-brand">`，manifest 动态生成。
- 公开接入手册：`GET /api/agent/handbook`（`modules/agent_handbook.py`）**不在鉴权守卫内**，正文来自 `docs/agent-handbook.md`，运行时替换 `{{BASE}}`（`baseUrl()`：`scheme://host`，`trustProxyProto` 打开时按受信的 `X-Forwarded-Proto` 修正 scheme，Host 头一律不采信）、`{{VERSION}}`（构建号）与 `{{LAN_LINE}}`（配了 `lanAddress` 才有的「局域网直连」行）；`?format=json` 返回 `{version, base, markdown}`。开关 `handbookEnabled` 缺省 `true`（`config.isHandbookEnabled()`），显式 `false` 时 404。公开正文里不放令牌、指纹字段、主机名或本机路径。
- 公开工具箱：`GET /api/agent/tools`、`GET /api/agent/tools/<name>` 与整包 `GET /api/agent/skill.zip`（`modules/agent_tools.py`）同样**不在鉴权守卫内**，单文件白名单四个文件（`deskport.py` / `driver.py` / `driver.example.json` / `COLLABORATION.md`，源在 `.agents/skills/deskport/`，不复制）；整包六个条目实时组装（四个工具 + 按请求替换占位符的 `SKILL.md` + 生成的 `.deskport.json`，不含令牌），技能名写死 `deskport`。与手册**共用** `handbookEnabled` 开关——工具箱入口写在手册里，独立开关只会造出「手册关了、工具还公开」的半开状态。凭据安全由 `tests/test_agent_tools.py` 与 `tests/test_agent_skill_zip.py` 正则扫描公开内容把关。
- 登录设备在 `modules/device_auth.py`：令牌只存 SHA-256，落在 `%APPDATA%\<stateDir>\devices.json`（**不进** `config.json`），mtime 缓存热读 + 原子写；与 `agentTokens` 同走 `agentFromRequest` 一个鉴权入口，设备令牌默认只有 `upload` scope。同设备名重登 = 换新令牌、旧令牌即刻失效；设备上限 20 台。`lastUsedAt`/`lastIp` 同秒节流回写，不逐次写盘。签发、自吊销、管理面吊销都落审计（`device.login` / `device.logout` / `device.revoke`）。
- 智能体令牌在 `modules/agent_tokens.py`：管理面（GET/POST/DELETE `/api/settings/agent-tokens`）走网页会话，与设置页其他卡片一致；智能体/设备令牌打这些接口一律 401（`authGuard` 只对 `/api/agent/*`、`/api/tasks*`、`/api/worklines` 开放令牌通道）。明文 `secrets.token_urlsafe(32)` 只在 POST 响应出现一次，落盘的只有 `tokenHash`（SHA-256，`agent_api.tokenHash`）；列表视图 `_public()` 只给 `{id, name, scopes, createdAt}`。三档权限与 `agent_api.agentHasScope` 同语义：缺省 `scopes` = 全权（向后兼容手改配置的老令牌），显式只认 `["upload","share"]` / `["share"]`。上限 10 条；吊销保护：`_isFull()` 识别全权令牌，只剩一条时拒绝删。审计 `agent.token.created` / `agent.token.revoked`。
- 上传目标：内置 `phone` 读 `phoneUploadDir`（缺省 `<fallbackDir>\手机上传`，并自动并入 `inboxDirs`）；自定义存 `config.json` 的 `uploadTargets[]`，`config.getUploadTargets()` 合并后返回（非法条目静默跳过）。`path` 只留服务端——下发 App 的 `GET /api/agent/upload/targets` 与 `device/login` 响应只给 `id` + `name`，路径不出网。
- 多机联邦：`config.getFederationToken()` / `setFederationToken()`、`config.getRemotes()` / `setRemotes()`；`remotes[].token` 必须等于远端 `config.json` 的 `federationToken`。联邦请求带 `X-Deskport-Token`，`authGuard` 第一道就放行（不依赖 cookie，因此没有 CSRF 面）；该令牌同时经智能体令牌通道按 `full` 放行（`/api/agent/*`、`/api/tasks*`，2026-11-13 批次 FD）——拿到联邦令牌等于拿到那台机器的完整控制权（含任意命令执行），请只发给自己的 Deskport 实例，泄露即更换（留空 = 关闭联邦）。`terminal` 缺省 `true`、`files` 缺省 `false`（浏览成员文件比接终端更敏感）、`filesWrite` 缺省 `false`（写成员文件再敏感一层；`files=true` 仍只读）、`apps` 缺省 `[]`（`["*"]` 全收）。`GET` 响应里的 `machines` / `fileMachines` / `discoveredApps` 是给设置页下拉与白名单提示用的派生视图。成员端的 `federationRoots`（写根白名单，空 = 带 `X-Deskport-From` 的联邦转发写一律 403）由 `config.load()['federationRoots']` 直读 + `config.setFederationRoots()` 维护，生效值取 `modules/federation_write.roots()`（归一化、去重、截断），设置页入口是 `POST /api/settings/federation/roots`（`GET /api/settings/federation` 的 `roots` 返回同一个生效值）；`federationUploadMaxMB`（缺省 2048）仍直接写在成员机 `config.json`。详见 [federation-files.md](federation-files.md)。
- `getInboxDirs()`：开关关闭返回空；开启且显式配置目录时只返回存在的目录；开启且目录为空时调用 `defaultInboxDirs()` 返回软件默认目录。
- Flask `secret_key` 由密码派生；改密时同步更新，旧会话签名立即失效。

## 安全说明

- 单用户，不提供用户名；登录失败有 5 次/60 秒限速（`auth.py`）。
- 设备登录同样限速（`device_auth.py`）：每 IP 60 秒内失败 ≥5 次拒绝到窗口滚出，全局 10 分钟内失败 ≥20 次锁定 10 分钟（锁定期统一 429，落审计 `device.lockout`）；网页登录计数独立，不受设备登录锁定影响。
- 密码只在本机校验，不出网。
- 通过 HTTP 明文传输时密码可被同网段嗅探；如需更强，请在前面加 HTTPS 反向代理。