# 远程访问与内网穿透

Deskport 默认监听 `0.0.0.0:6789`，但家庭/公司网络通常没有公网 IP。  
要在外面访问，需要一条“从浏览器到这台电脑”的通道。

**先看安全边界：Deskport 暴露的是完整的本机控制能力。**
- 不要把 `6789` 直接端口转发到公网
- 不要在 HTTP 明文下输入密码
- 优先使用私有网络（VPN 组网），其次才是带 HTTPS 的公网隧道
- 隧道层再加一层认证（Tailscale ACL / Cloudflare Access / frp token）更稳

## 方案对比

| 方案 | 适合 | 公网 IP | HTTPS | 复杂度 | 备注 |
|---|---|---|---|---|---|
| Tailscale / ZeroTier | 只给自己设备用 | 不需要 | Tailscale Serve / Caddy | 低 | **最推荐**，设备加入同一私有网络即可 |
| Cloudflare Tunnel | 有域名、想少运维 | 不需要 | 自带 | 低 | 可叠加 Cloudflare Access |
| frp + Caddy | 有 VPS、想完全自控 | 需要 VPS | Caddy 终止 TLS | 中 | 你已经在用的方案；注意 token 与 dashboard |
| ngrok | 临时演示 | 不需要 | 自带 | 很低 | 免费版 URL 随机、有限制，不适合长期 |
| 路由器端口转发 + DDNS | 有公网 IP | 需要 | 必须自己加 Caddy/Nginx | 中 | 暴露面最大，不推荐新手 |

## 1. Tailscale / ZeroTier（推荐）

思路：电脑和手机加入同一个私有网络，手机直接访问电脑的私有 IP，不经过公网端口。

```text
手机（Tailscale） ── 私有网络 ── 电脑（Tailscale，127.0.0.1:6789）
```

步骤（以 Tailscale 为例）：

1. 电脑和手机都安装 Tailscale，登录同一账号
2. 在电脑上确认 Tailscale IP（`100.x.x.x`）
3. 手机浏览器访问 `http://100.x.x.x:6789`

想要 PWA / 剪贴板所需的 HTTPS，可以用 Tailscale Serve 或本机 Caddy 提供 TLS：

```bash
# 以当前 Tailscale 版本的 serve 语法为准
tailscale serve --bg 6789
```

ZeroTier 同理：加入同一 network，用分配的私有 IP 访问。

**优点**：没有公网暴露面，配置最少，适合“只给自己用”。  
**注意**：手机和电脑都必须装客户端；这不是“纯浏览器”方案，但它是安全成本最低的方案。

## 2. Cloudflare Tunnel

适合有域名、又不想自己维护 VPS 的情况。Cloudflare 会替你建立出站隧道，不需要开放入站端口。

快速验证：

```bash
cloudflared tunnel --url http://127.0.0.1:6789
```

它会给出一个 `https://*.trycloudflare.com` 临时地址。  
长期使用建议创建 named tunnel，把自定义域名指到 tunnel，并加 Cloudflare Access 策略，只允许自己的邮箱/设备登录。

注意：

- Cloudflare 默认支持 WebSocket，终端/SSH 能正常代理
- 免费计划对长连接和流式响应有超时/限制，MJPEG 屏幕流可能需要调优
- 不要把 Access 关掉后把 URL 公开

## 3. frp + Caddy（自建 VPS）

适合有公网 VPS、希望完全自己掌控链路的情况。

推荐结构：

```text
浏览器 ──HTTPS──> Caddy（VPS:443） ──HTTP──> frps ──> frpc（家里电脑） ──> 127.0.0.1:6789
```

`frpc.toml` 示意（按你的 frp 版本调整字段）：

```toml
serverAddr = "your-vps.example.com"
serverPort = 7000
auth.token = "change-me"

[[proxies]]
name = "deskport"
type = "https"
customDomains = ["desk.example.com"]

[proxies.plugin]
type = "https2http"
localAddr = "127.0.0.1:6789"
crtPath = "/etc/letsencrypt/live/desk.example.com/fullchain.pem"
keyPath = "/etc/letsencrypt/live/desk.example.com/privkey.pem"
hostHeaderRewrite = "127.0.0.1"
```

如果不想在 frp 里管证书，也可以让 frps 只做 HTTP 转发，由 Caddy 终止 TLS：

```caddyfile
desk.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

注意事项：

- `frps` 的 dashboard 不要暴露公网，或至少改端口 + 强密码 + IP 白名单
- `auth.token` 用长随机值
- WebSocket 需要反向代理正确转发 `Upgrade` / `Connection`
- MJPEG 是长响应，代理要关闭响应缓冲并放宽读超时
- 只开放必要的 443/7000，其他端口一律关闭

## 4. ngrok（临时演示）

```bash
ngrok http 6789
```

适合临时给朋友演示。免费版 URL 每次变化、有连接/带宽限制，不建议作为长期入口。

## 5. 路由器端口转发 + DDNS（不推荐）

只有在你有公网 IP、且愿意自己维护 TLS 时才考虑：

```text
公网 443 -> Caddy/Nginx -> 127.0.0.1:6789
```

必须做到：

- 只转发 443，不要转发 6789
- Caddy/Nginx 配置有效证书
- 限制来源 IP（如果可能）
- 关闭路由器上的 UPnP 自动映射
- 定期更新系统和 Deskport

## 通用检查清单

无论用哪种方式，上线前确认：

- [ ] 浏览器地址栏是 `https://`（或 Tailscale 私有网络）
- [ ] Deskport 密码足够强，不是默认值
- [ ] 隧道层有自己的认证/ACL
- [ ] WebSocket 能连通：终端能正常输入输出，刷新后能 replay
- [ ] 屏幕流能出帧（MJPEG 长连接没有被代理缓冲/超时切断）
- [ ] 不在公网暴露 frps dashboard、调试端口、SSH 管理端口
- [ ] 手机安装 PWA 后能正常打开（PWA 需要 HTTPS）

## 方案怎么选

- **只给自己手机/平板用**：Tailscale / ZeroTier
- **有域名、想少运维**：Cloudflare Tunnel + Access
- **有 VPS、想完全自控**：frp + Caddy
- **临时演示**：ngrok
- **有公网 IP 且懂运维**：Caddy/Nginx 反向代理 + 严格防火墙

参考：

- Tailscale Serve: https://tailscale.com/kb/1242/tailscale-serve
- Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
- frp: https://github.com/fatedier/frp
- Caddy: https://caddyserver.com/docs/
- ngrok: https://ngrok.com/docs
