# 计划：外部应用反代挂载（首个用例：远窗 anzhuo）

> 状态：待上游提案（`ranuts`... 即 anzhuo 的 `web_prefix` 配置）被接受后启动。
> 本文档记录 Deskport 侧要做的事；给上游的变更提案见
> `E:\事项\远程\.tmp\anzhuo\docs\子路径挂载提案.md`（克隆内 `docs/`）。
> 落盘日期：2026-11-02

## 背景与前提

- [远窗 anzhuo](https://github.com/EricChong02/anzhuo)（MIT）是一个跑在本机
  `127.0.0.1:8765` 的 aiohttp 应用：浏览器上指挥电脑里的 Claude Code / Codex，
  带进度、决策按钮、危险命令审批、手机推送——与 Deskport 互补而非重叠。
- 上游提案（已发给对方）建议新增 `web_prefix` 配置；**若上游接受并合并**，
  远窗以 `web_prefix = "/yuanchuang"` 运行，Deskport 只需做反向代理。
- 上游未接受时的兼容模式：其前端所有引用已收在 `/_x/` 命名空间（已核实 45 处
  引用无一例外，且与本系统零冲突），可退化为「页面 + 命名空间透传」的
  模式 2，不依赖上游。

## 设计

### 配置（config.toml 新增节）

```toml
[external_apps.yuanchuang]
upstream = "http://127.0.0.1:8765"        # anzhuo 本机地址
prefix   = "/yuanchuang"                   # 对外访问路径（唯一要求：不与本系统路由冲突）
nav      = { label = "远窗", icon = "plug" }
```

设计取舍：

- 原生插件（api:1）与 legacy 插件继续走现有加载器；`external_apps` 是第三个
  档位「外部工具」——**零上游改动**即可接入任何自托管 Web 服务；
- 挂载模式两种：
  - `full`（上游已支持 web_prefix 时）：整站前缀代理，`/yuanchuang/*` 剥前缀转发；
  - `namespace`（兼容模式，远窗现状即可用）：页面路径走 prefix，API 走
    `/_x/*` 命名空间透传。依赖 anzhuo 保持 `/_x/` 约定（其前端已全部遵守）；
- 冲突检测：`prefix` 在装配时与 Deskport 已注册路由表比对，重合直接拒绝配置。

### 反代路由（`modules/external_proxy.py`，新增，约 120 行）

通用工厂 `makeProxy(prefix, upstream)`，注册：

- `GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS` 于 `/<prefix>` 与 `/<prefix>/<path:filename>`；
- **请求头**：剥 hop-by-hop（Connection/Keep-Alive/Transfer-Encoding/Upgrade/TE/Trailer）；
  透传 Origin/Cookie/Range/Content-Type；追加
  `X-Forwarded-For: <原值>, <remote_addr>`、`X-Forwarded-Proto`、`X-Forwarded-Host`
  （远窗的登录防暴力只认 XFF 最后一段，必须追加真实 IP）；
- **请求体**：流式转发（大文件上传不落盘、不进内存）；
- **响应**：`requests stream=True` 迭代转发；透传状态码（含 206 Range/401）与
  非 hop-by-hop 响应头（Content-Type/Content-Range/Cache-Control）；
  生成器响应 `is_streamed=True` → `gzipText` 自动跳过，SSE/视频 Range 不被缓冲破坏；
- **会话**：线程内 `requests.Session` 复用连接（上游同一台机器，keep-alive 收益大）；
- **错误透传**：上游 404/401/413 原样转发（含 JSON body），不吞不译。

### 认证与边界

- Deskport 登录守卫天然覆盖 `/yuanchuang/*`（未登录 → 302 登录页 / API 401 JSON），
  远窗自己的登录保留——双层，各归各；
- 暂不做 SSO（两套账号密码合并需要上游改 Cookie 签发，收益低）；
- 插件安全模型同样适用：外部应用 = 可信代码，配置期冲突检测拒绝与
  本系统路由重合的 prefix。

### 导航

- `_makePageHandler` 注入的 `__WP_PLUGIN_NAV__` 追加 external_apps 的 nav 项
  （href = prefix），桌面侧栏 / 移动「更多」自动出现；
- anzhuo 是整页应用 → MVP 先走**新标签页**打开；壳内 iframe 视图作为后续增强
  （其 SSE/通知在 iframe 下需实测）。

## 改动清单

| 文件 | 内容 | 估时 |
|---|---|---|
| `modules/external_proxy.py` | 反代工厂（新增） | 0.5d |
| `config.py` | `getExternalApps()` 解析与冲突校验 | 0.2d |
| `app.py` | 装配 external proxy + 导航注入合并 | 0.3d |
| `tests/test_external_proxy.py` | stdlib 上游桩：透传 / XFF / SSE 直通 / Range / 404 / 大 body 流式 | 0.5d |
| `docs/external-apps.md` | 部署文档（以远窗为示例） | 0.2d |

总计 ≈ 1.7 人日。M1（反代+测试）→ M2（导航注入+配置）→ M3（文档+远窗实测）。

## 验收清单

1. 未登录访问 `/yuanchuang/*` → 302 到 Deskport 登录；API 形态返回 401 JSON；
2. 登录后：远窗登录页渲染、静态资源全 200、SSE 进度实时推进；
3. 远窗登录后：建对话、发消息、文件上传下载、视频拖动播放（Range 206）全通；
4. 重启 Deskport：远窗进程不受影响（独立进程，由其自身方式保活）；
5. 关闭该项配置后导航消失、路由 404；
6. 回归：现有路由与 `/yuanchuang` 无冲突；`/api/*` 等本系统路由行为不变。

## 风险与预案

| 风险 | 预案 |
|---|---|
| SSE 经生成器转发在 werkzeug 下被缓冲 | 实测验证；兜底 `direct_passthrough=True` |
| 大文件上传的 chunked 转发 | aiohttp 接受 chunked；实测大 body 上传 |
| 上游长期不接受 `web_prefix` | 退回 namespace 兼容模式（已验证 `/_x/` 无冲突） |
| 上游未来新增根路径资源 | namespace 模式下会漏转发——切 `full` 模式（届时上游已支持 web_prefix） |

## 不做 / 暂缓

- SSO 合并登录（需上游改 Cookie 签发，收益低）；
- 把远窗的对话数据纳入 Deskport 审计流；
- 多外部应用的管理 UI（配置文件手写即可，单用户场景）。
