# 设计模式 / 组件化 / 高内聚低耦合 核查报告

日期：2026-11-05
范围：`modules/`、`app.py`、`sessiond.py`、`frontend/assets/js/`、`frontend/*.html`、`tools/`
方法：依赖图实测（无环检测）、类与模式盘点、门禁复跑、跨模块私有名扫描、导航配置比对

---

## 一、结论

**总体判断：架构是合格的，且明显好于同类单用户 Windows 远控项目。**

- 依赖方向干净：**无循环依赖**，分层清晰，共享层收敛。
- 用到的 14 种设计模式**全部用在了正确的位置**，没有为用而用。
- 主要问题不在"模式选错"，而在**三处内聚溃散 + 一个门禁自身失效 + 一个封装破坏**。

**核查中首先发现一个必须先说的问题：文件预算门禁从未生效过。**

`tools/check_structure.py:37` 的 `read_lines()`：

```python
def read_lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read(), sum(1 for _ in f)      # f.read() 已读到 EOF, 迭代器为空
```

`f.read()` 耗尽文件后，`sum(1 for _ in f)` 恒为 `0`。因此 `scan_budget()` 的行数永远是 0，
**行数预算与存量例外检查自写成以来就是空转**。

修复后立刻暴露 6 个超预算文件：

| 文件 | 行数 | 规则 |
|---|---:|---|
| `frontend/assets/js/ssh-sftp-ui.js` | 618 | > 500 |
| `modules/termcore.py` | 568 | > 400 |
| `frontend/assets/js/files-methods-core.js` | 523 | > 500 |
| `modules/ssh_sftp.py` | 503 | > 400 |
| `modules/plugins.py` | 439 | > 400 |
| `frontend/assets/js/term-features.js` | 467 | > 464（存量只减不增） |

已修复 `read_lines`。这 6 个文件是本次核查的主要工作量来源。

---

## 二、设计模式盘点（对照 GoF 23 种）

### 用得好的（有证据）

| 模式 | 落点 | 评价 |
|---|---|---|
| **Facade** | `modules/files.py`、`ssh.py`、`control.py`、`tasks.py`、`aihandoff.py` | 每个特性一个装配门面，只做 import 与 `register()`，不写业务。统一、克制 |
| **Adapter** | `task_engines.ThreadEngine`；`ConPTYBackend` / `SSHBackend`；`app-shell.js` embed 分支 | `ThreadEngine` 把裸线程体适配成 `TaskEngine`，注释也写明了意图 |
| **Strategy** | `task_engines`（引擎注册表）、`decision.py`（Provider 协议） | 新增引擎只需 `register(name, engine)`，`tasks_runner` 无需 if/else |
| **Decorator / CoR** | `decision.FallbackProvider(primary=SemIf, fallback=Lexical)` | 用组合实现降级链，比继承干净 |
| **Proxy** | `sessionproxy.py`（本地/联邦 WS 代理）、`tasks_store._LazyRepo` | `sessionproxy` 把"本机 or 远端"这一决策收敛到一处 |
| **Registry** | `SessionRegistry`、`ConnectionRegistry`、`HostRepository`、`TaskRepository` | 都是薄封装 + 锁，没有膨胀成 Service Locator |
| **Observer** | `screen.FrameBroadcaster`、前端 `SSHHostStore`、`events.py` 审计 | 屏幕帧广播用订阅而非轮询拉取 |
| **State** | `task_state.TaskState(StrEnum)` | 状态机集中，未散落在 runner 里 |
| **Command** | `control_input.py`（电源/按键/文本）、`tasks_runner` 的 message 指令 | 危险动作走同一条命令通道，便于统一二次确认与审计 |
| **Factory Method** | `termcore.newSession()`、`newSessionID()` | 轻量，仅收敛 ID 生成与装配 |
| **Memento（轻量）** | `termcore._replaySnapshot()` + 会话回放缓冲 | 断线重连恢复输出，实现方式是快照而非逐帧重放 |
| **Template Method（轻量）** | `files_archive.StreamingZipJob` + `_ChunkSink` | 定义了 zip 流式产出的骨架 |
| **Mediator** | `shell.js`（壳 ↔ iframe 的 postMessage 中转） | 壳是唯一中介，iframe 不直接互相通信 |
| **Iterator** | `SessionRegistry.values()`、`StreamingZipJob.chunks()` | 生成器，未过度设计 |

### 有意缺席且判断正确的

`Abstract Factory`、`Builder`、`Prototype`、`Bridge`、`Flyweight`、`Interpreter`、`Visitor` ——
对"单用户、单机、一个人运维"的系统，这些只会增加间接层。**不加是对的**，符合
`principles.md` 原则 10「技术克制」。

### 一处不一致

`tasks_store` 把惰性状态封装成了 `_LazyRepo` 类，而 `inbox_scan.py:31,111-114` 用了
**5 个裸模块级变量**（`_hashCache`、`_fpCache`、`_fpLoaded`、`_fpDirty`、`_fpLastSave`）。
同一类问题两种解法，应统一（见建议 P3-2）。

---

## 三、高内聚低耦合核查

### 3.1 依赖方向（实测，通过）

```
app.py / sessiond.py
   └─ modules/*_api       路由层
        └─ *_runner       编排层
             └─ *_store   状态层
                  └─ config / state / internal_api / events
```

实测结果：

- **循环依赖：0**
- 功能模块之间**零横向 import**（`aihandoff → files` 的历史例外已消除）
- 共享层被依赖次数：`fsutil` 8、`internal_api` 5、`files_base` 5、`termcore` 4、
  `ssh_base` 4、`task_state` 4、`state` 3、`contracts` 2、`textio` 2、`media` 2
- `internal_api.py` 确实是 web ↔ sessiond 的**唯一契约**，双方无互相 import

这一项是全书最好的部分。`fsutil` / `textio` / `media` / `contracts` 的下沉是有效的。

### 3.2 内聚（三个不符合项）

按 `code-structure.md` §二「一个模块只应有一个变化理由」判定：

#### P1-1 `modules/termcore.py`（568 行，5 个变化理由）

| 职责 | 行段 |
|---|---|
| backend 抽象（`ConPTYBackend`） | 45-102 |
| 会话实体 + 注册表（`TermSession`、`SessionRegistry`） | 104-172 |
| 生命周期与回收线程（`newSession`/`_startReaper`/`_reader`/`_sessionReaper`） | 174-265 |
| Win32 前台进程与 cwd 探测（`_foregroundInfo`/`_ctime`/`_cwdLabel`） | 266-325 |
| **WS 帧协议**（`_decodeFrame`/`_handleControl`/`_replaySnapshot`/`_sendJson`/`_sendText`/`_closeWs`/`sealSocketAfterClose`） | 351-440 |

改 WS 协议要动会话注册表，改 Win32 探测要动协议——职责明显混在一起。
尤其 WS 帧协议与 `contracts.py`（帧 TypedDict 已单独成文件）是同一关注点，却分居两地。

#### P1-2 `modules/ssh_sftp.py`（503 行，2 个变化理由）

- **SFTP CRUD**：list / mkdir / rename / delete / chmod / read / write（7 个路由）
- **批量传输与打包**：download / download-zip / upload / upload-batch / to-local / from-local（6 个路由）+ `_zipRemoteTree` / `_zipAddFile`

按 `code-structure.md` §二的 store/runner/api 三段制，这是漏拆的一个。

#### P1-3 `modules/plugins.py`（439 行，4 个变化理由）

| 职责 | 函数 |
|---|---|
| 目录与清单解析 | `getRoots`/`_readManifest`/`_manifestApi`/`_discover` |
| 动态导入与注册 | `_importModule`/`_collectBlueprints`/`_collectSock`/`_register`/`_autoRoute`/`_loadOne` |
| 导航与标签推导 | `_templateTitle`/`_docLabel`/`_legacyLabel`/`_nativeNav`/`_openWith`/`_applyOverrides` |
| 登记查询 API | `getRegistry`/`getNavItems`/`isPluginApiPath` |

插件系统是后加的能力，长得最快，也最该拆。

### 3.3 封装破坏（1 处）

```
modules/ssh_terminal.py:75    payload, isCtrl = termcore._decodeFrame(msg)
```

`_decodeFrame` 是私有名（下划线开头），被另一个模块直接调用。这是全项目**唯一**一处
跨模块私有访问——数量极少，但性质明确：它说明 WS 帧解码本就该是公开契约
（放进 `contracts.py` 或新的 `wsproto.py`），而不是藏在 `termcore` 里靠下划线自律。

### 3.4 概念归属不清（2 处）

1. `termcore` 被 `terminal`、`ssh`、`ssh_terminal`、`sessionproxy` 四个模块依赖——
   它实际是**跨特性的共享内核**，但名字读起来像 terminal 私有。
2. `files_base` 被 5 个模块依赖，同理：它是 `files_*` 家族的门面，位置正确，
   但"基座"与"共享层"的界线只存在于文档，没有机制保证。

---

## 四、组件化核查

### 4.1 后端：合格

门面 + 基座 + store/runner/api 三段，模式统一。`*_base.py` 提供 `blueprint` 与错误类型，
特性模块只挂路由——这是干净的组件边界。

### 4.2 前端：结构对，缺共享组件层

**做得好的：**

- **IIFE 模块 + 显式 ctx 依赖注入**：`SSHSftpUI(ctx)`、`SSHUiCtx` 把依赖显式传入，
  不靠隐式全局。这是本书前端最好的设计。
- `WP_MSG`（`messages.js`）把壳/iframe 消息契约冻结成常量，不再裸写字符串。
- 每个 Vue 页面 `createApp` 独立，互不 import。
- HTML 几乎无内联业务逻辑（仅 `app.html` 1 处插件导航注入），符合
  `code-structure.md`「HTML 不写业务逻辑」。

**不符合的：**

#### P2-1 导航配置重复且已漂移

| 位置 | 内容 |
|---|---|
| `frontend/assets/js/app-shell.js:8-23` | `PRIMARY` + `MORE`，含 `/` `/files` `/terminal` `/ssh` `/screen` `/command` `/settings` |
| `frontend/assets/js/shell.js:4-18` | `VIEWS` + `NAV`，含 home/tasks/files/session/screen/command/settings |

两份定义**已经不一致**：新壳有 `tasks`（任务），旧壳没有。
`principles.md` 已否决的设计里明确列了「复制动作表」。导航是全局唯一面
（原则 2），配置却有两份。

两个壳不是死代码——`app-shell.js` 的 embed 分支（38-64 行）是把旧页面适配进新壳的
Adapter，职责真实存在。问题只在**导航数据应该只有一个来源**。

#### P2-2 无共享组件层

`common.js` 只共享 `fmtSize` / `fmtTime` / `fmtAgo` / `escapeHtml` / `toast` / `fetchJson`。
确认弹窗用原生 `confirm()`（`tasks.js:265`），每个 Vue 应用各自写模态与空态。
在当前规模下可接受（原则 10 技术克制），但已是第 3 个需要"确认对话框"的地方，
继续增长前应抽一个 `WPCommon.confirm()`。

---

## 五、建议（按优先级）

| # | 事项 | 依据 | 代价 |
|---|---|---|---|
| **P0** | `read_lines` 修复（**已完成**） | 门禁空转，6 个文件超预算未被发现 | 1 行 |
| **P1-1** | 拆 `termcore.py`：`wsproto`（帧编解码/控制帧/回放）+ `termprobe`（Win32 前台探测）+ `termcore`（backend/实体/注册表/生命周期） | 5 个变化理由；`_decodeFrame` 私有名被跨模块调用 | 中 |
| **P1-2** | 拆 `ssh_sftp.py`：`ssh_sftp_base`（blueprint + 上下文管理器）+ `ssh_sftp_ops`（CRUD）+ `ssh_sftp_transfer`（流式传输与打包） | 2 个变化理由，漏三段制 | 中 |
| **P1-3** | 拆 `plugins.py`：`plugins_base` + `plugins_discover` + `plugins_load` + `plugins_nav` | 4 个变化理由 | 中 |
| **P2-1** | 导航配置单一来源：服务端下发的 `__WP_PLUGIN_NAV__` 已是一处，把 `PRIMARY/MORE/VIEWS/NAV` 也收敛到 `messages.js` 或服务端一处 | 两份定义已漂移；原则 2 + 已否决「复制动作表」 | 小 |
| **P2-2** | `common.js` 增 `confirm()`，逐步替换原生 `confirm` | 第 3 处使用点 | 小 |
| **P3-1** | `inbox_scan` 的 5 个裸缓存变量封装成类，与 `_LazyRepo` 一致 | 同类问题两种解法 | 小 |
| **P3-2** | `files_archive` 与 `ssh_sftp` 的 zip 策略（budget / 名称净化 / 重名）抽到 `fsutil` 或新 `ziputil` | 两处 zip 实现，策略重复 | 小 |
| **P3-3** | `termcore` 更名或在文档中明确其"跨特性共享内核"地位 | 名字误导归属 | 极小 |

---

## 六、复核方式

```bash
.venv/Scripts/python.exe tools/check_structure.py    # 预算 + pyflakes + 横向 import
.venv/Scripts/python.exe tools/check_frontend.py     # 资源/id/调用目标/API 端点
.venv/Scripts/python.exe -m pytest -q
```

P1 三项拆完后，`check_structure.py` 应回到 0 errors，且拆出的文件都 < 400/500 行。
