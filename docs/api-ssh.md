# SSH / SFTP API

网页侧 SSH 子系统：主机与凭据、known_hosts（TOFU）、连接管理、远程文件 CRUD、批量传输与打包、终端 WebSocket。

本文只覆盖浏览器/网页会话使用的接口。**外部智能体不使用这组接口**——它走 `/api/agent/sftp/*`（见 [agent-api.md](agent-api.md)），两者共用同一套 paramiko 连接池但契约不同。

## 认证与错误

- 全部要求网页会话（cookie 登录）；`X-Agent-Token` 对 `/api/ssh/*` 无效。
- 错误一律 JSON `{error}`；成功多返回 `{ok: true}` 或数据对象。
- `SftpAuthError` → **401**（`ssh.py` 里注册的 errorhandler）：主机不存在、密码/密钥认证失败、或该主机需要 keyboard-interactive 认证（SFTP 不支持）。
- 主机密钥变更 → 连接接口 **409** `{error, hostKeyChanged: true}`；WS 场景改为发一帧 `ssh-hostkey-changed` 后断开。
- 认证失败（密码）在 `/api/ssh/connect` 返回 **401** `{error, authRetry: true}`，前端据此提示重输密码。

## 主机与凭据

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/ssh/hosts` | — | `{hosts: [{id, name, host, port, user, authType, group, favorite, notes, createdAt}]}`，收藏优先、其余按名称排序；**不含密码/私钥** |
| POST | `/api/ssh/hosts` | `{id?, name, host, port?, user?, authType?, group?, favorite?, notes?, password?, privateKey?, passphrase?, createdAt?}` | `{ok: true, id}` |
| DELETE | `/api/ssh/hosts/<hostId>` | — | `{ok: true}`（顺带断开并丢弃连接） |

- `name`、`host` 必填，缺任一 → 400「名称与主机地址必填」；`port` 缺省 22，`user` 缺省 `root`，`authType` 缺省 `password`。
- `id` 缺省由服务端生成（`token_hex(8)`）；带 `id` 且已存在即更新，`createdAt` 保留原值。
- **秘密字段只在提供了才覆盖**：编辑时不传 `password`/`privateKey`/`passphrase` 即保留原值。明文存在 `%APPDATA%\<stateDir>\ssh_hosts.json`，与 `config.json` 同级信任（能读该目录的人本来就拥有整机权限）。

## known_hosts（首次信任 TOFU）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/ssh/hostkey/check?hostId=` | — | `{status: "new"\|"known"\|"changed", fingerprint, stored, host, port}` |
| POST | `/api/ssh/hostkey/trust` | `{host, port?, fingerprint}` | `{ok: true}` |

- 指纹格式 `SHA256:<base64 无填充>`，由 paramiko `get_fingerprint()` 再 SHA-256 得到。
- `check` 会真的连一次目标机读主机密钥（失败 502）；`status=changed` 表示与已存指纹不一致，前端必须显式确认后再 `trust` 覆盖。
- 首次连接（`status=new`）时 `_getConnection()` 会**自动信任并落盘**，`check` 端点只是用来让前端先展示确认。

## 连接

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/ssh/connect` | `{hostId}` | `{ok: true}`；401/409/502 见上 |
| POST | `/api/ssh/disconnect` | `{hostId}` | `{ok: true}`（配置了 sshd 时同时通知守护进程结束会话） |
| GET | `/api/ssh/sessions` | — | `{sessions: [{id, kind, label, hostId, attached, cwd, cwdLabel, foreground, foregroundCwdLabel}]}`（`termcore.listSessions`） |
| DELETE | `/api/ssh/sessions/<sessionID>` | — | 守护进程的关闭结果 |

连接按 `hostId` 缓存在进程内（`ConnectionRegistry`），keepalive 30 秒；SFTP 复用同一 transport，所以**先 connect 还是直接调 SFTP 都能工作**（SFTP 内部会按需建立连接）。

`/api/ssh/sessions*` 在配置了 `sshSessiondUrl` 时由 `sessionproxy` 转发给 sshd（见 [sessiond.md](sessiond.md)），否则由 web 进程内 `termcore` 直接提供。会话只由 WS 创建，没有 POST 新建端点。

## 远程文件（SFTP CRUD）

全部 POST JSON，`hostId` 必须存在于主机库。每次请求开一个 SFTP 通道，请求结束即关闭。

| 路径 | 请求 | 响应 |
|---|---|---|
| `/api/ssh/sftp/list` | `{hostId, path?}` | `{currentPath, homePath, parentPath, entries: [{name, path, type: "folder"\|"file", size, mtime, perms}]}`；`path` 缺省 `.`，相对路径按登录目录解析后**统一返回绝对路径**；目录不存在 404，连接错 502 |
| `/api/ssh/sftp/mkdir` | `{hostId, path}` | `{ok: true}`；`mkdir -p` 语义，逐级创建，已存在视为成功 |
| `/api/ssh/sftp/rename` | `{hostId, oldPath, newPath}` | `{ok: true}` |
| `/api/ssh/sftp/delete` | `{hostId, path, isDir}` | `{ok: true}`；目录走 `rmdir`（非空会失败） |
| `/api/ssh/sftp/chmod` | `{hostId, path, mode}` | `{ok: true}`；`mode` 为八进制字符串，如 `"644"` |
| `/api/ssh/sftp/read` | `{hostId, path}` | `{ok, content, encoding, lossy, readonly, mtime, path}` |
| `/api/ssh/sftp/write` | `{hostId, path, content, encoding?, baseMtime?}` | `{ok, encoding}` |

`read` / `write` 的编码契约与文件管理页一致（`modules/textio.py` 的 `decodeSmart` / `encodeUsing`，保留 GBK 等原编码；`lossy=true` 表示有不可逆替换）。

- `read` 上限 20MB，超限 **413**「文件超过 20MB，仅可下载」；超过 1MB 时响应带 `readonly: true`，前端不提供保存。
- `write` 带 `baseMtime` 时按 mtime 做乐观锁：远程文件已被改过 → **409**「文件已被修改 (mtime 冲突), 请刷新后重试」。`encoding` 缺省 `utf-8`。

## 传输与打包

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/ssh/sftp/download` | `hostId, path` | 远程文件流式转发给浏览器（`application/octet-stream`，`Content-Disposition` 带 RFC 5987 文件名）；404 时服务端日志会打印父目录内容辅助排查 |
| GET | `/api/ssh/sftp/download-zip` | `hostId, paths=<JSON 数组>, stamp?` | 多选/整夹打包，服务端递归读取写进内存 zip 后整体下发，文件名 `sftp-<stamp>.zip`；压缩总量超 2GB → 413 |
| POST | `/api/ssh/sftp/upload` | multipart：`hostId, remoteDir, file` | 单文件流式写远程（64KB 分块，不落盘中转）；同名直接覆盖 |
| POST | `/api/ssh/sftp/upload-batch` | multipart：`hostId, remoteDir, files[], paths[]` | 多选/整夹上传，`paths` 与 `files` 对齐保留目录结构（相对路径，含 `..` 的条目进 `failed`）；自动建父目录；响应 `{ok, uploaded: [], failed: [{name, error}]}` |
| POST | `/api/ssh/sftp/upload-local` | `{hostId, remoteDir, paths[]}` | **不经浏览器**：把本机路径直传远程。只接受本机磁盘绝对路径（盘符开头），拒绝整个盘符；目录递归上传；同名目标自动加 `(1)/(2)` 不覆盖；响应 `{ok, remoteDir, files, bytes, items[], errors[]}` |
| POST | `/api/ssh/sftp/to-local` | `{hostId, remotePath, localDir?}` | 远程文件存到本机（缺省 `~/Downloads`），`{ok, localPath}` |
| POST | `/api/ssh/sftp/from-local` | `{hostId, localPath, remoteDir}` | 本机文件推到远程，`{ok, remotePath}` |

`upload-local` / `to-local` / `from-local` 是「文件管理页 ↔ SSH 面板」的桥：浏览器不参与字节搬运，适合大文件与本地目录树。

## 终端 WebSocket

`/api/ssh/ws?hostId=<id>&session=<sessionId>&replay=1`

- 帧格式、回放、抢占接管与本地终端完全一致，见 [terminal-protocol.md](terminal-protocol.md)。
- `hostId` 决定连哪台主机；带存活 `session` 时直接附着（不再新建 shell），`replay=1` 要求先回放历史。
- 认证阶段特有帧：服务端发 `{type: "ssh-auth", title, instructions, prompts: [{text, echo}]}`，前端回 `{type: "ssh-auth-answer", answers: [...]}`（keyboard-interactive / 2FA，超时 `AUTH_TIMEOUT`）。密码被拒后自动尝试这条通道，成功后连接进注册表供 SFTP 复用。
- 主机密钥变更时发 `{type: "ssh-hostkey-changed", msg}` 后关闭；其他失败发 `{type: "error", msg}`。

## 实现位置

| 关注点 | 位置 |
|---|---|
| 蓝图与异常类型、超时常量 | `modules/ssh_base.py` |
| 主机仓储、连接注册表、known_hosts、主机/连接/指纹端点 | `modules/ssh_hosts.py` |
| 交互 shell 后端与 `/api/ssh/ws` | `modules/ssh_terminal.py` |
| SFTP 通道与会话、条目转换、`mkdir -p`、重名策略 | `modules/ssh_sftp_base.py` |
| 远程 CRUD 端点 | `modules/ssh_sftp_ops.py` |
| 传输/打包/本机互传端点 | `modules/ssh_sftp_transfer.py` |
| 装配（蓝图 + 可选 sessions/WS 路由） | `modules/ssh.py` |
| 托管模式下代理到 sshd | `modules/sessionproxy.py` |