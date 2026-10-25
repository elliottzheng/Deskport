# 设置 — 访问密码与收件箱

单用户系统，设置页负责修改访问密码和收件箱扫描开关，不做多账号/权限。

## 页面

- 入口：顶栏右侧「设置」（所有页面可达）。
- `/settings`：
  - 修改密码：当前密码、新密码（≥6 位）、确认新密码；成功后提示并跳转 `/login`，所有已登录设备立即失效。
  - 收件箱扫描：默认关闭；开启后可添加/移除观察目录。目录留空时使用软件默认目录（下载/桌面/Documents 下的微信/QQ）。
  - 品牌名称：显示在顶栏、登录页与 PWA 名称；默认 `Deskport`，保存后刷新页面生效。

## API

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/auth/password` | `{current, new}` | `{ok: true, relogin: true}` |
| GET | `/api/settings/inbox` | — | `{enabled, dirs, defaults, home}` |
| POST | `/api/settings/inbox` | `{enabled, dirs[]}` | `{ok: true, enabled, dirs}` |
| GET | `/api/settings/brand` | — | `{name}` |
| POST | `/api/settings/brand` | `{name}` | `{ok: true, name}` |

错误：密码接口 `400` 太短/过长、`403` 当前密码错误、`401` 未登录；收件箱接口目录不存在返回 `400`，最多 20 个目录。

## 实现

- 密码存放在目标机 `config.json` 的 `password` 字段；`config.setPassword()` 原子写回。
- 收件箱开关为 `inboxEnabled`（缺省 `false`），目录为 `inboxDirs`；`config.setInboxSettings()` 原子写回并失效配置缓存。
- 品牌名为 `brandName`（缺省 `Deskport`）；`config.setBrandName()` 原子写回，页面渲染时注入 `<meta name="wp-brand">`，manifest 动态生成。
- `getInboxDirs()`：开关关闭返回空；开启且显式配置目录时只返回存在的目录；开启且目录为空时调用 `defaultInboxDirs()` 返回软件默认目录。
- Flask `secret_key` 由密码派生；改密时同步更新，旧会话签名立即失效。

## 安全说明

- 单用户，不提供用户名；登录失败有 5 次/60 秒限速（`auth.py`）。
- 密码只在本机校验，不出网。
- 通过 HTTP 明文传输时密码可被同网段嗅探；如需更强，请在前面加 HTTPS 反向代理。