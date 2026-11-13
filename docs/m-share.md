# 网页分享表单（`/m/share`）

把「分享文件 → 选去处 → 提交」从 Android 原生表单搬进网页（批次 D）。App 只做
两件事：接住系统分享、把文件换成 ticket，然后用全屏窗口打开
`/m/share?ticket=…`。表单、校验、结果都在网页里。

```
App（设备令牌）  ──stage──▶  服务端暂存区 + 内存 ticket
                                   │
手机浏览器（Cookie）──GET /api/share/stage/<ticket>──▶ 文件名/大小/一句话
                   ──POST /api/share/submit──────────▶ share_core 的三个去处
```

「只上传」额外选**机器 + 上传目标**（批次 E）：本机目标来自 `config`，远端
目标由那台机器自己声明（只回 id/name）。页面的「交给 AI」不出现机器下拉——
附件必须落在执行点那台机器上，由执行点决定。

## 端点

| 端点 | 鉴权 | 用途 |
|---|---|---|
| `POST /api/agent/share/stage` | 设备令牌，`scope=share` | 交 0..10 个文件 + 可选 prompt，换 ticket |
| `GET /api/share/stage/<ticket>` | Cookie 会话 | 读这批文件元信息（**不消费**，页面刷新还能读） |
| `GET /api/share/nodes` | Cookie 会话 | 执行点列表（页面「新任务」用） |
| `GET /api/share/tasks` | Cookie 会话 | 最近 20 条非 closed 任务（页面「接到任务」用） |
| `GET /api/share/targets` | Cookie 会话 | 只上传的「机器 + 上传目标」列表（本机 + 远端） |
| `POST /api/share/submit` | Cookie 会话 | 三选一：只上传 / 交给 AI·新任务 / 接到任务 |

### POST /api/agent/share/stage

`multipart/form-data`：

```text
file     0..10 个，可重复；文件名走 staging.safeAttachmentName
prompt   可选，≤4000 字
→ 200 {"ticket": "<32 位十六进制>",
       "files": [{"name": "卷一.pdf", "size": 123}],
       "expiresAt": 1763000000}
```

- 单文件 ≤20MB；超过 413，已落盘的部分全部删掉；
- 暂存位置 `<任务根>/_uploads/stage-<12位>/`，ticket 只记内存；
- 文件名不合法 / 文件数超限 / prompt 与 file 都为空 → 400；
- 缺 `share` scope → 403 `{"need": "share"}`；无令牌 401；
- 审计 `share.stage`（target = ticket 前 8 位）。

### GET /api/share/targets

「只上传」的第二个轴：文件放**哪台机器**的**哪个上传目标**（批次 E）。

```json
{"targets": [
   {"node": "local",  "id": "phone", "name": "手机上传", "status": "online"},
   {"node": "local",  "id": "jyzl",  "name": "教研资料", "status": "online"},
   {"node": "win4710","id": "phone", "name": "手机上传", "status": "offline"}],
 "nodes": [
   {"name": "local",   "label": "本机",     "status": "online"},
   {"name": "win4710", "label": "win4710",  "status": "online"}],
 "default": {"node": "local", "id": "phone"}}
```

- **路径不出网**：响应里没有 `path` 字段，远端返回的目标也只取 `id`/`name`
  （对方即使误回路径也会被丢掉）；
- 本机目标来自 `config.getUploadTargets()`；远端目标由那台机器**自己声明**：
  agent node 走它的 `GET /api/agent/upload/targets`（`X-Agent-Token`），
  联邦成员同样路径但用 `X-Deskport-Token`；
- **离线不隐藏**：拉不到的机器仍在 `nodes[]` 里，`status=offline`；它上次
  成功声明的目标也会列出来并标 `offline`。`nodes[]` 供页面「机器」下拉，
  `targets[]` 供同机的「目标」下拉；
- 目标缓存 20 秒（每台机器独立），避免每次开页面都串行等网络；
- 本机目标的 `status=offline` 表示该目标没有配置目录（如 `phoneUploadDir`
  与 `fallbackDir` 都缺失），不是机器掉线；
- **agent node 的现状**：它是零依赖的会话宿主，没有 Deskport 的
  `uploadTargets` 概念，也没有上传端点。所以它会以 `status=offline` 出现在
  `nodes[]` 里（名字可见、目标为空），而不是消失；端到端的远端上传目前
  面向**联邦成员**（它跑的是完整 Deskport，自己有上传目标）；
- 未登录 401。

### POST /api/share/submit

`application/json`：

```json
{"ticket": "<32 位>", "way": "upload|new|append", "prompt": "…",
 "node": "local", "target": "phone", "taskId": "…", "model": "…",
 "mode": "default|auto", "organize": "none|folder", "folderName": "周测卷",
 "subdir": "周测卷"}
```

- `way=upload` 按 `node` 分两条路：
  - `node=local`（或缺省）→ 搬到本机上传目标（`target`，缺省 `phone`）的
    `subdir` 下，行为与批次 D 逐字一致；目标不存在 → 400 并回
    `targets` 列表（只含 id/name）；
  - `node=<agent node 或联邦成员>` → 交给对方自己的 `POST /api/agent/upload`：
    中枢只发 `target` id + 相对 `subdir` + 文件，**落点由成员端把 target id
    映射成路径**；成员机没有该 id → 400「成员机 X 上没有这个上传目标」。
    响应 `{"ok": true, "uploaded": [{"path": "手机上传/周测卷/卷一.pdf",
    "size": 123}], "node": "win4710", "target": "phone"}`，`path` 仍是
    「目标名/相对路径」，绝对路径不出网；
  - 远端上传要 `upload_remote`：按**签发这张票据的令牌**判断（stage 时
    记在票据里），设备令牌没勾选 / 手工令牌没有该 scope → 403
    `{"need": "upload_remote"}`，Cookie 会话本身绕不过去。本机上传不受影响；
  - 远端失败：机器不存在 → 400；连不上 → 502；成员端写入错误照原样带回，
    但文本里的绝对路径会先抹成 `…`。部分成功与 `/api/agent/upload` 同口径
    （200 + `failed[]`，已成功的文件留在成员机）；
  - `subdir` 复用 `/api/agent/upload` 的 `_safeSubdir`（拒绝绝对路径、盘符、
    `..`、非法字符）；文件名走 `staging.safeAttachmentName`，重名自动 `(2)`；
- `way=new` → 新建任务，返回 `{"taskId", "mode": "created", "paths", "prompt"}`；
  `taskId` 传了也忽略；
- `way=append` → 必须带 `taskId`，追加一条消息（正文 = prompt + 附件路径清单），
  返回 `{"taskId", "mode": "appended", "paths", "prompt"}`；任务不存在或已结束 → 409；
- `organize=folder` 语义逐字继承 `/api/agent/share`：在 `_uploads/<folderName>`
  建文件夹、`paths` 只给文件夹、prompt 末尾补「以上附件在 <folderName> 目录下」、
  重名自动 `(2)`；网页流程先把批次文件复制进命名文件夹再交付，失败只清副本，
  票据还能重试；
- 字段规则（prompt/mode/node/target/cwd/model/organize/folderName）与三个去处全部复用
  `modules/share_core.py`，网页入口不复制第二份；
- 未登录 → 401；`way` 非法 / 字段非法 → 400，票据**不被烧掉**，改正后可以重提；
- 审计 `share.submit`（way）+ `agent.share`（交付结果）。

### ticket 语义

- **内存**：服务端进程内字典，重启即失效（重启后页面会收到 400，重新分享）；
- **TTL 300 秒**：`GET`/`POST` 碰到过期票据 → 400，暂存目录当场删除；
  新 stage 时顺手清理没人再读的过期票据；
- **一次性**：提交成功后票据销号，同一个 ticket 再提交 → 400；
- **只绑定这批文件**：不是凭证。拿到 ticket 还需要 Cookie 会话才能提交，
  泄漏最坏结果是会话主人多建一个任务；
- 提交成功后**不再删文件**：本机新建任务已有工作区副本，但「接到任务」的
  消息正文直接引用暂存路径，删掉会让执行方读不到附件。过期删除与成功后的
  保留是两种情形；
- 提交进行中重复提交 → 409（`busy` 标记），不会产生两个任务。

## 页面 `/m/share`

移动优先（390px 起），只引 `tokens.css` + `app.css` + `m-share.css`，无框架。
未登录访问会被守卫 302 到 `/login?next=/m/share?ticket=…`，登录后回到原链接。

| 元素 id / class | 作用 |
|---|---|
| `#share-expire` | 倒计时 / 已过期 / 已提交 |
| `#share-files`、`.share-file` | 收到的文件名 + 大小 |
| `#share-count`、`#share-no-files` | 文件数 / 只有一句话的空态 |
| `#share-prompt` | 处理要求（stage 带的 prompt 预填） |
| `#way-new`、`#way-append`、`#way-upload`（`.share-way`，选中加 `.is-active`） | 三张去处卡片 |
| `#panel-new`、`#panel-append`、`#panel-upload` | 对应参数面板 |
| `#share-node`、`#share-cwd-field` / `#share-cwd` | 执行点；非 local 时显示远端工作目录（占位符跟着执行点平台变：windows → `C:\work`，linux → `/home/me/project`，未知 → `/path`；执行点报了 `defaultCwd` 时优先用真的） |
| `#share-mode`、`#share-model-field` / `#share-model` | 批准模式、模型（执行点声明了模型才显示） |
| `#share-organize`、`#share-folder-field` / `#share-folder` | 装进新文件夹 |
| `#share-task`、`#share-task-empty` | 接到哪个任务 |
| `#share-upload-node` | 只上传时的「机器」下拉（本机 + 各远端；离线项禁用并标「（离线）」） |
| `#share-upload-target`、`#share-upload-target-empty` | 该机器的「上传目标」下拉；没有可用目标时禁用并提示 |
| `#share-subdir` | 只上传时的文件夹名（相对目标根目录） |
| `#share-submit`、`#share-error`、`#share-fatal` | 提交 / 页内错误 / 致命错误（过期、缺 ticket） |
| `#share-result`、`#share-result-title`、`#share-result-text`、`#share-task-link` | 同页结果 + 「看任务」（`/app?view=tasks`） |

页面行为：

1. 读 `?ticket=`，`GET /api/share/stage/<ticket>` 渲染文件与 prompt；缺 ticket
   或已过期时禁用提交并给出重新分享的提示；
2. 三张卡片切换面板，切到「新任务」拉 `/api/share/nodes`、切到「接到任务」拉
   `/api/share/tasks`、切到「只上传」拉 `/api/share/targets`（各只拉一次）；
3. **只有「只上传」有机器轴**：出「机器 + 目标 + 子目录」三个控件；换机器时
   目标下拉只列那台机器声明的目标。「交给 AI」不出现机器下拉——附件必须落在
   执行点那台机器上，由执行点决定（设计文档 §6 的硬约束）；
4. **远端 cwd 占位符按平台变**（批次 NODE-OS）：`/api/share/nodes` 每项带执行点
   自报的 `os` 与 `defaultCwd`，换执行点时 `#share-cwd` 的 placeholder 跟着切
   （windows → `C:\work`、linux/darwin → `/home/me/project`、未知 → `/path`），
   只改占位符不改提交逻辑——提交后由服务端按平台校验 cwd 语法；
5. 提交成功后按钮收起、同页展示结果；失败把后端 `error` 原样显示，票据保留可重试。

## 手动验收（curl）

```bash
BASE=http://127.0.0.1:6789
COOKIE=/tmp/deskport.cookie

# 1) 登录（Cookie 会话）
curl -s -c "$COOKIE" -o /dev/null -X POST "$BASE/login" \
  --data-urlencode "password=<访问密码>" --data-urlencode "next=/m/share"

# 2) 原生侧交文件换 ticket（设备令牌需含 share；App 就是在做这一步）
TICKET=$(curl -s -H "X-Agent-Token: <设备令牌>" \
  -F "prompt=把这两张卷子合成一份周测卷" \
  -F "file=@卷一.pdf" -F "file=@卷二.pdf" \
  "$BASE/api/agent/share/stage" \
  | python -c "import json,sys; print(json.load(sys.stdin)['ticket'])")

# 3) 网页读这批文件（不消费）；手机/桌面浏览器打开同一链接即进入表单
echo "$BASE/m/share?ticket=$TICKET"
curl -s -b "$COOKIE" "$BASE/api/share/stage/$TICKET"

# 4) 看「只上传」能选哪些机器/目标（本机 + 远端，不含路径）
curl -s -b "$COOKIE" "$BASE/api/share/targets"

# 5) 提交：交给 AI·新任务
curl -s -b "$COOKIE" -H 'Content-Type: application/json' \
  -d "{\"ticket\":\"$TICKET\",\"way\":\"new\",\"mode\":\"auto\",
       \"node\":\"local\",\"prompt\":\"把这两张卷子合成一份周测卷\"}" \
  "$BASE/api/share/submit"

# 6) 看任务（浏览器）
#    $BASE/app?view=tasks
```

其余两种去处只换第 5 步的 body：

```json
{"ticket": "…", "way": "upload", "node": "local", "target": "phone",
 "subdir": "周测卷"}
{"ticket": "…", "way": "upload", "node": "win4710", "target": "phone",
 "subdir": "周测卷"}
{"ticket": "…", "way": "append", "taskId": "<任务号>", "prompt": "补充材料"}
```

远端上传要求 stage 用的令牌带 `upload_remote`（设备登录时勾选
`allowRemoteUpload`）；否则第二条会得到
`403 {"need": "upload_remote"}`。落盘位置由成员机的 `uploadTargets` 决定，
中枢只回相对路径（如 `手机上传/周测卷/卷一.pdf`）；要到成员机上确认：

```powershell
# 在那台成员机上（路径来自它自己的 config.json，不在中枢响应里）
Get-ChildItem "<该目标的 path>\周测卷"
```

## 自动化测试

`tests/test_share_web.py`：

- stage 鉴权（401 / 缺 share 403）、文件数 / 文件名 / prompt 超长 400；
- ticket 读取不消费、一次性复用 400、过期 400 且暂存目录已删、提交进行中 409；
- 三种 way 各一条（upload 落盘 + subdir、new 建任务、append 投消息）；
- `organize=folder`（目录、目录说明、重名 `(2)`、失败可重试）；
- `subdir` 校验（`../`、盘符）、非法 way / mode、未登录 401、closed 任务 409 后可重试；
- 审计 `share.stage` / `share.submit` / `agent.share`。

`tests/test_share_targets.py`（批次 E）：

- `/api/share/targets` 未登录 401、本机 + 远端合并、离线机器保留并标 `offline`、
  缓存目标在掉线时标不可用；
- **路径不出网**：响应 JSON 里没有 `path` 键，也没有临时目录/远端路径；
- 本机上传显式目标、未知目标 400 且票据可重试；
- 远端上传：`upload_remote` 缺失 403（`need=upload_remote`）、正常投递
  （成员收到 node/target/subdir/文件）、非法 node 400、成员缺该 target 400 且
  文案明确、离线机器 502、失败文本里的绝对路径被抹掉；
- 成员端 `/api/agent/upload/targets` 与 `/api/agent/upload` 接受联邦身份
  （`X-Deskport-Token`），仍只回 id/name、仍由本机解析目标路径。

## 与规格的差异

1. 规格只列了三个端点；页面还需要执行点与任务列表，新增
   `GET /api/share/nodes`、`GET /api/share/tasks`（Cookie），内部直接调
   `share_core.nodesPayload()/tasksPayload()`，没有第二份查询逻辑。
2. 「ticket 复用 → 400 且暂存文件已删」只对**过期**实现了删除。成功提交后
   的文件已经是任务输入（append 的消息正文直接引用暂存路径），再删会把任务
   弄坏；复用请求拿到 400，但不删上一批文件。
3. `way=upload` 的「文件夹名」用 `subdir` 表达（与 `/api/agent/upload` 同一条
   校验），`organize/folderName` 在该分支忽略。
4. 远端「只上传」没有走 `task_inputs` 的任务输入目录：那条通道的落点是
   `<cwd>/.deskport-inputs/<taskId>/`，而"只上传"根本没有 taskId，用户要的是
   成员机自己的收件箱。改用成员端现成的 `POST /api/agent/upload`——同样由
   成员机把 `target` id 映射成路径、同样只回相对路径，中枢不需要知道任何
   绝对路径。跨机传输、错误码与审计口径与批次 B 一致。
5. `GET /api/share/targets` 比规格多一个 `nodes[]`：机器可能在离线且从未成功
   声明过目标，只有 `targets[]` 时它会从下拉里消失（用户会问"我那台怎么
   不见了"）。`targets[]` 的形状严格按规格。