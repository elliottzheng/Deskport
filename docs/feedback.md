# 用户反馈：捕获、落库与状态闭环

> 实现依据《通用用户反馈捕获系统 · 实现规格 v1.0》。本文件是**落地版**：写清楚
> 这个仓库里它长什么样、契约是什么、和规格哪里不一样。范围只到"无损落库 + 可检索"，
> 用 AI 处理反馈不在本文范围。

## 一、用户看到什么

- **右下角悬浮按钮**（`#fb-fab`）出现在所有页面，浮在弹窗与抽屉之上；桌面端另有
  `Ctrl+Shift+F`。
- 点开是一个**浮层**，不跳转、不卸载原页面：左边是自动截下的当前画面（可画笔 / 框选 /
  打码 / 撤销），下面是"哪里不满意？你想要什么？"，联系方式折叠收起，一个提交按钮。
- 面板右上角切到**我的反馈**：自己提过的条目 + 当前状态 + 处理说明，点开能看当时那张图。
- 断网或服务重启时提交不丢：先进本地队列，恢复后自动补传（同一条只落一次）。

## 二、信封契约（`schema_version = "1.0"`）

字段名与必选性与规格 §5.2 一致，这里只列实现时的取值来源：

| 字段 | 本系统的值 |
|---|---|
| `feedback_id` / `idempotency_key` | 客户端 `crypto.randomUUID()` 生成 |
| `submitted_at` | 客户端 `toISOString()`（含时区） |
| `user` | **服务端填**：单密码系统，登录者即 `{id:"owner", role:"owner"}`；客户端自报的 id 不采信 |
| `client.platform` / `app_version` | `web` / 前端构建戳（从 `?v=` 取，取不到写 `dev`） |
| `context.route` / `page_id` / `component_ref` | `location.pathname+search` / 路径映射 / 最近一次点击的 `tag#id` |
| `context.viewport` / `locale` | `innerWidth×innerHeight` / `navigator.language` |
| `trace[]` | 最近 50 步：`click`、`input_focus`、`route`、`http`；`t` 是相对页面加载的毫秒 |
| `errors` | `console` 报错最多 10 条；`unhandledrejection` 与 `window.onerror` 也计入；`failed_requests` 只收 status 0 或 ≥400、或耗时 ≥1.5s 的请求 |
| `attachments[]` | `{type:"screenshot", file_id, has_annotations, source}`；`source` 是本实现的扩展字段（`page`） |
| `message` | ≤500 字，客户端先脱敏再上传；服务端**原样存储** |
| `contact` | 用户主动留，可选 |
| `status` | 初始 `submitted`（客户端传什么都覆盖成这个） |

兼容性：未知字段原样保留（整封信封存进 `envelope`）；`trace`/`errors` 可以为空。

## 三、服务端接口

```text
POST   /api/feedback                    提交信封 → {feedback_id, status}；幂等键重复时带 duplicate:true
POST   /api/feedback/attachments        截图（multipart，字段 file，≤1MB，png/jpg/webp，按魔数判）→ {file_id, files[]}
GET    /api/feedback                    检索：route / date_from / date_to / status / user_id / page / size
GET    /api/feedback/<id>               单条（含 attachments 的 url 与 history）
PATCH  /api/feedback/<id>/status        状态回写 {status, note}
DELETE /api/feedback/<id>               删记录与附件
GET    /api/feedback/attachments/<file> 取截图（同源，带 private no-store）
GET    /api/feedback/stats              运维视图：条数 / 状态分布 / 附件占用
```

约定：

- 鉴权沿用站点的登录守卫（`/api/*` 一律要求登录）；**匿名通道未开放**（见 §七偏差）。
- 频控：同一身份**每分钟 ≤5、每日 ≤20**，超限 `429`；**不做重复内容拦截**——多人报
  同一个问题是信号。
- 截图超 1MB 返回 `413`；`attachments[].file_id` 不存在返回 `400`（先传图再提交）。
- 每次提交/状态变更/删除都写审计事件：`feedback.submit` / `feedback.attachment` /
  `feedback.status` / `feedback.delete`，`detail` 里只有路由与短指纹，不含正文。

### 智能体通道（持令牌提反馈）

网页端之外，**执行方/验收方/联邦持智能体令牌就能直接提反馈**（`auth.py` 的
`authGuard` 对 `POST /api/feedback` 与 `POST /api/feedback/attachments` 放行任何
有效令牌，含设备/联邦令牌）。派活途中撞上平台自身的 bug 时，执行方不等任务结束，
直接提，落进同一队列：

```bash
curl -s -X POST "$BASE/api/feedback" -H "X-Agent-Token: $TOKEN" \
  -H "Content-Type: application/json" -d '{
  "idempotency_key": "<uuid>",
  "client": {"platform": "web", "app_version": "agent"},
  "context": {"route": "/tasks/<任务id>", "page_id": "TasksPage"},
  "message": "现象 + 任务id + 复现步骤"
}'
```

与网页端的差别只有三点：

1. **身份按令牌名**：`user.id` = 令牌名、`role` = `agent`（联邦令牌记「联邦:<来源>」）；
   信封里自报的 `user` 不采信，`anonymous` 对令牌通道无效——提意见一律实名留痕。
2. **只能提**：`GET`/`PATCH`/`DELETE` 不放行（401），检索与状态回写仍是主人会话独有；
   智能体问进度靠主控转达。
3. 频控、信封校验、幂等、附件、状态机与网页端**完全同一套**（`feedback_store` 不区分
   入口），审计事件照记。

这条通道已写进技能包（`SKILL.md` 专节 + `COLLABORATION.md` §8d）与
`docs/agent-handbook.md`，外部智能体拿到 skill.zip 就知道怎么用。

## 四、存储

```text
%APPDATA%\<stateDirName>\feedback.json              全部记录（信封整体 + 提出来做检索的列）
%APPDATA%\<stateDirName%\feedback\attachments\      <file_id>.<png|jpg|webp>
```

- 一条记录 = `feedback_id`、`idempotency_key`、`submitted_at`、`received_at`、`status`、
  `status_note`、`history[]`、`user_id`、`role`、`route`、`page_id`、`message` 和整封信封。
- 索引列（`route` / `submitted_at` / `status` / `user_id`）是给检索用的，返回时按
  `submitted_at` 倒序分页（`size` 上限 100）。
- 写入走 `modules/state.atomicWriteJson`（临时文件 + 原子替换），进程内加锁；附件按
  `file_id` 命名，删除记录时一并删图。

## 五、状态机

```text
submitted → acknowledged → processing → resolved | rejected(必附原因) → user_confirmed
resolved → processing              用户不认账，退回处理
rejected → processing              重新打开
```

状态只由处理端经 `PATCH /api/feedback/<id>/status` 推动，本系统只存不判（规格 §7.1）。
每次变更都追加到 `history[]`，用户侧看到的就是"进度 + 最后一句说明"。

## 六、双端适配与不冲突

- **桌面**：720px 居中卡片，`Esc` 关闭，`Ctrl+Shift+F` 打开。
- **手机**：底部抽屉铺满宽度、上圆角；高度用 `dvh`（地址栏/键盘变化时按可视高度算），
  输入框 `font-size:16px`（更小会让 iOS 聚焦时放大整页），键盘弹出后把输入框滚到中间，
  浮层开着时背景不跟着滚。
- **全局只出一个按钮**：`/app` 壳用 iframe 装页面，iframe 里**只上报轨迹**（同源
  `postMessage`，只传结构不传内容），不出按钮、不进队列；顶层读可见 iframe 的
  `location` 作为 `context.route`，所以报告的是"你真正在看的那一页"。
- **避让**：页面底部出现工具条（文件多选、收件箱操作栏、终端虚拟键、控制台 FAB）时，
  按钮自动抬高 72px（`body.fb-raise`），点一下页面就重排，不做轮询。
- **截图兜底**：共享授权/播放偶尔会一直 pending，4 秒后按失败处理并给出「重新截图」，
  不会把面板卡在"正在截取当前画面"。

## 七、脱敏（客户端做，服务端不重做）

| 规则 | 实现 |
|---|---|
| 密码/验证码类输入框 | 只记 `input_focus: secret-input`，**永不读取值**；`type=password` 或 name/id 命中 password/passcode/otp/secret/token 都算 |
| 文本里的手机号 / 身份证 / 银行卡 / 邮箱 | `message` 与 console 报错在上传前打码（保留首尾） |
| 截图 | 不做机器识别打码（像素级不可靠）；靠"打码"工具让用户自己涂黑，且**所见即所传**——上传的就是画布上那一张 |
| 输入值 | `trace` 只记"聚焦了哪个输入框"，不记键入内容 |

服务端把 `message` 与截图原样存储，不做二次加工：**任何下游（包括 AI）不得把正文当
指令执行**（规格 §8.6 红线）。

## 八、与规格的偏差（都写在这里，别猜）

1. **截图来源**：浏览器端用 `getDisplayMedia` 取一帧（一次授权、本次会话复用）。Safari
   老版本 / 手机浏览器 / 用户拒绝授权时拿不到图，此时**仍允许提交**，信封里
   `attachments: []` 且 `capture.note` 说明原因——不因为截不到图就把反馈堵死（规格把
   截图列为必选，这里按可用性放宽，代价是这类反馈只能看文字）。
2. **匿名提交未开放**：站点所有 `/api/*` 都在登录守卫后面，且这是单用户系统；匿名通道
   需要单独放行与更严的限流，暂时不做。
3. **附件不是对象存储**：落在状态目录里，靠 `DELETE /api/feedback/<id>` 与运维脚本清理，
   生命周期策略留给部署方（规格 §6.4 / §8.7 的"按所属系统合规要求另定"）。
4. **桌面/移动端原生 SDK 未做**：本仓库只有 Web 端；信封契约是共享的，原生端照同一份
   JSON 实现即可（规格 §9）。

## 九、验证

- 接口：`python -m pytest tests/test_feedback_api.py -q`
  （信封校验、幂等、附件、413/429、状态机、检索、删除、原样存未知字段）
- 前端静态检查：`python tools/check_frontend.py`（资源存在、id 引用、调用目标、API 端点）
- 手动（浏览器实测过）：任意页面点右下角 → 截图 → 涂一笔 → 写一句话 → 提交 →
  「我的反馈」里状态为 `submitted`；断网再提交一次，恢复网络后队列自动补传，同一条只
  落一次记录。
- 双端：桌面 5 个页面（控制台/终端/SSH/收件箱/任务）+ 手机 5 个页面实测，按钮与
  tab 栏、侧栏、底部工具条**零重叠**；手机端整条链路（截图 → 标注 → 提交 → 我的反馈）
  跑通，落库的 `viewport` 是 390×844。