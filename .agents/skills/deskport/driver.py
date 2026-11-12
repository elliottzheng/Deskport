"""通用驱动：把同目录的 deskport.py 当方向盘，指挥一个托管智能体干活。

    driver.py <别名> [y|n|c|m [档位]|new <会话id|w:工作线id> [档位]|st]

    (无参数)   接着跑，等这一轮结束（结论写 run/<别名>/last.txt 一行）
    y | n      批准 / 拒绝卡住的审批，然后接着跑
    c          压缩执行方上下文（/compact，一批活之间用）
    m [档位]   把 run/<别名>/msg.txt 发出去；换档位时同时切换预授权
    new <目标> 用 msg.txt 建任务：`new <kimi会话id>` 接手已有会话（fork 副本），
               `new w:<工作线id>` 在工作线上派活，`new -` 直接新开任务
    st         只打印状态（任务号、档位、上下文用量、有没有挂着的审批）

边界全在 driver.json（脚本同目录，或 `DRIVER_CONFIG` 指过去）：

    {
      "root": "E:/proj/app",                  // 可写根目录（执行方的边界）
      "danger": "rm\\s+-rf|\\\\bkill\\\\b|deploy",      // 命中即停下等人
      "safe": "^\\\\s*(ls|cat|grep|git (status|diff|log))\\\\b",  // 只读/测试，自动放行
      "profiles": {"dev": "<正则>", "commit": "<正则>"},   // 规格预授权档位（§8c）
      "extraWritable": ["/tmp/build/"], "defaultProfile": "dev",
      "serverPolicy": true                    // 同时给服务端 policy 兜一层
    }

「响应即上下文」的落实：stdout 只出一行结论（DONE / STOP / TIMEOUT），全量流水进
run/<别名>/log；命令本身短到两个字符（`driver.py a y`）。
"""
import hashlib
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deskport  # noqa: E402  同目录的单文件客户端

HERE = Path(__file__).resolve().parent
RUN_ROOT = Path(os.environ.get("DRIVER_RUN_DIR") or (HERE / "run"))
TIMEOUT, MAX_TEXT, STALL_SEC = 900, 3000, 600   # 一轮等待上限 / 结论正文上限 / 无事件判停


# ---------------------------------------------------------------- 边界判定

def executablePart(cmd: str) -> str:
    """只留真正会被 shell 执行的部分：去掉 heredoc 正文与引号内字符串。

    测试数据、代码文本里出现 `rm -rf`/端口号不算越界——审批别被正文噪音绊住。
    """
    s = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b", "<<HEREDOC", cmd, flags=re.S)
    s = re.sub(r"'[^']*'", "''", s)
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)


def splitSegments(cmd: str) -> list:
    """按**带空白**的分隔符切段：grep 正则里的 `\\|` 不是管道。"""
    bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', cmd)
    return [p for p in re.split(r"\s(?:&&|\|\||\|)\s|;\s|\n", bare) if p.strip()]


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").lower()


def inside(path: str, root: str, extras=()) -> bool:
    p = _norm(path)
    for base in [root] + list(extras):
        b = _norm(base).rstrip("/")
        if b and (p == b or p.startswith(b + "/")):
            return True
    return False


class Policy:
    """把 driver.json 的边界翻译成 allow/ask。纯函数，可单测。"""

    def __init__(self, cfg: dict):
        self.root = cfg.get("root") or ""
        self.danger = re.compile(cfg["danger"]) if cfg.get("danger") else None
        self.safe = re.compile(cfg["safe"]) if cfg.get("safe") else None
        self.extras = cfg.get("extraWritable") or []
        self.profiles = cfg.get("profiles") or {}
        self.server = cfg.get("serverPolicy", True)

    def preauth(self, profile: str):
        pat = self.profiles.get(profile)
        return re.compile(pat) if pat else None

    def _cleanupOnly(self, cmd: str) -> bool:
        """`cd <root> && rm -rf build dist` 这类清理自己产物，不按危险算。"""
        segs = [s.strip() for s in re.split(r"&&|;", executablePart(cmd))]
        rm = [s for s in segs if re.match(r"rm\s+(-\w+\s+)*", s)]
        if not rm:
            return False
        cd = [s for s in segs if s.startswith("cd ")]
        if not self.root or not cd or not inside(cd[0][3:].strip().strip('"\''), self.root, self.extras):
            return False
        for s in rm:
            for target in s.split()[2:]:
                if target.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", target) or ".." in target:
                    return False
        return True

    def classify(self, event: dict) -> tuple:
        """返回 (allow|ask, 理由)。event 是 permission_request（带 args 最好）。"""
        args = event.get("args") or {}
        if isinstance(args, str):
            args = {"command": args}
        name = (event.get("name") or event.get("title") or "").lower()
        cmd = args.get("command") or args.get("cmd") or ""
        path = args.get("path") or args.get("file_path") or args.get("filePath") or ""
        profile = event.get("_profile") or ""
        pre = self.preauth(profile) if profile else None
        if cmd and pre and pre.search(cmd):
            return "allow", "规格预授权"
        if cmd and self.danger and self.danger.search(executablePart(cmd)) \
                and not self._cleanupOnly(cmd):
            return "ask", "命中危险关键词"
        if path and self.danger and self.danger.search(_norm(path)):
            return "ask", "路径命中危险关键词"
        if cmd:
            if self.safe and all(self.safe.match(p) for p in splitSegments(cmd)):
                return "allow", "只读/测试命令"
            return "allow", "边界内"          # 没命中 danger：按边界放行，不逐条盯
        if path:
            return ("allow", "边界内文件") if inside(path, self.root, self.extras) \
                else ("ask", "边界外路径")
        if any(k in name for k in ("read", "grep", "glob", "search", "list")):
            return "allow", "只读工具"
        return "ask", "无法识别"

    @staticmethod
    def pick(event: dict, allow: bool) -> str:
        opts = event.get("options") or []
        want = ("allow_once", "allow") if allow else ("reject_once", "reject", "deny")
        for w in want:
            for o in opts:
                if w in (o.get("kind") or "").lower() or w in (o.get("optionId") or "").lower():
                    return o.get("optionId", "")
        return (opts[0] if allow else opts[-1]).get("optionId", "") if opts else ""

    def serverPolicy(self) -> dict:
        """服务端那一层（先放行，越界才挂起）；disabled 时空 dict。"""
        if not self.server:
            return {}
        deny = self.danger.pattern if self.danger else None
        return {"allowPaths": [self.root], "allowCommands": ["."],
                "denyCommands": [deny] if deny else [],
                "default": "ask", "timeoutSec": 0}


# ---------------------------------------------------------------- 别名目录

class Run:
    """一个别名一个目录：state.json / msg.txt / last.txt / log / pending.json。"""

    def __init__(self, alias: str):
        self.dir = RUN_ROOT / alias
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = self.dir / "state.json"
        self.msg = self.dir / "msg.txt"
        self.last = self.dir / "last.txt"
        self.logf = self.dir / "log"
        self.pending = self.dir / "pending.json"
        self.lock = self.dir / "lock"

    def load(self) -> dict:
        try:
            return json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, st: dict) -> None:
        self.state.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def log(self, line: str) -> None:
        with self.logf.open("a", encoding="utf-8") as f:   # 全量流水只进文件
            f.write(line + "\n")

    def say(self, line: str) -> None:
        """结论：stdout 一行 + last.txt 追加（被挪到后台也只读这一行）。"""
        print(line, flush=True)
        self.log(line)
        with self.last.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def busy(self) -> bool:
        """同一别名只允许一个驱动在跑（心跳 120s）。"""
        try:
            pid = self.lock.read_text(encoding="utf-8").strip()
            return (time.time() - self.lock.stat().st_mtime < 120
                    and pid != str(os.getpid()))
        except OSError:
            return False


# ---------------------------------------------------------------- 驱动循环

def _brief(event: dict) -> str:
    a = event.get("args") or {}
    if isinstance(a, str):
        a = {"command": a}
    return (a.get("command") or a.get("path") or json.dumps(a, ensure_ascii=False))[:400]


def _retry(fn, run, tries=3):
    """隧道抖动（SSL EOF / 读超时 / 502-504）整体重试。"""
    for i in range(tries):
        try:
            return fn()
        except (deskport.DeskportError, OSError, http.client.HTTPException) as err:
            fatal = isinstance(err, deskport.DeskportError) and err.status not in (502, 503, 504)
            if fatal or i == tries - 1:
                raise
            run.log("  [net-retry] %s: %s" % (type(err).__name__, str(err)[:120]))
        time.sleep(5)


def drive(dp, task, run: Run, policy: Policy, profile: str,
          queued: list, timeout=TIMEOUT) -> int:
    """跑到本轮结束 / 需要人判断 / 超时。返回退出码：0 DONE、2 超时、3 停下、4 失效审批。"""
    end = time.time() + timeout
    auto = ghost = 0
    while time.time() < end:
        r = _retry(lambda: task.until(min(60, max(1, int(end - time.time()) + 1))), run)
        if r is None:                                    # 老中枢没有阻塞 wait：不降级轮询
            run.say("ERROR 中枢不支持 /wait（R4-3 之前），先升级中枢再驱动")
            return 1
        st = r.get("status")
        if st == "permission":
            event = dict(r.get("pending") or {}, _profile=profile)
            verdict, why = policy.classify(event)
            if verdict == "allow":
                try:
                    task.permission(event.get("requestId"), policy.pick(event, True))
                except deskport.DeskportError as err:
                    if err.status == 409:                # 回放出来的旧审批，不能当新请求
                        run.say("STALE 审批已失效（多半是回放污染），停下待查：%s" % err)
                        return 4
                    raise
                auto += 1
                run.log("  [auto-allow:%s] %s" % (why, _brief(event)))
                continue
            run.pending.write_text(json.dumps(event, ensure_ascii=False, indent=1), encoding="utf-8")
            run.say("STOP %s | %s | %s" % (why, event.get("name"), _brief(event)))
            return 3
        if st == "paused":                               # 中枢重启等原因暂停：不是超时也不是完成
            run.say("STOP paused 任务被暂停，先查状态再续（driver.py <别名> st）")
            return 3
        if st == "running":
            info = _retry(task.info, run)
            if info.get("pendingPermission"):            # 服务端 wait 漏报挂起审批的兜底
                task.seq = 0
                continue
            idle = time.time() - (info.get("lastActiveAt") or time.time())
            if idle > STALL_SEC and not info.get("currentTool"):
                ctx = info.get("context") or {}
                run.say("STOP stall %ds 无事件且无工具在跑；context=%s/%s，建议换新会话接棒"
                        % (int(idle), ctx.get("used"), ctx.get("max")))
                return 3
            continue
        if queued and st == "waiting":                   # 本轮结束：补发 409 排队的消息
            _retry(lambda: task.message(queued.pop(0)), run)
            time.sleep(5)
            run.log("  [queued-sent] 已补发排队消息")
            continue
        pend = (_retry(task.info, run)).get("pendingPermission")
        if pend:                                         # 轮末才冒出来的"幽灵审批"
            ghost += 1
            if ghost > 3:
                run.pending.write_text(json.dumps({"requestId": pend, "options": []}), encoding="utf-8")
                run.say("STOP ghost 轮末仍挂着审批 %s，先 driver.py <别名> n 拒掉再发消息" % pend)
                return 3
            task.seq = 0
            run.log("  [ghost] 轮末仍挂审批 %s（第 %d 次），回头处理" % (pend, ghost))
            continue
        stats = r.get("stats") or {}
        txt = r.get("finalText") or ""
        run.log("[assistant] " + txt)
        cut = "…(+%d 字，见 log)" % (len(txt) - MAX_TEXT) if len(txt) > MAX_TEXT else ""
        run.say("DONE %s tools=%s server-auto=%s local-auto=%d errors=%s\n%s%s"
                % (st, stats.get("tools"), stats.get("autoApproved"), auto,
                   stats.get("errors"), txt[:MAX_TEXT], cut))
        return 0
    run.say("TIMEOUT 任务仍在跑")
    return 2


# ---------------------------------------------------------------- 入口

def loadConfig() -> dict:
    path = Path(os.environ.get("DRIVER_CONFIG") or HERE / "driver.json")
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg.setdefault("root", os.getcwd())
    return cfg


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip().splitlines()[0])
        print("用法: driver.py <别名> [y|n|c|m [档位]|new <会话id|w:工作线id> [档位]|st]", flush=True)
        return 1
    alias, rest = argv[0], argv[1:]
    cmd = rest[0] if rest else ""
    cfg = loadConfig()
    policy = Policy(cfg)
    run = Run(alias)
    if run.busy():
        print("BUSY 别名已有驱动在运行，未启动第二个", flush=True)
        return 6
    run.lock.write_text(str(os.getpid()), encoding="utf-8")
    run.last.write_text("", encoding="utf-8")
    st = run.load()
    queued = st.pop("queued", []) or []
    if st.get("root"):
        cfg["root"] = st["root"]
        policy = Policy(cfg)
    profile = st.get("profile") or cfg.get("defaultProfile") or "dev"
    try:
        dp = deskport.connect().dp
        text = None
        if cmd == "new":
            target = rest[1] if len(rest) > 1 else "-"
            profile = rest[2] if len(rest) > 2 else profile
            text = run.msg.read_text(encoding="utf-8")
            opts = {"policy": policy.serverPolicy() or None}
            if target.startswith("w:"):
                routed, task = dp.workline(target[2:]).send(text, **opts)
                run.log("  [routed] %s" % routed)
            elif target == "-":
                task = dp.create_task(text, **opts)
            else:
                task = dp.create_task(text, kimi_session=target, **opts)
            st["task"] = task.id
            run.say("NEW %s" % task.id)
        else:
            task = dp.task(st["task"])
            if not st.get("task"):
                run.say("STOP 别名 %s 还没有任务，先 driver.py %s new <会话id|w:工作线id|->" % (alias, alias))
                return 3
            if cmd == "st":
                info = _retry(task.info, run)
                q = "有挂起审批" if info.get("pendingPermission") or run.pending.exists() else "无挂起审批"
                ctx = info.get("context") or {}
                run.say("ST %s profile=%s %s context=%s/%s" % (info.get("status"), profile, q,
                        ctx.get("used"), ctx.get("max")))
                return 0
            if cmd in ("y", "n"):
                if run.pending.exists():
                    event = json.loads(run.pending.read_text(encoding="utf-8"))
                    try:
                        _retry(lambda: task.permission(event.get("requestId"),
                                                       policy.pick(event, cmd == "y"), deny=cmd == "n"), run)
                    except deskport.DeskportError as err:
                        run.say("WARN 审批失败（可能已失效）：%s" % err)
                    run.pending.unlink()
                task.seq = 0                       # 处理完回头把这一轮从头发一遍
            elif cmd == "c":
                _retry(lambda: task.message("/compact"), run)
            elif cmd == "m":
                profile = rest[1] if len(rest) > 1 else profile
                text = run.msg.read_text(encoding="utf-8")
                digest = hashlib.sha1(text.encode()).hexdigest()
                if digest == st.get("lastMsg"):     # msg.txt 没更新就别重发旧消息
                    run.say("REFUSE msg.txt 与上次发送内容相同，未发送")
                    return 5
                try:
                    _retry(lambda: task.message(text), run)
                    st["lastMsg"] = digest              # 送达后才记，409 时可重发
                except deskport.DeskportError as err:
                    if err.status != 409:
                        raise
                    run.say("QUEUED 消息未送达（本轮在跑），结束后自动补发")
                    queued.append(text)
            else:                                   # 单纯续跑：从最后一条 user 之后算
                users = [e.get("seq", 0) for e in task.events() if e.get("type") == "user"]
                if users:
                    task.seq = users[-1]
                if _retry(task.info, run).get("pendingPermission"):
                    task.seq = 0
        st.update(task=st.get("task"), profile=profile, root=cfg.get("root"))
        run.save(st)
        rc = drive(dp, task, run, policy, profile, queued)
        st["queued"] = list(queued)                 # 没补发出去的落盘，下次接着补
        run.save(st)
        return rc
    except (OSError, http.client.HTTPException, deskport.DeskportError) as err:
        run.say("ERROR %s: %s" % (type(err).__name__, str(err)[:300]))
        return 1
    finally:
        try:
            run.lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())