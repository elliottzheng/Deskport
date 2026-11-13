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

## 端点

| 端点 | 鉴权 | 用途 |
|---|---|---|
| `POST /api/agent/share/stage` | 设备令牌，`scope=share` | 交 0..10 个文件 + 可选 prompt，换 ticket |
| `GET /api/share/stage/<ticket>` | Cookie 会话 | 读这批文件元信息（**不消费**，页面刷新还能读） |
| `GET /api/share/nodes` | Cookie 会话 | 执行点列表（页面「新任务」用） |
| `GET /api/share/tasks` | Cookie 会话 | 最近 20 条非 closed 任务（页面「接到任务」用） |
| `POST /api/share/submit` | Cookie 会话 | 三选一：只上传 / 交给 AI·新任务 / 接到任务 |

### POST /api/agent/share/stage

`multipart/form-data`：

```text
file     0..10 个，可重复；文件名走 staging._safeAttachmentName
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

### POST /api/share/submit

`application/json`：

```json
{"ticket": "<32 位>", "way": "upload|new|append", "prompt": "…",
 "node": "local", "taskId": "…", "model": "…", "mode": "default|auto",
 "organize": "none|folder", "folderName": "周测卷", "subdir": "周测卷"}
```

- `way=upload` → 搬到上传目标（缺省内置 `phone`）的 `subdir` 下；
  `subdir` 复用 `/api/agent/upload` 的 `_safeSubdir`（拒绝绝对路径、盘符、
  `..`、非法字符）；返回 `{"ok": true, "uploaded": [{"path": "手机上传/周测卷/卷一.pdf", "size": 123}]}`；
- `way=new` → 新建任务，返回 `{"taskId", "mode": "created", "paths", "prompt"}`；
  `taskId` 传了也忽略；
- `way=append` → 必须带 `taskId`，追加一条消息（正文 = prompt + 附件路径清单），
  返回 `{"taskId", "mode": "appended", "paths", "prompt"}`；任务不存在或已结束 → 409；
- `organize=folder` 语义逐字继承 `/api/agent/share`：在 `_uploads/<folderName>`
  建文件夹、`paths` 只给文件夹、prompt 末尾补「以上附件在 <folderName> 目录下」、
  重名自动 `(2)`；网页流程先把批次文件复制进命名文件夹再交付，失败只清副本，
  票据还能重试；
- 字段规则（prompt/mode/node/cwd/model/organize/folderName）与三个去处全部复用
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
| `#share-node`、`#share-cwd-field` / `#share-cwd` | 执行点；非 local 时显示远端工作目录 |
| `#share-mode`、`#share-model-field` / `#share-model` | 批准模式、模型（执行点声明了模型才显示） |
| `#share-organize`、`#share-folder-field` / `#share-folder` | 装进新文件夹 |
| `#share-task`、`#share-task-empty` | 接到哪个任务 |
| `#share-subdir` | 只上传时的文件夹名 |
| `#share-submit`、`#share-error`、`#share-fatal` | 提交 / 页内错误 / 致命错误（过期、缺 ticket） |
| `#share-result`、`#share-result-title`、`#share-result-text`、`#share-task-link` | 同页结果 + 「看任务」（`/app?view=tasks`） |

页面行为：

1. 读 `?ticket=`，`GET /api/share/stage/<ticket>` 渲染文件与 prompt；缺 ticket
   或已过期时禁用提交并给出重新分享的提示；
2. 三张卡片切换面板，切到「新任务」拉 `/api/share/nodes`、切到「接到任务」拉
   `/api/share/tasks`（各只拉一次）；
3. 提交成功后按钮收起、同页展示结果；失败把后端 `error` 原样显示，票据保留可重试。

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

# 4) 提交：交给 AI·新任务
curl -s -b "$COOKIE" -H 'Content-Type: application/json' \
  -d "{\"ticket\":\"$TICKET\",\"way\":\"new\",\"mode\":\"auto\",
       \"node\":\"local\",\"prompt\":\"把这两张卷子合成一份周测卷\"}" \
  "$BASE/api/share/submit"

# 5) 看任务（浏览器）
#    $BASE/app?view=tasks
```

其余两种去处只换第 4 步的 body：

```json
{"ticket": "…", "way": "upload", "subdir": "周测卷"}
{"ticket": "…", "way": "append", "taskId": "<任务号>", "prompt": "补充材料"}
```

## 自动化测试

`tests/test_share_web.py`：

- stage 鉴权（401 / 缺 share 403）、文件数 / 文件名 / prompt 超长 400；
- ticket 读取不消费、一次性复用 400、过期 400 且暂存目录已删、提交进行中 409；
- 三种 way 各一条（upload 落盘 + subdir、new 建任务、append 投消息）；
- `organize=folder`（目录、目录说明、重名 `(2)`、失败可重试）；
- `subdir` 校验（`../`、盘符）、非法 way / mode、未登录 401、closed 任务 409 后可重试；
- 审计 `share.stage` / `share.submit` / `agent.share`。

## 与规格的差异

1. 规格只列了三个端点；页面还需要执行点与任务列表，新增
   `GET /api/share/nodes`、`GET /api/share/tasks`（Cookie），内部直接调
   `share_core.nodesPayload()/tasksPayload()`，没有第二份查询逻辑。
2. 「ticket 复用 → 400 且暂存文件已删」只对**过期**实现了删除。成功提交后
   的文件已经是任务输入（append 的消息正文直接引用暂存路径），再删会把任务
   弄坏；复用请求拿到 400，但不删上一批文件。
3. `way=upload` 的「文件夹名」用 `subdir` 表达（与 `/api/agent/upload` 同一条
   校验），`organize/folderName` 在该分支忽略。