# 文件管理 API 参考

文件管理模块 (`modules/files.py`) 的完整 API。所有端点受登录守卫保护（未认证：页面 302，`/api/*` 401）。

路径约定：一律使用目标机本地绝对路径（如 `C:\Users\you\doc.txt`），Windows 分隔符；JSON 中的路径接受 `/` 或 `\`。除注明外请求/响应均为 JSON。

## 浏览

### GET /api/files

| 参数 | 说明 |
| --- | --- |
| `path` | 目录绝对路径；缺省或 `/` 返回磁盘列表 |
| `hidden` | `1` 时返回隐藏/系统文件（前端本地过滤也可） |

响应：`{currentPath, parentPath (null=已到根), entries[], quickAccess{home,desktop,downloads,documents}}`。

`entries[]` 元素：`{name, path, type (drive/folder/file), size, mtime (unix 秒), hidden, system, readonly}`。
权限不足返回 403（附已可达的 parentPath）。

## 下载与流

### GET /api/files/download?path=

单文件附件下载（`as_attachment`）。文件不存在返回 404 JSON。

### POST /api/files/zip

| Body | 说明 |
| --- | --- |
| `paths[]` | 文件/文件夹绝对路径数组 |

响应为 `application/zip` 流（单文件直接入包；文件夹递归归档，arc 相对自身父目录）。
实现为有界队列真流式（256KB × 8 队列深度背压）：内存恒定、不落盘临时文件；客户端断开自动终止压缩线程。

## 写操作

### POST /api/files/rename

`{path, newName}` → `{ok, path}`。非法字符 `\/:*?"<>|` 返回 400；同名返回 409。

### POST /api/files/new-folder

`{path (父目录), name}` → `{ok, path}`。校验同上。

### POST /api/files/recycle-delete

`{paths[]}` → `{ok, count}`。**移入回收站**（`SHFileOperationW` + `FOF_ALLOWUNDO`，可从回收站还原）。
多路径合并为一次 shell 调用；`pFrom` 使用 `create_unicode_buffer` 双 NUL 结尾（`c_wchar_p` 会在首个 `\0` 截断的坑）。
被占用/取消返回 500/400。

### POST /api/files/delete

`{paths[]}` → `{ok, deleted}`。**永久删除**（仅文件；显式 API，UI 默认不暴露）。

### POST /api/files/start

`{path}` → `{ok}`。等价于在目标机上双击（`os.startfile`）。

### POST /api/files/upload

multipart：`file` + `path`（保存目录）+ `execute`/`tempdir`（`true`/`false`）。
`tempdir=true` 时忽略 `path` 存入临时目录。`execute=true` 保存后 `os.startfile` 启动（启动失败返回 200 + `warn` 字段）。

### POST /api/files/remote-download

`{url, path, execute}` → `{ok, filename, path}`。服务端直连下载（不经代理，`verify=False`），失败清理半成品并返回 502。

## 文本预览与保存

### POST /api/files/read

`{path}` → `{content, size, encoding, lossy, truncated}`。

- 上限 20MB（超限 413，仅可下载）；超限判断基于文件大小，`truncated` 恒为 false
- 编码探测顺序：UTF-8 → GBK → Latin-1（Latin-1 无损兜底，任何字节都能显示）
- `encoding` 是探测结果，保存时应原样带回；`lossy=true`（含 NUL，通常是二进制）时前端只读

### POST /api/files/write

`{path, content, encoding}` → `{ok, encoding}`。

- `encoding` 缺省 UTF-8，只接受 `utf-8` / `gbk` / `latin-1`
- 写回沿用读取时的 `encoding`，避免“GBK 读、UTF-8 写”的乱码
- 字符在目标编码中不可表示或编码名非法 → 400

SFTP 的 `/api/ssh/sftp/read`、`/api/ssh/sftp/write` 使用同一套 `encoding` 契约（`modules/textio.py`）。

## 压缩与解压（L2）

### POST /api/files/extract

`{archivePath, destDir?}` → `{ok, dest, count}`。

- 支持 **ZIP** 与 **tar / tar.gz / tar.bz2 / tar.xz**（纯标准库；RAR/7z 明确不支持——无纯 Python 实现/未引入依赖）
- `destDir` 缺省解到与压缩包同名的文件夹（自动追加 `(2)` 避免覆盖）
- 安全：zip-slip 归一化拒绝（`../`/绝对路径/盘符逃逸，命中即 400 并清理半解压产物）；解压总量 4GB 上限 + 目标盘剩余空间守卫
- 兼容：无 UTF-8 标志位的 ZIP 条目按 `cp437→GBK` 回退解码中文文件名
- tar 解压使用 Python 3.12+ 的 `filter='data'` 安全过滤器

### POST /api/files/compress

`{paths[], parent, name}` → `{ok, path}`。在 `parent` 下创建 `name.zip`（自动补扩展名、`(2)` 避重）；
文件夹递归归档。zip-slip 不适用（写入方向），非法名/磁盘错误返回 400/500。

## 搜索（L2）

### GET /api/files/search

| 参数 | 说明 |
| --- | --- |
| `path` | 递归起始目录 |
| `q` | 文件名子串（不区分大小写） |

响应：`{results[{name, path, parent, type, size, mtime, hidden, system, readonly}], truncated}`。
限制：深度 ≤5、结果 ≤2000（`truncated=true` 表示被截断）；不跟随符号链接。

## 前端交互要点（frontend/assets/js/files.js）

- 多选：单击选中、Ctrl 切换、Shift 范围（锚点行）、Ctrl+A 全选；移动端长按 500ms 进入多选 + 底部操作栏
- 双击（桌面）：文件夹进入；**文件=下载**（用户确认的语义）；启动在右键/弹窗中
- 删除：两段确认（第一次点"删除"变"确认删除"）后移入回收站
- 拖拽：文件拖入列表区域即上传到当前目录（L2）
- 快捷键：F2 重命名 / Del 删除 / F5 刷新 / Ctrl+A 全选 / Esc 清除（输入框聚焦时豁免）
