# 外部应用反代挂载（external_apps）

Deskport 可以把本机自托管 Web 应用挂到面板子路径下：所有流量经 Deskport
反向代理转发，统一受 Deskport 登录守卫保护，导航自动出现在桌面侧栏与移动
「更多」菜单（新标签页打开）。首个用例：[远窗 anzhuo](https://github.com/EricChong02/anzhuo)。

## 配置

`config.json` 新增 `externalApps` 节（每个条目一个外部应用）：

```json
{
  "externalApps": {
    "yuanchuang": {
      "upstream": "http://127.0.0.1:8765",
      "prefix": "/yuanchuang",
      "mode": "namespace",
      "nav": { "label": "远窗", "icon": "plug" }
    }
  }
}
```

| 字段 | 说明 |
|---|---|
| `upstream` | 上游服务地址（`http://` / `https://`，结尾 `/` 会被去掉） |
| `prefix` | 对外访问路径；**不得与 Deskport 已有路由重合**，重合时启动直接报错拒绝装配 |
| `mode` | `full`（上游已配 `web_prefix`）或 `namespace`（兼容模式，缺省） |
| `nav.label` / `nav.icon` | 导航标签与图标；缺省 label=应用 id、icon=plug |

字段非法的条目被跳过；删除条目或整节后重启，导航消失、路由 404。

## 两种挂载模式

### full（上游支持 web_prefix 时）

上游自己在 `/yuanchuang/...` 下服务全部路由。Deskport 把
`/<prefix>/<path>` **原样透传**（不剥前缀）——浏览器请求什么路径，上游
收到什么路径。

### namespace（兼容模式，上游未实现前缀时）

- 页面路径剥前缀转发：`/<prefix>/<path>` → `/<path>`；
- 同时把 `/_x/<path>` 原样透传（远窗前端所有资源引用都在 `/_x/` 命名
  空间，已核实无冲突）。

## 转发行为

- **请求头**：剥 hop-by-hop（Connection / Keep-Alive / Transfer-Encoding /
  Upgrade / TE / Trailer，以及 Connection 列出的逐跳头）；透传
  Origin / Cookie / Range / Content-Type；追加
  `X-Forwarded-For: <原值>, <remote_addr>`（上游的防暴力只认 XFF 最后
  一段）、`X-Forwarded-Proto`、`X-Forwarded-Host`。
- **请求体**：全流式转发（大文件上传不落盘、不进内存）。
- **响应**：全流式（`iter_content` 生成器，`is_streamed=True`，gzip 层自动
  跳过——SSE 事件流与视频 Range 不被缓冲破坏）；状态码与非 hop-by-hop
  头原样透传（含 206 / 401 / 404 / 413 及其 body，不吞不译）。
- **连接**：线程内复用 `requests.Session`（上游同机，keep-alive 收益大）。
- 上游不可达时返回 `502 {"error": "upstream unreachable"}`。

## 安全边界

- Deskport 登录守卫罩住全部挂载路由：未登录访问页面 → 302 登录页；
  `/<prefix>/api/*`（API 形态）→ `401 {"error": "unauthorized"}`。
- 外部应用 = 可信代码，与原生插件同档；不做 SSO——远窗自己的登录保留，
  双层各归各。
- 上游必须只监听本机（如 `127.0.0.1`），不要直接暴露公网；防暴力依赖
  Deskport 转发追加的 XFF。

## 部署示例（远窗 anzhuo）

1. 远窗以 `web_prefix` 运行时用 `full`：

   ```
   upstream = http://127.0.0.1:8765   mode = full
   ```

2. 远窗现状（无 web_prefix）用兼容模式：

   ```
   upstream = http://127.0.0.1:8765   mode = namespace
   ```

3. 重启 Deskport；导航点「远窗」新标签页打开 `/yuanchuang/`。
