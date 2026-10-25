# web-platform 文档索引

本目录是 Deskport（内部包名仍为 `web-platform`）的文档体系，基于当前代码编写；代码变更时请同步更新对应文档。

## 文档清单

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 系统架构：Flask 入口与模块注册、页面路由、认证守卫、静态资源、MJPEG 广播、终端线程模型 |
| [api.md](api.md) | API 参考：auth、system、command、screen、terminal |
| [api-files.md](api-files.md) | 文件管理 API：浏览/下载/上传/重命名/回收站/打包/解压/压缩/搜索 |
| [terminal-protocol.md](terminal-protocol.md) | 终端 WebSocket 帧协议：连接参数、帧类型、回放、抢占接管 |
| [ai-handoff.md](ai-handoff.md) | 收件箱与「交给 AI」：扫描去重、落点判断、案例目录、配置 |
| [control.md](control.md) | 控制台：电源/进程/窗口/应用/文本/审计 |
| [settings.md](settings.md) | 设置页：修改访问密码与会话失效 |
| [sessiond.md](sessiond.md) | 会话守护进程：终端/SSH 分治、web 代理、启动方式 |
| [ops.md](ops.md) | 发布与运维：部署脚本、版本/诊断、watchdog、日志、缓存戳 |
| [deployment.md](deployment.md) | 安装与配置：uv、Python 3.13、config.json、端口、开机自启、依赖 |
| [principles.md](principles.md) | 产品与设计原则 |
| [ui-rules.md](ui-rules.md) | UI 设计规则 |
| [design-system.md](design-system.md) | 设计令牌、组件类与断点规范 |
| [code-structure.md](code-structure.md) | 模块分层、依赖方向、文件预算与门禁 |

## 建议阅读顺序

1. **deployment.md** — 先在本机跑起来。
2. **architecture.md** — 建立整体认识。
3. **api.md** — 调用接口时查阅。
4. **terminal-protocol.md** — 修改终端前后端前必读。
5. **design-system.md** / **ui-rules.md** — 新增或修改页面前查阅。
