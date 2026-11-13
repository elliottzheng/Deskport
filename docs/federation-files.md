# 联邦文件读写（服务端）

状态：2026-11-13 实施（`batch/fed-rw`）。本文件是联邦文件读写的行为契约；
手机 App 的「上传目标」下拉仍属于后续批次；`upload_remote` scope 已在 §8
实现（见文末）。

## 1. 三层开关（缺一层就退回只读）

| 层 | 开关 | 缺省 | 作用 |
| --- | --- | --- | --- |
| 成员端 | `config.federationRoots` | `[]` | 联邦写请求的路径必须 realpath 落在这些根下；空数组 = 联邦一律 403 写 |
| 中枢端 | `remotes[].filesWrite` | `false` | 是否把写端点转发给该成员；`files: true` 仍然只是只读 |
| App | 设备令牌 scope `upload_remote` | 未授 | 手机上传到远端需要；设备登录 `allowRemoteUpload: true` 才追加（见 §8） |

`files: true` 控制只读浏览，`filesWrite: true` 控制写；两者都要求 `files: true`
（未开文件浏览的机器连写请求也不会转发）。成员端根白名单只约束写操作，
只读浏览保持现状。

### 1.1 怎么打开（两边都要配）

联邦写**两边都要配**才成立：中枢开「转发写」、成员开「允许落到哪」。只配一边
仍然是只读，缺哪边就在哪边 403。

中枢（A，想写别人）：

1. 设置页 →「多机联邦」→ 远端卡片先勾「允许浏览它的文件」(`files`)，再勾
   「允许写它的文件」(`filesWrite`)，保存；
2. 卡片右上角徽标会显示 `只读` / `可写`（未开 `files` 时是 `未接文件`）；
   取消「允许写」并保存，下一个写请求立刻退回只读。

成员（B，被别人写）：

1. 设置页 →「联邦可写目录」→ 添加目录（如 `E:\共享\给中枢`）→ 保存；
2. 列表非空且落点在列表内 → 写请求放行；**清空列表 → 立刻恢复「联邦写一律
   拒绝」**，页面在该状态下有橙色提示，不是配置出错；
3. 只读浏览不看这个列表（那由中枢的 `files` 控制）。

等价 API（都立即生效，无需重启）：

```bash
# 成员端：设置可写目录（全量替换；[] = 联邦写一律拒绝）
curl -s -b cookie.txt -H 'Content-Type: application/json' \
  -d '{"roots":["E:\\共享\\给中枢"]}' \
  http://B:6789/api/settings/federation/roots

# 成员端：读取当前生效值（归一化后的真实路径）
curl -s -b cookie.txt http://B:6789/api/settings/federation | jq .roots

# 中枢端：给某远端开写（remotes 是全量替换，原有字段要一起带上）
curl -s -b cookie.txt -H 'Content-Type: application/json' \
  -d '{"remotes":[{"id":"b","name":"B 机","base":"http://B:6789","token":"<对方联邦令牌>","files":true,"filesWrite":true}]}' \
  http://A:6789/api/settings/federation/remotes
```

非法根（相对路径 / `..` / 盘符相对 `C:foo` / 设备路径 `\\.\...`）→ 400，响应
`error` 里指出是「第 N 项」，且**原有列表不变**；超过 20 个目录同样 400。

这条根白名单不只管手动写文件：任务输入投递（§9）把附件落到远端
`<cwd>/.deskport-inputs/<taskId>/` 时，最终仍要落在这份列表的根内；中枢转发前
会先用 `GET /api/files/federation-info` 的 `writeRootsConfigured` 预检一次，
所以「成员没配根」在任务侧会表现为明确的 403，而不是传到一半失败。

## 2. 在线编辑的乐观锁（本机与联邦同一套）

`POST /api/files/read` 现在返回：

```json
{"content": "...", "encoding": "utf-8", "lossy": false, "truncated": false,
 "size": 123, "mtime": 1763000000, "sha256": "<64 hex>"}
```

`POST /api/files/write` 必须携带 `baseMtime` 和/或 `baseSha256`：

- 任一版本与当前文件不一致 → **409** `{"error": "...", "currentMtime": <int>}`，
  **文件内容不会被改动**；
- 两个都不带 → 400；只有本机请求可以改用 `"force": true` 强制覆盖；
  联邦请求（带 `X-Deskport-From`）即使带 `force` 也返回 403；
- 写入走同目录临时文件 + `os.replace`，失败不留半成品；成功响应回带新的
  `mtime` / `sha256`，客户端可直接更新基线。

本机网页编辑器已同步：读取时保存 `mtime`/`sha256`，保存时带回；收到 409
时提示重新加载。

## 3. 转发清单（明确的白名单）

转发给成员：

| 端点 | 说明 |
| --- | --- |
| `GET /api/files` | 浏览 |
| `GET /api/files/download` | 下载（流式） |
| `GET /api/files/thumb` | 缩略图 |
| `POST /api/files/read` | 文本读取 |
| `GET /api/files/search` | 搜索 |
| `POST /api/files/rename` | 重命名 |
| `POST /api/files/new-folder` | 新建文件夹 |
| `POST /api/files/recycle-delete` | 移入成员机回收站 |
| `POST /api/files/paste` | 复制/移动 |
| `POST /api/files/upload` | 上传（流式，原子落盘） |
| `POST /api/files/write` | 文本保存（带乐观锁） |

永不转发（中枢 403，成员端白名单也会拒绝）：

- `POST /api/files/delete`（永久删除）——联邦只允许回收站，要真删登到那台机器；
- `extract` / `compress` / `zip` / `remote-download` / `start`（爆炸半径过大）；
- `GET|POST /api/files/pins`（中枢本地 UI 状态）。

成员端收到联邦请求时只放行上表并集；`From` 头优先于 `machine`：
即使同时带了 `machine`，也只在本机处理，**绝不二次转发（单跳）**。

## 4. 成员端根校验

配置：

```json
{
  "federationRoots": ["E:\\共享\\给中枢", "\\\\nas\\team"]
}
```

设置页「联邦可写目录」写的就是这个键（见 §1.1）；直接改 `config.json` 也生效
（mtime 热读，不必重启）。`GET /api/files/federation-info` 会把
`writeRootsConfigured`（本列表是否非空）与 `maxUploadMB` 一起声明给中枢，
用于转发前预检。

校验规则（`modules/federation_write.py`）：

1. 空数组 = 一律 403，不是"不校验"；
2. 先剥 `\\?\` / `\\?\UNC\` 长路径前缀，再 `realpath`；
3. `realpath` 解析软链与 `..`，逃逸出根的路径拒绝；
4. 比较用 `normcase` + `normpath`：Windows 盘符、UNC 共享名大小写不敏感；
5. 拒绝相对路径、盘符相对路径（`C:foo`）、设备路径（`\\.\...`）；
6. 每个写端点按"最终会落盘的路径"校验：`rename` 同时校验旧路径与新名，
   `new-folder` 校验父目录与结果，`paste` 校验全部源与目标，`upload` 在
   入口和最终文件名上各校验一次，`recycle-delete` 校验每个路径。

## 5. 身份与审计

- 中枢转发时设置 `X-Deskport-Actor: <百分号编码的网页请求来源 IP>`；
- 成员端审计 `files.federation`，`target = 来自 <中枢品牌名>`，
  `detail = <操作> <路径> from=<中枢> actor=<谁>`；
- 授权只看 `federationRoots`，`actor` 只用于审计，不引入第二套 ACL。

## 6. 上传：原子落盘、去重、上限

- 成员端先写 `<name>.part-<rand>`，成功后 `os.rename` 到最终名；失败/超时/
  客户端断开都会在 `finally` 删除 `.part`，不留半成品；
- 同名冲突自动 `<name>(2)<ext>`，**绝不覆盖**；撞名重试 20 次后 409；
- 文件名过 `_safeUploadName`：只接受纯文件名、≤120 字符，拒绝 `\ / : * ? " < > |`
  与控制字符；客户端带来的目录部分直接截掉；
- 单文件上限两端取小：成员端 `config.federationUploadMaxMB`（缺省 2048）
  通过 `GET /api/files/federation-info` 声明 `maxUploadMB`，中枢缓存 5 分钟后
  在转发前按 `Content-Length` 先拒（413），不等传完再失败；成员端流式计数
  再复核一次；
- 联邦上传禁止 `tempdir=true`（会写到成员临时目录，不在根白名单内）和
  `execute=true`（成员端不因联邦请求启动文件）；
- 不做断点续传；重试靠 `(2)` 幂等，不产生重复内容覆盖。

## 7. agent_sftp 写操作（跨机投递的内部通道）

新增（与 list/get/put 同一把 `X-Agent-Token`，全部写审计）：

| 端点 | Body | 说明 |
| --- | --- | --- |
| `POST /api/agent/sftp/delete` | `{host, path, recursive?}` | 文件/软链 remove；目录空删，`recursive:true` 整棵删 |
| `POST /api/agent/sftp/mkdir` | `{host, path, parents?}` | 已存在 409；`parents:true` 逐级创建 |
| `POST /api/agent/sftp/rename` | `{host, from, to, overwrite?}` | 目标存在且 `overwrite:false` → 409 |

`host: "local"` 走本机文件系统，其余走 SFTP。审计 action：
`agent.sftp.delete` / `agent.sftp.mkdir` / `agent.sftp.rename`。

## 8. 本批未实施与已知缺口

- **App 的 `upload_remote` scope（2026-11-13 批次 B2 已补）**：`/api/agent/share`
  执行点是远端时要求该 scope，`full` 视为含；设备令牌默认不授，
  `POST /api/agent/device/login` 带 `allowRemoteUpload: true` 才追加，
  响应里的 `scopes` 如实反映。设备令牌仍只能走 `/api/agent/*`——远端投递
  由服务端按该 scope 代投，手机不直接触发联邦写端点。
- 不做联邦 `pins`、远端压缩/解压、断点续传、多跳联邦（A→B→C）。

## 9. 任务输入投递（跨机附件）

任务执行点是联邦成员时，`paths` / `attachments` 经本文件这套写端点投到
`<cwd>/.deskport-inputs/<taskId>/`（实现见 `modules/task_inputs_fed.py`）：

- 中枢没开 `remotes[].filesWrite` → 直接 403，错误信息点名这一层；
- 成员端 `federationRoots` 为空 → 先按 `/api/files/federation-info` 的
  `writeRootsConfigured` 拒绝，错误信息点名这一层；根不覆盖目标目录时
  成员的 403 原文透传；
- 建目录用 `new-folder`，上传用 `upload`（同名自动 `(2)`），失败清理用
  `recycle-delete`（联邦没有永久删除），不碰 `.part` 残留；
- agent node 不走这里：它经执行点配置的 SSH 主机走 SFTP。

测试：`tests/test_task_inputs.py`、`tests/test_task_inputs_api.py`。

## 10. 测试

```powershell
python -m pytest -q tests/test_files_write_lock.py tests/test_federation_roots.py `
  tests/test_files_upload.py tests/test_files_federation.py `
  tests/test_agent_sftp_write.py tests/test_settings_federation.py `
  tests/test_settings_federation_roots.py
```

覆盖：空根 403、根内放行、根外 / `..` / 软链 / UNC / 长路径、版本冲突 409
且内容不变、联邦 `force` 403、永久 delete 永不生效、上传中断无 `.part`、
同名 `(2)` 且原文件不变、单跳不二次转发、上传上限中枢先拒；设置页接口侧另加
「加目录即时生效 / 清空即时拒绝 / 非法项 400 且不写入 / 上限 20」。
