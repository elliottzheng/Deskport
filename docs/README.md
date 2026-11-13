# web-platform 文档索引

本目录是 Deskport（内部包名仍为 `web-platform`）的文档体系，基于当前代码编写；代码变更时请同步更新对应文档。

## 文档清单

### 骨架与接口

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 系统架构：Flask 入口与模块注册、页面路由、认证守卫、静态资源、MJPEG 广播、终端线程模型 |
| [api.md](api.md) | API 参考：auth、system、command、screen、terminal |
| [api-files.md](api-files.md) | 文件管理 API：浏览/下载/上传/重命名/回收站/打包/解压/压缩/搜索 |
| [api-ssh.md](api-ssh.md) | SSH/SFTP API：主机 CRUD、known_hosts TOFU、连接、远程文件 CRUD、批量传输与本机互传 |
| [terminal-protocol.md](terminal-protocol.md) | 终端 WebSocket 帧协议：连接参数、帧类型、回放、抢占接管 |
| [sessiond.md](sessiond.md) | 会话守护进程：终端/SSH 分治、web 代理、启动方式 |

### 智能体面（任务卡 / 节点 / 工作线）

| 文档 | 内容 |
|---|---|
| [agent-api.md](agent-api.md) | 智能体 API 契约：令牌与 scope、设备登录、快命令/SFTP/持久会话、任务 API、工作线、技能分发、审计 |
| [agent-network.md](agent-network.md) | 智能体网络架构：中枢与执行点、通信通道、任务生命周期、工作线、韧性、信任模型、扩展动作、设计原则与成本取舍、多智能体协作模式 |
| [agent-kit.md](agent-kit.md) | 智能体技能包：手册 + 协作原则 + 客户端 + 驱动、自举命令、`driver.json` 边界、接入约定 |
| [audit-agent-network.md](audit-agent-network.md) | 智能体网络质量审计：实测缺陷清单与结论 |
| [lessons-agent-api-feedback.md](lessons-agent-api-feedback.md) | 外部智能体使用反馈沉淀的经验与决策 |

### 功能与设计

| 文档 | 内容 |
|---|---|
| [ai-handoff.md](ai-handoff.md) | 收件箱与「交给 AI」：扫描去重、落点判断、案例目录、配置 |
| [control.md](control.md) | 控制台：电源/进程/窗口/应用/文本/审计 |
| [settings.md](settings.md) | 设置页：访问密码、收件箱扫描、品牌名称、登录设备、上传目标 |
| [feedback.md](feedback.md) | 用户反馈：悬浮入口与浮层、信封契约、提交/检索/状态接口、脱敏规则、存储与偏差 |
| [plugin-api.md](plugin-api.md) | 插件系统：原生清单、RemoteConnecter legacy 兼容、导航生成与认证 |
| [external-apps.md](external-apps.md) | 外部应用反代挂载：prefix、白名单、与联邦成员 `apps` 的关系 |
| [federation-files.md](federation-files.md) | 联邦文件读写：三层开关、写根白名单、乐观锁、上传原子落盘、审计 |
| [principles.md](principles.md) | 产品与设计原则 |
| [ui-rules.md](ui-rules.md) | UI 设计规则 |
| [design-system.md](design-system.md) | 设计令牌、组件类与断点规范 |
| [design-review.md](design-review.md) | 设计评审记录与重构复盘（含门禁失效事故） |
| [code-structure.md](code-structure.md) | 模块分层、依赖方向、文件预算与门禁、例外登记 |

### 运维与过程资料

| 文档 | 内容 |
|---|---|
| [ops.md](ops.md) | 发布与运维：部署脚本、版本/诊断、watchdog、日志、缓存戳 |
| [deployment.md](deployment.md) | 安装与配置：uv、Python 3.13、config.json、端口、开机自启、依赖 |
| [remote-access.md](remote-access.md) | 远程访问与内网穿透：Tailscale/ZeroTier、Cloudflare Tunnel、frp+Caddy、ngrok、端口转发与安全检查 |
| [plan/](plan/) | 各轮设计草稿（未定稿/已落地前的方案） |

## 建议阅读顺序

1. **deployment.md** — 先在本机跑起来。
2. **architecture.md** — 建立整体认识（模块注册表、路由、前端组织）。
3. **api.md** / **api-files.md** / **api-ssh.md** — 调用接口时查阅。
4. **terminal-protocol.md** — 修改终端前后端前必读。
5. **agent-network.md → agent-api.md → agent-kit.md** — 要给别的智能体发令牌、派活、改动任务卡/节点/工作线，或给外部智能体装技能包时读这三篇。
6. **code-structure.md** — 新增模块或拆分文件前读，注意文件预算与例外登记。
7. **design-system.md** / **ui-rules.md** — 新增或修改页面前查阅。