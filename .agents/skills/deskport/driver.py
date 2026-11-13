"""通用驱动：把同目录的 deskport.py 当方向盘，指挥一个托管智能体干活。

    driver.py -h | --help                                 打印本用法（不碰任何状态）
    driver.py <别名> new <会话id|w:工作线id|-> [档位]      先 new 建任务，之后才能 y/n/c/m/st
    driver.py <别名> [y|n|c|m [档位]|st]

    new <目标>  用 msg.txt 建任务：`new <kimi会话id>` 接手已有会话（fork 副本），
               `new w:<工作线id>` 在工作线上派活，`new -` 直接新开任务
    (别名后无命令)  接着跑，等这一轮结束（结论写 run/<别名>/last.txt 一行）
    y | n      批准 / 拒绝卡住的审批，然后接着跑
    c          压缩执行方上下文（/compact，一批活之间用）
    m [档位]   把 run/<别名>/msg.txt 发出去；换档位时同时切换预授权
    st         只打印状态（任务号、档位、上下文用量、有没有挂着的审批）

边界全在 driver.json（脚本同目录，或 DRIVER_CONFIG 指过去）：必填 root/danger/safe，
可选 profiles/extraWritable/defaultProfile/serverPolicy——字段与最小样例见同目录
driver.example.json；缺文件、缺键都会一句话报错并指向它。

「响应即上下文」的落实：stdout 只出一行结论（DONE / STOP / TIMEOUT），全量流水进
run/<别名>/log；命令本身短到两个字符（`driver.py a y`）。
"""
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import time
import traceback
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deskport  # noqa: E402  同目录的单文件客户端

HERE = Path(__file__).resolve().parent
RUN_ROOT = Path(os.environ.get("DRIVER_RUN_DIR") or (HERE / "run"))
TIMEOUT, MAX_TEXT, STALL_SEC = 900, 3000, 600   # 一轮等待上限 / 结论正文上限 / 无事件判停
EXAMPLE = HERE / "driver.example.json"          # 最小样例：driver.json 缺什么照它补
REQUIRED_KEYS = ("root", "danger", "safe")      # 边界三件套：去哪写、什么停、什么放


class DriverError(Exception):
    """可预期的失败：一句人话 + 退出码 2，不吐栈（要栈用 DRIVER_DEBUG=1）。"""


# 证书错误口径与 deskport.py 的 CLI 一致（批次 M）：同一句话术，不造第二套
CERT_HINT = """driver: 对端证书校验失败（自签证书：常见于反向代理 / frp 隧道的 HTTPS 入口）
  三种处理，按可信度从高到低：
   1) 把对端证书存成文件，用 connect(cafile="/path/to/cert.pem") 或 .deskport.json 的 "cafile" 键
   2) 确认对端就是你要连的那台 Deskport 时，在 .deskport.json 加 "insecure": true
   3) 同一网段可改用内网明文地址（http://<host>:6789，不需要证书）
  注意：只有在你确认连的是自己的 Deskport 时才可用 2) 或 3)。"""


def _fail(run, msg, code=2, err=None):
    """一句人话收尾（run 建好了进 last.txt/log）；DRIVER_DEBUG=1 才打栈到 stderr。"""
    if run is None:
        print(msg, flush=True)
    else:
        run.say(msg)
    if err is not None and os.environ.get("DRIVER_DEBUG"):
        traceback.print_exc()
    return code


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
            raw = self.state.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            return json.loads(raw)
        except ValueError as err:
            raise DriverError("%s 损坏（%s）；删掉它即可重置这个别名"
                              % (self.state, err)) from None

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
    """跑到本轮结束 / 需要人判断 / 超时。返回退出码：0 DONE、2 超时、3 停下。"""
    end = time.time() + timeout
    auto = ghost = 0
    lastStale = None
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
                    if err.status == 409:
                        # 审批已失效（2026-11-14 外部智能体反馈：误报 STALE 打死
                        # 6+ 个驱动窗口）。两种因：竞态（刚被解掉/回放）或会话已死。
                        # 同一 requestId 反复 409 = 盘上留着死标记，必须停；
                        # 否则跳过这条，继续等下一个决策点，不误杀窗口
                        rid = event.get("requestId")
                        if rid and rid == lastStale:
                            run.say("STOP 审批 %s 反复失效：任务盘上留着死标记，"
                                    "先 driver.py <别名> st 查状态，发消息唤醒或重建" % rid)
                            return 3
                        lastStale = rid
                        info = _retry(task.info, run)
                        if info.get("status") in ("paused", "interrupted", "failed",
                                                  "closed", "cancelled"):
                            run.say("STOP 审批 %s 已失效，任务%s：先 st 查状态，"
                                    "发消息唤醒或重建" % (rid, info.get("status")))
                            return 3
                        run.log("  [skip] 审批 %s 已失效（竞态/回放），继续等下一决策点" % rid)
                        task.seq = r.get("lastSeq") or task.seq
                        continue
                    raise
                auto += 1
                lastStale = None
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
    """边界配置：缺失/损坏/缺键/坏正则都是 DriverError——一句话说清缺什么，指向 example。"""
    path = Path(os.environ.get("DRIVER_CONFIG") or HERE / "driver.json")
    if not path.exists():
        raise DriverError("找不到边界配置 %s；照同目录 %s 抄一份 "
                          "（或设 DRIVER_CONFIG 指过去）" % (path, EXAMPLE.name))
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        raise DriverError("边界配置 %s 损坏（%s）；照同目录 %s 重写"
                          % (path, err, EXAMPLE.name)) from None
    if not isinstance(cfg, dict):
        raise DriverError("边界配置 %s 不是 JSON 对象；照同目录 %s 重写"
                          % (path, EXAMPLE.name))
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise DriverError("边界配置 %s 缺键 %s；字段见同目录 %s"
                          % (path, "、".join(missing), EXAMPLE.name))
    try:
        re.compile(cfg["danger"])
        re.compile(cfg["safe"])
    except re.error as err:
        raise DriverError("边界配置 %s 的 danger/safe 不是合法正则（%s）；"
                          "改法见同目录 %s" % (path, err, EXAMPLE.name)) from None
    return cfg


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip(), flush=True)
        return 0
    alias, rest = argv[0], argv[1:]
    cmd = rest[0] if rest else ""
    run = None
    try:
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
        if cmd != "new" and not st.get("task"):     # 判空前置：新别名不走任何网络
            run.say("STOP 别名 %s 还没有任务，先 driver.py %s new <会话id|w:工作线id|->"
                    % (alias, alias))
            return 3
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
    except DriverError as err:                 # 可预期失败：一句人话 + exit 2，无栈
        return _fail(run, "driver: %s" % err, 2, err)
    except urllib.error.URLError as err:       # 首次连接最常见：证书 / 连不上
        reason = getattr(err, "reason", err)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return _fail(run, CERT_HINT, 2, err)
        if isinstance(reason, ssl.SSLError):
            return _fail(run, "driver: 与对端的 TLS 连接失败（%s）；证书不受信任的处理"
                          "同证书校验失败，见 %s 的 _说明" % (reason, EXAMPLE.name), 2, err)
        if isinstance(reason, ConnectionError):
            return _fail(run, "driver: 连不上对端（%s）；查地址、网络与服务是否在跑" % reason,
                         2, err)
        return _fail(run, "ERROR URLError: %s" % str(err)[:300], 1, err)
    except (OSError, http.client.HTTPException, deskport.DeskportError) as err:
        return _fail(run, "ERROR %s: %s" % (type(err).__name__, str(err)[:300]), 1, err)
    except ValueError as err:                  # 缺令牌/缺地址/prompt 超长：deskport 已说清
        return _fail(run, "driver: %s" % err, 2, err)
    except Exception as err:                   # 顶层兜底：一句人话，栈只在 DRIVER_DEBUG=1
        head = (str(err).strip().splitlines() or ["无描述"])[0]
        return _fail(run, "driver: 内部错误 %s: %s" % (type(err).__name__, head[:300]), 2, err)
    finally:
        if run is not None:
            try:
                run.lock.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())