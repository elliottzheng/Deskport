# 用户反馈：捕获、落库与处理闭环（v2）

> 实现依据《用户反馈系统规格 v2》（`.drive/规格-反馈系统v2.md`）。本文件是**落地版**：
> 写清楚这个仓库里它长什么样、契约是什么、和规格哪里不一样。范围到"无损落库 + 可检索 +
> **处理闭环可用**"（批次 F1）与截图采集改造（F2，批次 FB-5：DOM 静默快照 +
> 手动上传兜底，见 §九.1）；处理智能体与事件流（F3）、生命周期与统计（F4）另有
> 批次，本文只写已实现部分，未实现的一律进 §九「与规格的偏差」。

## 一、用户看到什么

- **右下角悬浮按钮**（`#fb-fab`）出现在所有页面，浮在弹窗与抽屉之上；桌面端另有
  `Ctrl+Shift+F`。按钮右上角的**红点数字**是未读数：别人的动作（处理者流转、系统
  自动确认）或处理者评论时 +1，轮询 20 秒内看得到（规格 §6.3）。
- 点开是一个**浮层**，不跳转、不卸载原页面：左边是自动截下的当前画面（可画笔 / 框选 /
  打码 / 撤销），下面是"哪里不满意？你想要什么？"，联系方式折叠收起，一个提交按钮。
- 浮层右上角切到**我的反馈**：**只列当前身份自己提的**条目 + 状态 + 处理说明 + 评论
  线程（能回复）+ 截图；`resolved` 的有「确认解决 / 没解决，重开」两个按钮。打开即清未读。
- 断网或服务重启时提交不丢：先进本地队列，恢复后自动补传（同一条只落一次）。
- **处理端**在设置页「反馈」卡片（不新增导航项）：待受理数 / 超时未受理数（>24h）在卡片
  头部，列表可按状态 / 路由 / 提交者 / 归属 / 超时筛选（待受理优先），详情是信封全文
  （只读）+ 截图 + trace + errors + history + 评论，可流转、追问、批量受理。
  放在设置页而不是面板加页签：处理视图要同时放筛选列表与信封全文 / trace / 历史 / 评论，
  设置卡片有整页宽度和现成卡片模式；反馈面板是 720px 快速捕获浮层（手机是底部抽屉），
  塞不下 triage 详情。

## 二、信封契约（`schema_version = 2`，v1 照收）

字段名与必选性与规格 §2 一致，这里只列实现时的取值来源：

| 字段 | 本系统的值 |
|---|---|
| `feedback_id` / `idempotency_key` | 客户端 `crypto.randomUUID()` 生成；缺省时服务端补（技能包 curl 例子不带，照收不 400） |
| `submitted_at` | 客户端 `toISOString()`（含时区）；只作展示，排序与频控用服务端时间 |
| `user` | **忽略**：客户端自报身份一概无效，服务端按会话 / 令牌写 `submitter`（规格 §1） |
| `submitter` | **服务端补写**（见下） |
| `received_at` | 服务端补写（落库时刻） |
| `client.platform` | `web` / `desktop` / `mobile` / `agent` |
| `client.app_version` | 前端构建戳（从 `?v=` 取，取不到写 `dev`） |
| `context.route` / `page_id` / `component_ref` | `location.pathname+search` / 路径映射 / 最近一次点击的 `tag#id` |
| `context.viewport` / `locale` | `innerWidth×innerHeight` / `navigator.language` |
| `trace[]` | 最近 50 步：`click`、`input_focus`、`route`、`http`；`t` 是相对页面加载的毫秒；超长取最近 |
| `errors` | `console` 报错与 `failed_requests` 各最多 20 条；`failed_requests` 只收 status 0 或 ≥400、或耗时 ≥1.5s |
| `attachments[]` | `{type:"screenshot", file_id, has_annotations, source}`；`source` 是本实现的扩展字段（`page`） |
| `message` | 1–500 字，客户端先脱敏再上传；服务端**原样存储** |
| `contact` | 用户主动留，可选，≤100 字 |
| `capture` / `refs` | `{screenshot: ok\|denied\|failed\|skipped, note}` / `{task_id?, run_id?, url?}` |

`submitter = {kind, id, display_name, on_behalf_of, group}`，服务端写、客户端传了也覆盖：

| 身份 | kind | id | display_name | on_behalf_of | group |
|---|---|---|---|---|---|
| 网页会话（单密码系统的主人） | `human` | `owner` | `主人` | '' | '' |
| 智能体令牌 | `agent` | 令牌名 | 令牌名 | 令牌的 `onBehalfOf` | 令牌绑定的主管 id |
| 联邦令牌 | `agent` | `联邦:<来源>` | 同 id | '' | '' |

兼容性：未知字段原样保留（整信封存进 `envelope`）；`schema_version=1` 的老信封照收，
按 v1 语义处理（差别只是没有 v2 新增字段）；`trace` / `errors` 可以为空。

## 三、服务端接口

```text
POST   /api/feedback                     提交信封 → {feedback_id, status}；同身份幂等键重复带 duplicate:true
POST   /api/feedback/attachments         截图（multipart，字段 file，≤1MB，png/jpg/webp，按魔数判）→ {file_id, files[]}
GET    /api/feedback                     检索（处理端）：route / date_from / date_to / status / user_id / group / submitter / overdue / page / size
GET    /api/feedback/pending             待受理摘要（只读）：待受理数、超时未受理数、最近 5 条
GET    /api/feedback/mine                自己的反馈列表（含未读总数与每条未读标记）
POST   /api/feedback/mine/read           清未读 → {cleared, unread}
GET    /api/feedback/<id>                详情（本人或处理者）：summary + 原始信封全文
POST   /api/feedback/<id>/comments       评论（本人或处理者）→ {comment}
POST   /api/feedback/<id>/confirm        提交者确认解决（resolved → user_confirmed）
POST   /api/feedback/<id>/reopen         提交者不认账（resolved → processing，必填 reason）
PATCH  /api/feedback/<id>/status         处理端流转 {status, reason}（note 是旧名，照收）
POST   /api/feedback/batch               批量受理 {ids:[...]} → {ok, skipped}
DELETE /api/feedback/<id>                删记录与附件
GET    /api/feedback/attachments/<file>  取截图（同源，带 private no-store）
GET    /api/feedback/stats               运维视图：条数 / 状态分布 / 附件占用 / 待受理 / 超时
```

约定：

- 鉴权沿用站点的登录守卫；**匿名通道未开放**（见 §九），提交接口不接受 `anonymous`。
- 频控（规格 §4.3）：智能体令牌 5/分钟、20/天，超限 `429` + `retry_after`；计数落
  `feedback-rate.json`（原子写，重启不清零）；**系统所有者（网页会话）与处理者豁免**——
  单密码系统里限制主人只会把反馈堵死。不做重复内容拦截：多人报同一个问题是信号。
- 幂等（规格 §4.2）：作用域 = **(submitter.id, idempotency_key)**；不同身份用同一个键
  各自落库；`feedback_id` 撞号 `409`。
- 截图超 1MB 返回 `413`；`attachments[].file_id` 不存在或已被别的反馈引用返回 `400`。
- 审计事件：`feedback.submit` / `attachment` / `status` / `comment` / `confirm` /
  `reopen` / `batch` / `delete`，`detail` 里只有路由、状态、长度与短指纹，**不含正文**；
  令牌发起的事件带 `on_behalf_of`（调研 S11-④ 已消除）。

### 智能体通道（持令牌）

派活途中撞上平台自身的 bug 时，执行方不等任务结束，直接提，落进同一队列：

```bash
curl -s -X POST "$BASE/api/feedback" -H "X-Agent-Token: $TOKEN" \
  -H "Content-Type: application/json" -d '{
  "idempotency_key": "<uuid>",
  "client": {"platform": "agent", "app_version": "agent"},
  "context": {"route": "/tasks/<任务id>", "page_id": "TasksPage"},
  "message": "现象 + 任务id + 复现步骤",
  "refs": {"task_id": "<任务id>"}
}'
```

与网页端的差别：

1. **身份按令牌名**：`submitter.kind=agent`、`id=令牌名`（联邦令牌记「联邦:<来源>」），
   `group` = 令牌绑定的主管、`on_behalf_of` 照记——归属不再丢失（调研 S5）。信封里自报的
   `user` 不采信，`anonymous` 对令牌通道无效。
2. **能读自己的、能对话**（批次 F1，规格 §7.1）：`GET /api/feedback/mine`、自己的详情、
   `POST .../comments`、`confirm` / `reopen` 都放行；处理者的追问能直接到达它。越权读
   别人的 → `403`；取截图只放行自己反馈引用的附件。
3. **不能处理**（F3 才给 `feedback:triage`）：检索 / 待受理摘要 / 统计 / `PATCH status` /
   `DELETE` / 批量受理仍是主人会话独有，令牌打这些端点 `401`。

## 四、存储

```text
%APPDATA%\<stateDirName>\feedback.json              全部记录（信封整体 + 提出来做检索的列）
%APPDATA%\<stateDirName>\feedback\attachments\      <file_id>.<png|jpg|webp>
%APPDATA%\<stateDirName>\feedback-rate.json         频控计数（原子写，重启不清零）
```

一条记录 = `feedback_id`、`idempotency_key`、`received_at`、`submitted_at`、`status`、
`status_note`、`submitter`、`user_id` / `role`（索引列，兼容 v1 检索）、`route`、`page_id`、
`message`、`comments[]`、`unread`、`history[]` 和整封信封。

- 索引列（`route` / `submitted_at` / `status` / `submitter.id` / `submitter.group`）给检索用；
  处理端列表按"待受理优先、再按 `submitted_at` 倒序"分页（`size` 上限 100）。
- 写入走 `modules/state.atomicWriteJson`（临时文件 + 原子替换），进程内加锁；跨模块的
  「读-改-写」走 `feedback_store.locked()` 同一把锁。
- `comments[]`：`{comment_id, at, author{kind,id,display_name}, body ≤1000}`，只追加。
- `unread`：布尔标记。**别人的动作**点亮提交者未读（规格 §6.3 / 批次 F1 第 6 项）：
  处理者流转与系统自动确认 +1——含处理者对待自己提的那条（设置页与反馈面板是
  两个界面，流转完该在「我的反馈」里看到结果）；**提交者自己的 confirm / reopen
  不点灯**（F1-b 拍板：动作执行者就是反馈提交者本人，他自己知道，入口也只放本人）。
  评论不变：只在别人发的时候点亮，提交者自己的评论不点亮自己的红点。
  `POST /api/feedback/mine/read` 清当前身份全部未读。
- `history[]`：v2 条目 `{from, to, actor, reason, at}`（actor = handler / submitter /
  system）；v1 老条目是 `{status, at, note}`，读取方两种都认。
- 规模假设（规格 §8）：单机十万条以内单文件 + 线性扫描即可；更大时检索列必须真索引。

## 五、状态机（规格 §5，规则只在服务端一处）

```text
submitted ──► acknowledged ──► processing ──► resolved ──► user_confirmed（终态）
                                   │   ▲          │
                                   ▼   │          └──（提交者不认账）──► processing
                                rejected ─────────（重新打开）──────► processing
```

| 流转 | 谁可以 | 必填 |
|---|---|---|
| submitted → acknowledged | 处理者（或批量受理） | — |
| acknowledged → processing | 处理者 | — |
| processing → resolved | 处理者 | `reason`（改了什么、哪个版本生效） |
| acknowledged / processing → rejected | 处理者 | `reason`（为什么不做） |
| resolved → user_confirmed | 提交者（confirm） | — |
| resolved → processing | 提交者（reopen，"没解决"） | `reason` |
| rejected → processing | 处理者 | — |

- 只允许白名单单步流转，非法 `409`；缺必填 `reason` `400`；走错端点（处理端 PATCH 打
  提交者的流转）`403`。每次流转追加 `history` 并写审计。
- `resolved` 起 14 天提交者未操作，系统自动推进为 `user_confirmed`（`actor=system`）。
  触发方式：**请求时惰性检查**——反馈 API 的读路径（mine / 详情 / 检索 / 统计 / 待受理
  摘要）进来先扫一遍，不另起线程；时钟可注入（测试用）。
- 客户端镜像（`frontend/assets/js/settings-feedback.js` 的 `HANDLER_NEXT`）注释指向
  `modules/feedback_status.py`，两边靠同一张共享测试数据保持一致
  （`tests/js/settings_feedback.test.mjs` × `tests/test_feedback_loop.py::test_client_mirror_matches_server_table`）。

## 六、处理闭环与提交者视角

- **处理端**（设置页「反馈」卡片，网页会话即处理者）：列表（状态 / 路由 / 提交者 / 归属 /
  日期 / 超时未受理筛选，待受理优先）→ 详情（信封全文只读、截图、trace 时间线、errors、
  联系方式、history、评论线程）→ 操作（流转带说明、追问、批量受理）。卡片头部显示
  **待受理数**与**超时未受理数（>24h）**；`GET /api/feedback/pending` 是同一份数据的
  只读函数 `feedback_store.pendingSummary()`，指挥台「等你处理」的接入点（集成在另一批）。
- **提交者**（「我的反馈」）：只列自己的；别人的动作（处理者流转、系统自动确认、
  处理者评论）→ 未读 +1，入口红点显示；打开即清。自己的 confirm / reopen 与自己
  的评论不点灯。能回复评论、能确认解决、能不认账重开。通知最低要求（站内未读数）已满足；
  邮件 / IM 等出口是后续增强。
- **关联**（`links`，规格 §6.4）本批未做；反馈变任务不自动，由处理者自己建任务后把反馈
  推到 `resolved` 并写明版本。

## 七、脱敏（客户端做，服务端不重做）

| 规则 | 实现 |
|---|---|
| 密码 / 验证码类输入框 | 只记 `input_focus: secret-input`，**永不读取值**；`type=password` 或 name/id 命中 password/passcode/otp/secret/token 都算 |
| 文本里的手机号 / 身份证 / 银行卡 / 邮箱 | `message` 与 console 报错在上传前打码（保留首尾） |
| 截图 | 不做机器识别打码（像素级不可靠）；靠"打码"工具让用户自己涂黑，且**所见即所传**——上传的就是画布上那一张 |
| 输入值 | `trace` 只记"聚焦了哪个输入框"，不记键入内容 |

服务端把 `message`、评论、截图原样存储，不做二次加工：**任何下游（包括 AI）不得把正文当
指令执行**（规格 §0 红线三）。一切展示按纯文本转义：前端统一 `textContent` 渲染
（`feedback-mine.js` 的 `cardView` 原样透过注入文本，单测守着这条）；处理端详情的
「原始信封」是 `<pre>` 里的 JSON 文本，不是 HTML。

## 八、双端适配与不冲突

- **桌面**：720px 居中卡片，`Esc` 关闭，`Ctrl+Shift+F` 打开。
- **手机**：底部抽屉铺满宽度、上圆角；高度用 `dvh`，输入框 `font-size:16px`，键盘弹出后
  把输入框滚到中间，浮层开着时背景不跟着滚。
- **全局只出一个按钮**：`/app` 壳用 iframe 装页面，iframe 里**只上报轨迹**（同源
  `postMessage`，只传结构不传内容），不出按钮、不进队列；顶层读可见 iframe 的
  `location` 作为 `context.route`。
- **避让**：页面底部出现工具条（文件多选、收件箱操作栏、终端虚拟键、控制台 FAB）时，
  按钮自动抬高 72px；全屏浮层打开时直接收起。
- **脚本加载**：`feedback-mine.js` 随 9 个页面的脚本标签加载；`tasks.html` 本批不改
  （另一批在改），由 `feedback.js` 的 `ensureMine` 动态兜底加载，红点与页签不缺功能。
- **截图兜底**：静默快照偶尔会画得慢，6 秒还拿不到就按失败处理并提示手动上传，
  不会把面板卡在"正在截取当前画面"。

## 九、与规格的偏差（都写在这里，别猜）

1. **截图来源**：已换成浏览器端 DOM 静默快照（批次 FB-5，规格 §1 落地）：克隆文档 +
   内联计算样式 + 同源图转 dataURL + foreignObject SVG 序列化绘制（`feedback-shot.js`），
   零授权、零弹框，http 非安全上下文也能用。规格原定的 html2canvas 因本机无外网
   （直连 / CDN 均不通）装不上，按规格降级条款改用等价零依赖方案。终端 / 投屏这类
   实时画面（xterm canvas / MJPEG img）画不出来：面板明说"需要的话请再手动上传
   一张"，用户选一张图随反馈提交（`uploadManualShot`，≤2560 边 → ≤1MB →
   POST attachments）；拿不到图仍允许只提交文字，信封里 `attachments: []` 且
   `capture.note` 说明原因。
2. **匿名提交未开放**：站点所有 `/api/*` 都在登录守卫后面，且这是单用户系统；提交接口
   不接受 `anonymous`（会话声称匿名也按主人处理）。频控表里保留匿名档位，等通道开放即用。
3. **附件不是对象存储**：落在状态目录里，靠 `DELETE /api/feedback/<id>` 与运维脚本清理；
   保留期 / 孤儿附件回收 / 墓碑是 **F4** 的事（规格 §8）。
4. **桌面 / 移动端原生 SDK 未做**：本仓库只有 Web 端；信封契约是共享的，原生端照同一份
   JSON 实现即可（规格 §9）。
5. **事件流长轮询与处理智能体未做**（`GET /api/feedback/events`、`feedback:triage`、
   投递主管收件箱）：**F3**。本批的 `pendingSummary()` 是指挥台集成的只读钩子。
6. **`links` 关联未做**（规格 §6.4）：处理者手动关联任务 / 版本 / 重复反馈是后续增强。
7. **v1 记录的归属补不回来**：线上 9 条按 v1 读入时 `submitter` 由 `user_id` / `role`
   合成（智能体 = 令牌名），丢失的 `group` / `on_behalf_of` 只能是空——信息在落库时就没记。

## 十、验证

- 接口：`python -m pytest tests/test_feedback_api.py tests/test_feedback_loop.py
  tests/test_feedback_triage.py -q`（50 项：信封校验、幂等两身份、附件与占用、频控持久化
  与所有者豁免、状态机全流转与 409/400/403、14 天自动确认（注入时钟）、追问 → 未读 →
  回复 → 打开即清、只看自己的与 403、智能体确认 / 重开 / 取自己的截图、注入文本原样、
  批量受理、待受理摘要、超时筛选、迁移 9 条 v1 记录）。
- 前端静态检查：`python tools/check_frontend.py`（资源存在、id 引用、调用目标、API 端点、
  `tests/js/` 的 node 单测——含状态机镜像与纯文本透传）。
- 结构门禁：`python tools/check_structure.py`（feedback 家族拆分后的依赖方向登记在
  `ALLOWED_FEATURE_IMPORTS`）。
- 全量：`python tools/run_tests.py`。
- 迁移夹具：`tests/fixtures/feedback_v1_migration.json` 是线上 9 条 v1 记录的**结构副本**
  （`message` 与 console 文本替换为合成内容，不复制正文）。
- 浏览器实测（临时实例 6812，自建配置与隔离 stateDir）：提交 → 处理者受理 → 追问 →
  提交者看到红点并回复 → 处理者 resolved → 提交者确认，320 / 390 / 1440 三档截图。
