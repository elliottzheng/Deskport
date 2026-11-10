# 经验记录：智能体 API 反馈改进轮（2026-11-10）

来源：外部智能体（Claude）对 agent API 的实测反馈（`Deskport-API-使用反馈-2026-11-10.md`）
的改进实施。本文沉淀这轮踩过的坑与得出的规则，供后续维护者与智能体复用。

对应提交：9216000（exec+sftp）→ 1672686（会话引擎）→ d2f9209（任务契约）→
f649e18（选模型）→ c0647cd（本机全等价）→ 75cae14（文档）。

## 1. 外壳隔离：用户文本绝不进 shell 语法层（本轮最重要的经验）

**现象**：持久会话里一条 `echo 'oops`（未闭合引号）让会话永久卡死。

**弯路**：先用「bash -n 语法预检 + 原文在 `{ }` 里执行」的方案——预检能发现
语法错误，但命令原文仍要被持久 shell **解析第二次**，未闭合引号照样吞掉后续
输入流。预检防止不了「受保护的 shell 自己解析用户文本」。

**规则**：
- 要保护一个常驻 shell，必须让用户文本**根本不进它的语法层**——base64 编码
  后进 `eval "$(printf %s B64 | base64 -d)"`，解析错误在 eval 内部爆掉
  （rc=2 + bash 原始报错），外壳每一行都是服务端固定文本。
- 预检在隔离方案下是冗余的；做两遍解析的方案（预检一遍 + 执行一遍）是错的。
- 附带收益：`</dev/null` 让 `read x`/`cat` 直接见 EOF（不再吞哨兵）；
  `|| rc=$?` 列表按 POSIX 语义抑制 errexit——`set -e; false` 只让本条
  rc=1 而不炸会话，这正是 agent API 想要的 confinement。

## 2. 流式通道的坑（paramiko / 管道双流）

- **stderr 与 stdout 是两条流**：哨兵（stdout）可能比错误报文（stderr）先到。
  切游标前要等一个短暂静默期（实测 60ms 无新数据即可），否则语法错误信息
  会被截丢。
- **`BufferedReader.read(n)` 会等满 n 字节或 EOF**——管道读线程必须用
  `read1(n)`。用 `read(4096)` 的读线程在 bash 少量输出时永远卡住
  （测试桩和产品代码都中过招）。
- **等待循环的每个出口都要设置状态旗标**：老代码只在 `chan.dead()` 时设
  `timedOut`，deadline 到期退出时它仍是 False——「实际超时却报 false」就是这么
  来的。写 wait-loop 先列全所有 break 路径。
- **缓冲区头部截断时，所有游标同步平移**：等哨兵期间防 `tail -f` 撑爆内存要
  截断缓冲，`outStart` / `pendingFrom` 必须一起减，否则下一条命令的输出切片
  全错位。
- **EXIT trap 捕获 shell 退出码**：`trap 'printf "__EXIT_<nonce>=%s\n" "$?"' EXIT`
  nonce 每条命令换新；通道死后从缓冲里找标记拿 rc；**返回前把标记行从输出里
  剥掉**（真机测试抓到过泄漏，单元桩没抓到——两条流时序不同）。
- **正向就绪探针**：开会话后立刻喂一行 `printf __READY_<nonce>`，等到回显才算
  就绪；cd 失败则通道秒关。启动链必须全程 `&&`——`cd x && export ...; bash -s`
  里那个 `;` 会让 bash 在 cd 失败后照样启动。
- **超时杀远端进程**：`setsid -w bash -c '<固定文本含b64>'` + 先打印
  `__PID__$$`，超时后第二条通道 `kill -TERM -- -PID` 再 nohup 延迟 KILL
  （nohup 才能在会话回收后活下来完成补刀）；PID 标记行同样要剥。

## 3. SFTP/文件写入

- **宽松解码 = 静默写空文件**：`base64.b64decode('!!!')` 返回 `b''` 不报错，
  覆盖写直接清空原文件还回 ok:true（本轮 P0 之最）。必须剔除空白后
  `validate=True` 严格解码 + 补齐校验。解码函数放 `fsutil.decodeBase64`
  共享（sftp、本机 sftp、任务附件三处同源）。
- 原子写：临时文件 + `posix_rename`，老服务器不支持时退化为直写并清理；
  覆盖前 stat 保存权限、写后 chmod 恢复（这是当初被夸的行为，别改丢）。
- 错误码语义表：errno 13 → 403，errno 2 → 404，用法错误（对目录 get、
  对文件 list）→ 400；部分服务器不设 errno，用报错文本兜底匹配。

## 4. 测试策略

- **桩测路由，真件测语义**。假通道桩全绿、真 bash 一跑就坏（heredoc 用例）。
  `LocalBashChannel`（真 bash 子进程实现通道鸭子接口）这套「进程桩」是本轮
  最值的投资——语法隔离、哨兵协议、退出码捕获只有真跑才测得出。
- **测试必须走被测对象自己的协议**：`_handleAcpPermission` 会重建
  `agent['permDecision']`，测试预置的决策字段直接被丢弃 → 挂 300 秒。要用
  后台线程等 handler 挂起 perm 后再写入决策。
- **轮询线程会竞争覆盖测试记录**：镜像循环线程把 `calls['payload']` 冲成
  None。按 method 分键记录，并让轮询端点返回终态让循环自然停下。
- **测试不得依赖宿主机状态**：模型白名单测试在装了真实 kimi config.toml 的
  机器上直接 400。要 patch `nodeModelDecl` 或用 tmp 配置，绝不读真实环境。
- **`monkeypatch` 懒导入的目标**：`agent_api → agent_local → agent_sftp →
  agent_api` 环靠函数内延迟导入断开；patch 时要 patch 懒导入实际取用的那个
  模块属性。

## 5. 工具与流程

- **永远不要 `taskkill /IM python.exe`**——本轮把 dev 和 prod 一起杀了。
  清理测试进程用 `netstat -ano | grep <port>` 拿 PID 再 `taskkill /PID`。
- **反馈先对照当前代码再动手**：反馈是拿旧版 prod 测的，多项（remote node
  校验、消息 409 JSON 等）dev 已修。逐项核对「反馈期望 vs 当前实现 vs 实际
  缺口」，避免重复造已存在的修复。
- **每阶段结束在 dev 上真机复验**（curl 打真实 SSH 主机 + 真本机 bash），
  再提交——trap 标记泄漏就是真机抓的，单元桩没抓到。提交信息里保留反馈
  编号（§3.4 / P0-2），反馈→提交可回溯。
- **反馈文档本身就是验收清单**：§5「做得好的地方」同样重要——列出的保留项
  （权限保留、并发串行、heredoc）在重构时全部加了回归测试，防止改坏。

## 6. 能力扩展模式（§7 选模型）

- **配置声明 + 自动发现双层**：执行点可在 config 显式声明 `models` 白名单
  （校验不命中 400 附列表）；未声明时自动发现——kimi 的 `config.toml` 可用
  `tomllib` 解析出 `[models.*]` 别名 + `default_model`（node /health 上报远端，
  web 侧解析本机，60s 缓存）。选错别名的快速失败由 kimi 自己完成，错误原文
  经任务 error 回流。
- **新增可选字段向后兼容**：联邦转发把 `model` 直接透传给对端 `/api/tasks`，
  旧版对端忽略未知字段安全降级为对端默认——新字段不需要锁版本。

## 7. 第二轮补充（2026-11-10 晚）

- **删 import 前先全文件搜索用法**：第一轮重写 agent_api 导入时删掉
  `import os`（当时唯一使用方 sftp 已迁走），skills 段还在用 → 上线后
  skills 接口全 500。规则：删 import 必须全文 grep；路由级测试要覆盖
  每个端点至少一次（skills 曾是盲区）。
- **PowerShell `exit` 语句会在对象格式化器之前终止进程**：重定向输出时
  `Get-Location` 这类对象输出整个消失（字符串没事）。修复是命令包进
  `& { ... } | Out-Default` 强制管道内同步格式化；进度流的 CLIXML 噪音
  用 `$ProgressPreference='SilentlyContinue'` 压制。定位靠开关矩阵实验
  （前缀/后缀/OutputFormat 逐项组合），比猜快得多。
- **`pkill -f '<模式>'` 会匹配到外层 bash 包装进程**：远程复合命令
  `bash -c "pkill -f 'xxx' ... setsid ... xxx"` 的命令行里含同样字符串，
  bracket 技巧 `[x]xx` 只防 pkill 自己，防不了包装 shell——自杀导致重启
  不执行。远程重启守卫进程用显式 PID 或锚定正则 `^python3 ...`。
- **老 Python 部署面（最终决策：不做兼容）**：node 跑在系统 python3
  (<3.11 无 tomllib) 曾让 /health 直接崩。先写了正则兜底，随后按
  "API 不做跨版本兼容"的原则改为：入口 fail-fast 版本守卫（≥3.11，
  报错里给 uv 建议）+ 远端改用 uv 跑 3.13。兼容垫片只该是过渡态，
  决策定了就删；部署后立刻打一次 /health 再宣告完成。
- **接手会话的默认安全侧**：kimi fork 有会话锁（占用中直接报错），API
  默认 fork:true 让外部智能体永不与用户终端并发写同一会话；fork 输出用
  正则提 `session_<uuid>`，锁错误原文透传给调用方。

## 8. 第三轮补充（2026-11-11，fbr4 复测后；条目为记录，修复单独派活）

- **重启 prod web 会打断经由 prod 驱动的任务回合**：任务线程活在 web 进程里，
  杀 web 即丢本轮 pending 输出与事件，任务落为 paused（部署命令的最后一步
  输出因此丢失）。规则：部署窗口放在任务回合落定之后（`waiting` 态可跨重启）；
  部署流程应加「有无 running/waiting 任务」预检，重启中断的回合要能被
  `message` 显式续跑。
- **重启后续聊死锁（待修，已复现）**：重启窗口内 node 侧会话发起的
  permission_request web 收不到——node 权限挂起（900s）、`running=true` 不变，
  web 侧 `message` 永远 409「远端会话正在执行」并把任务转 failed；`canResume`
  再续仍撞同一堵墙。规则方向：node 对未决权限做留存并在客户端重连后重推；
  web 对「远端忙」提供探测/强制取消出口，不允许无限 409 循环。
- **session/load 回放污染事件流（待修）**：resume 时 update 事件已有
  `replay` 过滤，但 `session/load` 回放的历史（旧 assistant 文本、旧
  permission_request）仍被当新事件落卡；审批历史请求必然 409「授权请求已
  失效」。规则：resume 路径回放的一切事件必须带 `replay` 标记并被任务卡
  跳过，只有新事件才是审批候选。
- **close 后 kimi acp 进程积压（归属未核实）**：close 后本机 node 下仍有
  7 个 acp 进程——分不清「为续聊故意保留（45 分钟空闲回收）」还是
  「宿主会话已删的泄漏进程」，node meta 没有 pid、审计没有进程清单。规则：
  node 会话 meta 带 pid 并在 /api/sessions 暴露；回收路径（idle/close/delete）
  各自留痕；诊断先列 pid↔sid 映射再下结论。
- **联邦成员模型缓存重启后有 ~60s 暗窗**：health 后台探测冷启动，第一轮
  刷新前 nodes 是灰的。规则：web 启动时主动预热一次（异步，不等它）；
  nodes 响应可带 `cacheAge`，让调用方知道拿到的是冷启动数据。
