# 收件箱与「交给 AI」

一条龙：**收件箱选文件 → 填任务名 → 自动判落点 → 建案例目录 → 终端里拉起 AI CLI**。
目标场景：微信/QQ 收到材料，人不在电脑边，也能在手机上把文件和任务交给终端里的 AI。

## 一、页面与流程

入口：**文件页侧栏「收件箱」**（`/files?loc=inbox`，虚拟位置）；旧链接 `/inbox` 302 到新位置。
收件箱行与普通文件使用同一套动作（预览/打开方式/下载/复制/移动/重命名/删除/属性/在终端中打开/问 AI）。

1. **列表**：扫描观察目录，按修改时间倒序。默认观察微信文件目录、下载、桌面、QQ 文件目录。
2. **去重**：先去掉副本后缀（`(1)`、`（2）`、`- 副本`、`副本(3)`）再比文件名 + 大小，
   然后比 SHA-256；一致合并为一条并标「重复 ×N」，展示时优先用序号最少/原名。
   同名但内容不同则各自保留。
3. **多选**：桌面/移动端均为复选框；选中后浮出底部操作条（下载/压缩/问 AI/删除/复制/移动/全选）。
4. **任务面板**：任务名（默认取第一个文件名，截断 40 字）+ 补充要求（可选）。
5. **落点判断**：输入任务名后自动请求 `/api/ai/plan`，展示三种形态：
   - `auto`：高置信，直接显示将新建的目录；
   - `choose`：给 top3 概率卡片点选；
   - `fallback`：落 `fallbackDir`。
   还可手动「就放这一层」或走目录选择器改落点。
6. **建案例**：`新建子目录` → `<落点>\YYYY-MM-DD 任务名\`；`就放这一层` → 直接复制进所选目录。
   文件原样复制（只读属性会被清除），同名同内容跳过，不同则加 `(1)`；写 `任务.txt`。
7. **拉起 AI**：跳到 `/terminal`，`cwd=案例目录`，启动配置的 AI CLI，并在 hello 后自动发送：
   「请先阅读本目录的 任务.txt，然后按里面的说明处理目录中的文件。」

## 二、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/inbox/recent?limit=80` | 扫描并去重；返回 `items[]`、`total`、`dirs[]` |
| GET | `/api/inbox/thumb?path=` | 图片缩略图（限收件箱目录内） |
| GET | `/api/ai/config` | `workDirs`、`fallbackDir`、`aiCommand`、`detectedCommands`、`inboxDirs` |
| POST | `/api/ai/plan` | `{task, files[]}` → `{mode, suggestion, alternatives[], confidence, margin, source}` |
| POST | `/api/ai/case` | `{task, files[], mode, dest, note}` → 建目录+复制+写任务文件，返回 `terminal{cwd,cmd,kick}` |

`items[]` 字段：`name, path, size, mtime, source, ext, kind, thumb, dupes[], id`。
`kind` ∈ image / pdf / doc / sheet / slide / archive / media / other。

## 三、配置（config.json）

| 键 | 缺省 | 说明 |
|---|---|---|
| `inboxEnabled` | `false` | 收件箱扫描开关；关闭时 `inboxDirs` 不生效。可在设置页开启 |
| `inboxDirs` | 软件默认目录 | 观察目录数组；开启且为空 = 用户主目录下的下载、桌面、Documents 微信/QQ 目录 |
| `workDirs` | `[用户主目录, ~\Documents]` | 落点候选来源；只允许落到这些目录之下 |
| `fallbackDir` | `<第一个 workDir>\默认工作空间` | 低置信度兜底 |
| `dirNotes` | `{}` | 目录用途备注，如「备课」「项目 A」 |
| `aiCommand` | 自动 | 终端里启动的命令；空 = 依次探测 `omp` / `kimi`，都没有则 `pwsh.exe`（无则 powershell） |
| `decisionApiKey` | 空 | SiliconFlow Key；空则退回词面匹配（不联网） |

## 四、落点判断

- 模型：`SemIf`，`POST https://api.siliconflow.cn/v1/systemone`；无 Key 时退回本地词面匹配。
- 候选：工作目录下 0–2 层，按任务名做字符二元组粗筛，最多 12 个；**只给完整路径**。
- 提示词固定带「选最具体目录」规则（父/子同现选子目录）。
- 判定门：`conf ≥ 0.6 且 margin ≥ 0.4` → auto；`conf ≥ 0.35` → choose；否则 fallback。
- 模型失败/未配置 Key → 词面匹配，≥3 分才自动，否则 fallback。
- 结果按 `任务+文件名+候选列表` 缓存；模型输出确定，重复调用结果一致。

## 五、安全边界

- 允许复制本机磁盘路径下的文件或**文件夹**（递归）；拒绝 UNC/设备路径，单次总量上限 1GB。
- 从文件页发起时可为任意已存在路径；从收件箱发起时原文件仍只读，副本可写。
- 落点必须是 `workDirs` / `fallbackDir` 之下，目录须已存在（`403` / `400`）。
- 原始文件永不修改；副本清除只读位，保证 AI 可写。
- 目录名过滤 Windows 非法字符，冲突自动 `(2)` 递增。

## 六、已知限制

- 微信发送者识别、聊天记录自动提取要求：未做（消息库加密）。
- 案例没有历史列表页；目录即事实来源。
- 收件箱全量扫描约 1 秒（1200 个文件），暂无增量缓存。
- 新建终端会话时偶发终端能力应答（`ESC[?1;2c`）漏进输入行；前端会过滤固定形态的设备应答。