# 设计系统落地

本文覆盖：设计令牌（`frontend/assets/css/tokens.css` 逐变量）、样式分层与组件类清单、布局与断点规范、图标规范。
本文不覆盖：页面 JS 行为（见 [architecture.md](architecture.md) 前端一节）、文件管理页专属样式（该模块重写中，随新设计另行补充）。

设计令牌源自《给ChatGPT的网站平台视觉规范》（灰蓝底 + 白卡片 + 蓝色强调），是 v1"五套设计语言"问题的 P0 修复（路线图见老仓库 `REPORT-frontend-roadmap.md`）。

## 样式分层

```
tokens.css   令牌定义 (:root CSS 变量), 全站唯一样式来源, 任何页面必引
app.css      reset + 基础排版 + 共享组件类 (顶栏/卡片/按钮/表单/弹窗/提示/移动端 tabbar)
──── 以下按页可选 ────
login.css    登录页 (独立于 app.css: 登录页在免认证白名单内, 只引 tokens.css + login.css)
terminal.css 终端工作台 (亮色外壳 + 深色终端区 + 侧边栏 + 控制键)
screen.css   屏幕页 (深色画面容器 + 状态胶囊)
```

规则：页面 CSS 只消费变量与共享组件类，不新增裸色值；唯一例外是终端深色工作区与屏幕画面容器的固定深底（`#0d1117`、`#101418`）及 xterm 主题色（定义在 `term-client.js`，非 CSS）。

## 设计令牌表（tokens.css 逐变量）

### 配色

| 变量 | 值 | 用途 |
|---|---|---|
| `--bg` | `#F4F7FB` | 页面背景 |
| `--bg-2` | `#EDF2F8` | 次级背景（输入框底、悬停态、信息瓦片） |
| `--surface` | `#FFFFFF` | 内容表面（卡片、顶栏、控件底） |
| `--text-1` | `#1D1D1F` | 主要文字 |
| `--text-2` | `#374151` | 次级文字 |
| `--text-3` | `#6B7280` | 辅助文字（标签、占位、元信息） |
| `--brand-deep` | `#003C88` | 品牌深蓝（logo 字、标题强调） |
| `--brand` | `#0050B5` | 交互蓝（主按钮、链接、激活态、进度条） |
| `--brand-hover` | `#00459C` | 交互蓝悬停 |
| `--brand-bg` | `#EFF6FF` | 浅蓝底（激活导航、选中会话、状态胶囊） |
| `--border` | `rgba(15,23,42,.08)` | 分隔线/卡片边框（低对比发丝线） |
| `--border-ctrl` | `#CBD5E1` | 控件边框（输入框、次级按钮） |
| `--ok` | `#16A34A` | 成功状态文字 |
| `--warn` | `#D97706` | 警告（当前未使用，保留） |
| `--err` | `#DC2626` | 错误文字、危险按钮 |
| `--err-bg` | `#FEF2F2` | 错误底（错误提示条、危险悬停） |

### 圆角 / 阴影 / 字体 / 间距 / 布局

| 变量 | 值 | 用途 |
|---|---|---|
| `--radius-s` | `8px` | 小控件（导航项、小按钮、提示条） |
| `--radius-m` | `12px` | 中控件（按钮、输入框、toast） |
| `--radius-l` | `16px` | 卡片、弹窗 |
| `--radius-xl` | `20px` | 登录卡 |
| `--shadow-card` | `0 1px 2px rgba(15,23,42,.02)` | 卡片静置 |
| `--shadow-float` | `0 10px 24px rgba(15,23,42,.05), 0 2px 5px rgba(15,23,42,.025)` | 浮层（弹窗、toast、悬停卡片） |
| `--font` | `"Inter", -apple-system, ..., "PingFang SC", "Microsoft YaHei", "Noto Sans SC", Arial, sans-serif` | 正文栈（无在线字体，离线可用） |
| `--mono` | `ui-monospace, "Cascadia Mono", Consolas, "Courier New", monospace` | 路径、命令、会话名、元数据 |
| `--sp-1` 到 `--sp-10` | `4/8/12/16/20/24/32/40px` | 间距阶（4px 基，无 6/7 号） |
| `--container` | `1200px` | 页面容器最大宽 |
| `--header-h` | `56px` | 顶栏高（终端工作台以此为高度基准） |

基础排版（app.css）：body 15px / 1.6 行高 / `--text-1`；`:focus-visible` 统一 2px 交互蓝描边；`::selection` 浅蓝底。

## 组件类清单（app.css 及页面 CSS）

### 全局框架

| 类 | 说明 |
|---|---|
| `.topbar` | 吸顶顶栏，高 `--header-h`，白底下边框 |
| `.brand` | 左侧站名（品牌深蓝加粗） |
| `.topnav` / `.nav-link` | 桌面导航；`.is-active` 浅蓝底 + 交互蓝文字；图标 18px |
| `.logout` | 右侧退出链接，悬停转危险色 |
| `.container` | 内容容器，`max-width: var(--container)` 居中 |
| `.page-title` / `.page-sub` | 页头标题 24px / 副标题 14px |
| `.tabbar` / `.tab` | 移动端底部导航（详见"布局与断点"） |

### 内容组件

| 类 | 说明 |
|---|---|
| `.card` / `.card-title` | 白卡片（radius-l + shadow-card）；标题行两端对齐，右侧可放 `.title-side` 元信息 |
| `.btn` + `.btn-primary` / `.btn-secondary` / `.btn-danger` / `.btn-ghost` | 按钮 40px 高、radius-m；`:disabled` 半透明禁点；内联 SVG 图标 16px |
| `.input` / `.select` | 40px 高控件，焦点边框转交互蓝 |
| `.field` / `.field-label` | 表单字段块 + 13px 标签 |
| `.toast` | 底部居中深底白字提示，`.show` 淡入，z-index 200 |
| `.empty-tip` / `.load-error` | 空态提示 / 错误提示条（err 底 + err 字） |
| `.modal-mask` / `.modal` / `.modal-title` / `.modal-meta` / `.modal-actions` / `.modal-close` | 弹窗：全屏半透明遮罩 + 居中 420px 白卡，右上角关闭钮 |

### 概览页

| 类 | 说明 |
|---|---|
| `.info-grid` / `.info-item` / `.info-label` / `.info-value` | 信息瓦片（bg-2 底），数值用 `tabular-nums` |
| `.meter` | 6px 进度条（内存占比） |
| `.entry-grid` / `.entry-btn` / `.entry-name` / `.entry-desc` | 功能入口卡片网格 |

### 命令执行页

| 类 | 说明 |
|---|---|
| `.cmd-row` / `.cmd-input` | 命令输入行（等宽字体） |
| `.output-block` / `.output-meta` / `.output-pre` / `.output-err` | 输出块：元信息条 + 滚动 pre（50vh 上限）；stderr 红字 |

### 终端页（terminal.css）

| 类 | 说明 |
|---|---|
| `.term-shell` | 顶栏下满屏工作台（`calc(100vh - var(--header-h))`） |
| `#sidebar` / `.session-item` / `.session-name` / `.session-state` | 会话侧边栏 220px；当前项浅蓝底 + 左侧蓝条；窄屏转固定定位覆盖层 + `#sidebar-backdrop` 遮罩 |
| `#bar` / `.preset-command` / `.preset-tag` / `#cmd` / `#connect` / `#status` | 工具条：预设终端按钮（pwsh7 带"默认"胶囊）、自定义命令输入、新建钮、状态文字（`.ok`/`.err` 着色，点击切换控制键区） |
| `#terminal` / `.session-terminal` | 深色终端区 `#0d1117`（唯一深底区）；多会话 DOM 叠放、`hidden` 切换 |
| `#control-keys` / `.control-key` | 控制键条（ESC/TAB/Ctrl+C/方向/Enter），发送对应 VT 序列；localStorage 记忆显隐 |

### 屏幕页（screen.css）

| 类 | 说明 |
|---|---|
| `.screen-wrap` / `#stream-img` | 深色画面容器 `#101418`；图最高 78vh 防溢出 |
| `.screen-offline` | 断线覆盖层 |
| `.screen-meta` / `.status-pill`（`.status-ok` / `.status-err`） | FPS/分辨率元信息行；状态胶囊（浅蓝/浅红底） |

文件管理页专用的 `pathbar`、`file-*` 系列类暂存于 app.css，随模块重写一并调整，本文不展开。

## 布局与断点

- 容器：`--container` 1200px 上限，内边距桌面 `--sp-6`（24px）、移动端 `--sp-4`。
- 断点：**唯一断点 640px**。`max-width: 640px` 为移动端，`min-width: 641px` 起多列展开（`info-grid` 2→3 列、`entry-grid` 2→4 列）。
- 移动端模式：顶部 `.topnav` 隐藏，改用底部固定 `.tabbar`（高 60px + `env(safe-area-inset-bottom)`），body 预留 64px 底部空间；五个主导航（概览/终端/屏幕/文件/命令）与桌面一一对应。
- 终端工作台不参与 tabbar 布局（`terminal.css` 覆写 `body { padding-bottom: 0 }`、整页 `overflow: hidden`）。

## 图标规范

- 一律**内联 SVG 线性图标**，全站（含登录页）禁止 emoji——这是项目级约定，也是本仓库文档的书写约定。
- 统一参数：`viewBox="0 0 24 24"`、`fill="none"`、`stroke="currentColor"`、`stroke-width="1.7"`，颜色随文字继承，尺寸由 CSS 控制（导航 18px、按钮 16px、tabbar 22px、入口 22px、文件图标 28px）。
- 现有图标集（概览四宫格、终端、显示器、文件夹、播放圆、菜单三横线）直接在各 HTML 中内联复制使用；新图标按同参数绘制，不引入图标库、不使用在线字体图标。
