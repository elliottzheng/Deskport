"""Deskport Agent API 的极简 Python 封装（仅依赖标准库）。

    import deskport
    h = deskport.connect()                 # 读 .deskport.json 的 url/host/insecure
    print(h.sh("uptime"))                  # 返回 stdout，失败抛 CommandError
    repo = h.cd("/srv/app")                # 绑定工作目录；相对路径都按它解析
    print(repo.sh("git log --oneline -5"))
    print(repo.ls("."))
    repo.write("notes.txt", "hello"); print(repo.read_text("notes.txt"))

配置（后者覆盖前者）：脚本同目录 .deskport.json → 环境变量 DESKPORT_URL /
DESKPORT_HOST / DESKPORT_INSECURE=1 → 函数参数。
令牌：参数 token → 环境变量 DESKPORT_TOKEN → 脚本同目录 .agent-token。

服务端已知行为：exec 输出超过 2 MiB 会被截断、超时上限 600s（truncated=True，sh() 会告警）；
命令里含 NUL 字符服务端会 500，这里在客户端提前拒绝。
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shlex
import ssl
import urllib.error
import urllib.parse
import urllib.request
import posixpath
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent


class DeskportError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class CommandError(Exception):
    def __init__(self, result: "ExecResult"):
        super().__init__(
            f"command failed (exit={result.exit}, timedOut={result.timed_out}): "
            f"{result.command}\n{result.err.strip() or result.out.strip()}"
        )
        self.result = result


@dataclass
class ExecResult:
    command: str
    exit: int | None
    out: str
    err: str
    timed_out: bool
    truncated: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit == 0 and not self.timed_out

    def check(self) -> "ExecResult":
        if not self.ok:
            raise CommandError(self)
        return self

    def __str__(self) -> str:
        return self.out


@dataclass
class Entry:
    name: str
    path: str
    size: int
    mtime: int
    dir: bool

    def __repr__(self) -> str:
        return f"{self.name}{'/' if self.dir else ''}"


class Deskport:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 token_file: str | os.PathLike | None = None,
                 insecure: bool = False, cafile: str | None = None,
                 http_timeout: float = 60, pin: bool = False):
        self.base_url = (base_url or os.environ.get("DESKPORT_URL", "")).rstrip("/")
        if not self.base_url:
            raise ValueError("需要 base_url 或环境变量 DESKPORT_URL")
        self._token = token or os.environ.get("DESKPORT_TOKEN") or self._read_token(token_file)
        if insecure:
            self._ctx = ssl._create_unverified_context()
        else:
            self._ctx = ssl.create_default_context(cafile=cafile)
            if pin and cafile:
                # 证书锁定：只信任 cafile 里这一张自签名证书，不再校验主机名（同一张证书服务 dx/yd/lt 多个运营商入口，SAN 只有 dx）
                self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self._ctx.check_hostname = False
                self._ctx.verify_mode = ssl.CERT_REQUIRED
                self._ctx.load_verify_locations(cafile=cafile)
        self.http_timeout = http_timeout

    @staticmethod
    def _read_token(token_file) -> str:
        p = Path(token_file) if token_file else _HERE / ".agent-token"
        try:
            return p.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise ValueError(f"找不到令牌：{p}（或设置 DESKPORT_TOKEN）") from None

    def __repr__(self) -> str:  # 不泄露令牌
        return f"Deskport({self.base_url!r})"

    # ---- 底层请求 ----
    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("X-Agent-Token", self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            for attempt in (1, 2):   # 隧道偶尔截断大响应（IncompleteRead）；GET 幂等，重试一次
                try:
                    with urllib.request.urlopen(req, context=self._ctx,
                                                timeout=timeout or self.http_timeout) as r:
                        return json.loads(r.read().decode("utf-8"))
                except http.client.IncompleteRead:
                    if method != "GET" or attempt == 2:
                        raise
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(raw)
                msg = j.get("error", raw)
                hints = {k: v for k, v in j.items() if k not in ("error", "code")}
                if hints:   # availableHosts / nodes / models / shells / requestId 等提示
                    msg = f"{msg} {json.dumps(hints, ensure_ascii=False)[:500]}"
            except ValueError:
                msg = raw.strip()[:300]
            raise DeskportError(e.code, msg) from None

    # ---- 持久会话 / 任务派活 ----
    def session(self, host: str, cwd: str | None = None) -> "Session":
        body = {"host": host}
        if cwd:
            body["cwd"] = cwd
        return Session(self, self._request("POST", "/api/agent/ssh/sessions", body)["sessionId"])

    def nodes(self) -> list[dict]:
        return self._request("GET", "/api/tasks/nodes")["nodes"]

    # ---- 工作线（R5-1a）：接力棒由 Deskport 保存，新任务自动注入，执行方轮末回写 WORKLINE.md ----
    def worklines(self) -> list[dict]:
        return self._request("GET", "/api/worklines").get("worklines", [])

    def workline(self, wid: str) -> "Workline":
        return Workline(self, wid)

    def create_workline(self, wid: str, title: str, doc: str, policy: dict | None = None,
                        mode: str = "default", repo: str = "", branch: str = "") -> "Workline":
        """doc = 接力棒初稿（markdown）；policy 会带到这条工作线上开的每个任务。"""
        self._request("POST", "/api/worklines", {
            "id": wid, "title": title, "doc": doc, "policy": policy or {},
            "defaults": {"mode": mode}, "repo": repo, "branch": branch})
        return Workline(self, wid)

    def kimi_sessions(self, cwd: str | None = None) -> list[dict]:
        """Deskport 本机已有的 Kimi CLI 会话（接手候选，最近更新在前）。"""
        q = "?cwd=" + urllib.parse.quote(cwd) if cwd else ""
        return self._request("GET", "/api/tasks/kimi-sessions" + q)["sessions"]

    def tasks(self) -> list[dict]:
        return self._request("GET", "/api/tasks")["tasks"]

    def task(self, task_id: str) -> "Task":
        return Task(self, task_id)

    def create_task(self, prompt: str, node: str = "local", mode: str = "default",
                    cwd: str | None = None,
                    paths: list[str] | None = None, model: str | None = None,
                    attachments: dict[str, bytes | str] | None = None,
                    kimi_session: str | None = None, fork: bool = True,
                    policy: dict | None = None) -> "Task":
        """派活给 Deskport 托管的智能体。默认 mode=default（危险操作逐项等审批），
        需要全自动时显式传 mode="auto"。node=执行点（缺省 "local"）；cwd=执行目录
        （本机可给绝对目录，任务目录仍是 workspace；非本机给该机器上已存在的目录，
        缺省 = 任务目录）。两者都支持 kimi_session 接手（R4-7）。
        model 可选（列表见 nodes() 的 models）；attachments={文件名: 内容} 随任务上传。
        prompt 服务端上限 4000 字符，长材料放 attachments。
        kimi_session=已有 Kimi 会话 id（见 kimi_sessions()）：默认 fork 副本再接手，
        fork=False 直接接管原会话（被占用时服务端 409）。
        policy=服务端审批边界（R4-2）：{"allowPaths":[...], "allowCommands":[正则], "denyCommands":[正则],
        "default":"ask|allow|deny", "timeoutSec":n}；边界内自动放行，越界才挂起。"""
        if len(prompt) > 4000:
            raise ValueError(f"prompt {len(prompt)} 字符超过服务端上限 4000；长内容请用 attachments")
        body: dict = {"prompt": prompt, "node": node, "mode": mode}
        if model:
            body["model"] = model
        if kimi_session:
            body["kimiSessionId"] = kimi_session
            if not fork:
                body["fork"] = False
        if attachments:
            body["attachments"] = [
                {"name": n, "contentB64": base64.b64encode(
                    d.encode("utf-8") if isinstance(d, str) else d).decode()}
                for n, d in attachments.items()]
        if node and node != "local" and not cwd:
            raise ValueError("非本机执行点需要 cwd（该机器上已存在的目录）")
        if cwd:
            body["cwd"] = cwd
        if paths:
            body["paths"] = paths
        if policy:
            body["policy"] = policy
        return Task(self, self._request("POST", "/api/tasks", body)["task"]["id"])

    # ---- 端点 ----
    def hosts(self) -> list[dict]:
        return self._request("GET", "/api/agent/hosts")["hosts"]

    def host(self, name: str, cwd: str | None = None) -> "Host":
        return Host(self, name, cwd)

    def upload(self, name: str, data: bytes | str) -> str:
        """上传文件到 Deskport 本机任务区，返回本机路径（可放进 create_task 的 paths）。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        j = self._request("POST", "/api/tasks/files",
                          {"name": name, "contentB64": base64.b64encode(data).decode()})
        return j.get("path") or j

    def run(self, host: str, command: str, timeout: int = 30, check: bool = False,
            cwd: str | None = None, shell: str | None = None) -> ExecResult:
        """shell 仅对 host="local" 有效：bash（默认）/ powershell / pwsh。"""
        if "\x00" in command:
            raise ValueError("命令里含 NUL 字符（服务端会 400）")
        timeout = max(1, min(600, int(timeout)))
        body = {"host": host, "command": command, "timeoutSec": timeout}
        if cwd:
            body["cwd"] = cwd          # heredoc+cwd 的服务端 bug 已于 2026-11-10 修复
        if shell:
            body["shell"] = shell
        j = self._request("POST", "/api/agent/ssh/exec", body, timeout=timeout + 30)
        res = ExecResult(command=command, exit=j.get("exit"), out=j.get("out", ""),
                         err=j.get("err", ""), timed_out=bool(j.get("timedOut")),
                         truncated=bool(j.get("outTruncated")),
                         duration_ms=j.get("durationMs", 0))
        return res.check() if check else res

    def ls(self, host: str, path: str) -> list[Entry]:
        j = self._request("POST", "/api/agent/sftp/list", {"host": host, "path": path})
        ents = [Entry(e["name"], e.get("path", path.rstrip("/") + "/" + e["name"]),
                      e.get("size", 0), e.get("mtime", 0), bool(e.get("dir")))
                for e in j["entries"]]
        return sorted(ents, key=lambda e: (not e.dir, e.name))

    CHUNK = 4 * 1024 * 1024

    def read(self, host: str, path: str, offset: int | None = None,
             length: int | None = None) -> bytes:
        """整读；超过 8MB（413）时自动按 offset/length 分块拼接。"""
        if offset is not None or length is not None:
            body = {"host": host, "path": path, "offset": offset or 0}
            if length is not None:
                body["length"] = length
            return base64.b64decode(self._request("POST", "/api/agent/sftp/get", body)["contentB64"])
        try:
            j = self._request("POST", "/api/agent/sftp/get", {"host": host, "path": path})
            return base64.b64decode(j["contentB64"])
        except DeskportError as e:
            if e.status != 413:
                raise
        parts, off = [], 0
        while True:
            j = self._request("POST", "/api/agent/sftp/get",
                              {"host": host, "path": path, "offset": off, "length": self.CHUNK})
            b = base64.b64decode(j["contentB64"])
            parts.append(b)
            off += len(b)
            if not b or off >= j.get("total", off):
                return b"".join(parts)

    def write(self, host: str, path: str, data: bytes | str) -> dict:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self._request("POST", "/api/agent/sftp/put",
                             {"host": host, "path": path,
                              "contentB64": base64.b64encode(data).decode(),
                              "sha256": hashlib.sha256(data).hexdigest()})


class Session:
    """持久 SSH 会话：cd / export / source 在多条命令间延续。用 with 自动关闭。"""

    def __init__(self, dp: Deskport, sid: str):
        self.dp, self.id = dp, sid

    def run(self, command: str, timeout: int = 30, combined: bool = False,
            full: bool = False) -> dict:
        """超时返回 timedOut=True、exit=None；上一条没跑完时服务端回 409 busy；
        会话里 exit 后 alive=False，之后 410。本机（local）会话只支持 bash。"""
        body = {"command": command, "timeoutSec": max(1, min(600, int(timeout)))}
        if combined:
            body["combined"] = True
        if full:
            body["full"] = True
        return self.dp._request("POST", f"/api/agent/ssh/sessions/{self.id}", body,
                                timeout=timeout + 30)

    def sh(self, command: str, **kw) -> str:
        j = self.run(command, **kw)
        if j.get("exit") != 0:
            raise RuntimeError(f"exit={j.get('exit')}: {command}\n{j.get('err') or j.get('out')}")
        return j.get("out", "")

    def close(self) -> None:
        try:
            self.dp._request("DELETE", f"/api/agent/ssh/sessions/{self.id}")
        except DeskportError as e:
            if e.status != 404:
                raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Task:
    """派出去的智能体任务。events() 增量拉事件，wait() 轮询到本轮结束。"""

    DONE = ("waiting", "failed", "closed", "cancelled")

    def __init__(self, dp: Deskport, tid: str):
        self.dp, self.id, self.seq = dp, tid, 0
        self.pending_tools: list[dict] = []   # 最近的工具调用，审批时用来看它要做什么

    def __repr__(self) -> str:
        return f"Task({self.id!r})"

    def info(self, events: bool = False) -> dict:
        """任务详情。服务端会把全部事件塞进详情（很长），默认剥掉；要事件用 events()。"""
        j = self.dp._request("GET", f"/api/tasks/{self.id}" + ("?events=1" if events else ""))
        j = j.get("task", j)
        if not events:
            j.pop("events", None)   # 旧版服务端默认内嵌事件，客户端剥掉
        return j

    def until(self, timeout: int = 600) -> dict | None:
        """服务端阻塞等待（R4-3）：等到本轮结束 / 需要人工审批 / 超时，返回精简结果
        {status, finalText, pending, stats, lastSeq, error}，并推进 self.seq。
        服务端不支持（旧版 404）时返回 None，调用方退回 wait()。"""
        try:
            j = self.dp._request("GET", f"/api/tasks/{self.id}/wait?after={self.seq}&timeout={int(timeout)}",
                                 timeout=int(timeout) + 30)
        except DeskportError as e:
            if e.status == 404:
                return None
            raise
        self.seq = j.get("lastSeq", self.seq)
        return j

    def events(self) -> list[dict]:
        # 实测参数名是 after（手册写的 since 会被忽略、每次返回全量）；再按 seq 去重兜底
        j = self.dp._request("GET", f"/api/tasks/{self.id}/events?after={self.seq}")
        evs = [e for e in j.get("events", []) if e.get("seq", 0) > self.seq]
        if evs:
            self.seq = max(e.get("seq", 0) for e in evs)
        return evs

    def wait(self, poll: float = 2.5, timeout: float = 1800, on_event=None,
             on_permission=None) -> dict:
        """轮询直到 status 为 waiting/failed/closed。遇到 permission_request：
        有 on_permission(event) 就用它的返回值 (optionId, deny) 审批，否则返回让调用方处理。"""
        import time
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events():
                if on_event:
                    on_event(e)
                if e.get("type") == "tool":
                    self.pending_tools.append(e)
                if e.get("type") == "permission_request":
                    if not on_permission:
                        return {"status": "permission_request", "event": e}
                    opt, deny = on_permission(e)
                    self.permission(e.get("requestId") or e.get("id"), opt, deny)
                    self.pending_tools.clear()
            info = self.info()
            if info.get("status") in self.DONE:
                return info
            time.sleep(poll)
        raise TimeoutError(f"task {self.id} 超过 {timeout}s 未结束")

    def compact(self) -> dict:
        """压缩执行方会话上下文（Kimi 的 /compact，后台进行，完成后生效）。适合一批活结束、开始下一批前调用。"""
        return self.message("/compact")

    def message(self, text: str) -> dict:
        return self.dp._request("POST", f"/api/tasks/{self.id}/message", {"text": text})

    def permission(self, request_id: str, option_id: str, deny: bool = False) -> dict:
        return self.dp._request("POST", f"/api/tasks/{self.id}/permission",
                                {"requestId": request_id, "optionId": option_id, "deny": deny})

    def cancel(self) -> dict:
        return self.dp._request("POST", f"/api/tasks/{self.id}/cancel", {})

    def close(self) -> dict:
        return self.dp._request("POST", f"/api/tasks/{self.id}/close", {})


class Workline:
    """工作线：主控只对工作线说话，Deskport 决定投递到当前任务 / 排队 / 开新任务。"""

    def __init__(self, dp: Deskport, wid: str):
        self.dp, self.id = dp, wid

    def __repr__(self) -> str:
        return f"Workline({self.id!r})"

    def info(self) -> dict:
        """{workline:{id,title,doc,rev,currentTask,context,nextAction,...}}"""
        j = self.dp._request("GET", f"/api/worklines/{self.id}")
        return j.get("workline", j)

    def send(self, text: str, **task_opts) -> tuple[str, "Task"]:
        """发一句话：返回 (routed, Task)。routed ∈ new/message/queued。task_opts 用于新开任务时（node、cwd、mode、model…）。"""
        body = {"prompt": text, **task_opts}
        j = self.dp._request("POST", f"/api/worklines/{self.id}/tasks", body, timeout=150)
        tid = (j.get("task") or {}).get("id") or j.get("taskId")
        return j.get("routed", ""), Task(self.dp, tid)

    def update_doc(self, doc: str, base_rev: str) -> dict:
        """主控改接力棒；base_rev 不匹配（执行方刚回写过）返回 409。"""
        return self.dp._request("PUT", f"/api/worklines/{self.id}", {"doc": doc, "baseRev": base_rev})

    def rotate(self, text: str, **task_opts) -> "Task":
        """上下文太大时换新会话：新任务自动带最新接力棒，旧任务 close。"""
        j = self.dp._request("POST", f"/api/worklines/{self.id}/rotate", {"prompt": text, **task_opts}, timeout=150)
        return Task(self.dp, (j.get("task") or {}).get("id") or j.get("taskId"))

    def delete(self) -> dict:
        return self.dp._request("DELETE", f"/api/worklines/{self.id}")


class Host:
    """绑定到某台主机（可选绑定工作目录）的快捷对象。"""

    def __init__(self, dp: Deskport, name: str, cwd: str | None = None,
                 shell: str | None = None):
        self.dp, self.name, self.cwd, self.shell = dp, name, cwd, shell

    def __repr__(self) -> str:
        return f"Host({self.name!r}" + (f", cwd={self.cwd!r})" if self.cwd else ")")

    def cd(self, path: str) -> "Host":
        """返回绑定了工作目录的新 Host（相对路径基于当前 cwd）。"""
        return Host(self.dp, self.name, self.path(path), self.shell)

    def using(self, shell: str | None) -> "Host":
        """返回改用指定 shell 的新 Host（仅 local：bash/powershell/pwsh）。"""
        return Host(self.dp, self.name, self.cwd, shell)

    def path(self, p: str) -> str:
        if p:
            p = p.replace("\\", "/")       # Windows 路径统一成正斜杠
        if (p and re.match(r"^[A-Za-z]:/", p)) or posixpath.isabs(p) or not self.cwd:
            return posixpath.normpath(p) if p else p
        return posixpath.normpath(posixpath.join(self.cwd, p))

    def run(self, command: str, timeout: int = 30, check: bool = False,
            cwd: str | None = None) -> ExecResult:
        cwd = self.path(cwd) if cwd else self.cwd
        return self.dp.run(self.name, command, timeout=timeout, check=check, cwd=cwd,
                           shell=self.shell)

    def session(self, cwd: str | None = None) -> Session:
        """持久会话，默认从当前绑定目录开始。"""
        return self.dp.session(self.name, self.path(cwd) if cwd else self.cwd)

    def sh(self, command: str, **kw) -> str:
        """执行并返回 stdout；失败抛 CommandError，输出被截断时告警。"""
        r = self.run(command, check=True, **kw)
        if r.truncated:
            warnings.warn(f"输出超过服务端上限（2 MiB）被截断：{command[:80]}", stacklevel=2)
        return r.out

    def ls(self, path: str = ".") -> list[Entry]:
        return self.dp.ls(self.name, self.path(path))

    def read(self, path: str, offset: int | None = None, length: int | None = None) -> bytes:
        return self.dp.read(self.name, self.path(path), offset, length)

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding, "replace")

    def write(self, path: str, data: bytes | str) -> dict:
        return self.dp.write(self.name, self.path(path), data)


def _load_config() -> dict:
    cfg: dict = {}
    f = _HERE / ".deskport.json"
    if f.exists():
        cfg.update(json.loads(f.read_text(encoding="utf-8")))
    for key in ("url", "host"):
        if os.environ.get(f"DESKPORT_{key.upper()}"):
            cfg[key] = os.environ[f"DESKPORT_{key.upper()}"]
    if os.environ.get("DESKPORT_INSECURE"):
        cfg["insecure"] = os.environ["DESKPORT_INSECURE"] not in ("0", "", "false")
    return cfg


def connect(host: str | None = None, url: str | None = None,
            insecure: bool | None = None, cwd: str | None = None, **kw) -> Host:
    """按配置一步拿到 Host。"""
    cfg = _load_config()
    url = url or cfg.get("url")
    host = host or cfg.get("host")
    if insecure is None:
        insecure = bool(cfg.get("insecure", False))
    if not host:
        raise ValueError("需要 host（参数、DESKPORT_HOST 或 .deskport.json）")
    h = Deskport(url, insecure=insecure, cafile=kw.pop("cafile", cfg.get("cafile")),
                 pin=kw.pop("pin", bool(cfg.get("pin"))), **kw).host(host)
    return h.cd(cwd) if cwd else h


if __name__ == "__main__":
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="Deskport Agent API CLI（默认读 .deskport.json）")
    ap.add_argument("--url")
    ap.add_argument("--host")
    ap.add_argument("-k", "--insecure", action="store_true", default=None)
    ap.add_argument("-C", "--cwd", help="远端工作目录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hosts")
    p = sub.add_parser("run"); p.add_argument("command"); p.add_argument("-t", "--timeout", type=int, default=30)
    p = sub.add_parser("ls"); p.add_argument("path", nargs="?", default=".")
    p = sub.add_parser("cat"); p.add_argument("path")
    a = ap.parse_args()

    def _unmangle(p):
        """Git Bash(MSYS) 会把 /mnt/x 改写成 C:/Program Files/Git/mnt/x，这里还原成远端路径。"""
        if not p or not (len(p) > 2 and p[1] == ":" and p[2] in "/\\"):
            return p
        import shutil
        roots = {os.environ.get("MSYS_ROOT", "")}
        bash = shutil.which("bash")
        if bash:  # ...\Git\usr\bin\bash.exe 或 ...\Git\bin\bash.exe
            b = Path(bash).resolve()
            roots |= {str(b.parents[1]), str(b.parents[2])}
        norm = p.replace("\\", "/")
        for r in sorted(filter(None, roots), key=len, reverse=True):
            r = r.replace("\\", "/").rstrip("/")
            if norm.lower().startswith(r.lower() + "/"):
                return norm[len(r):]
        return p

    a.cwd = _unmangle(a.cwd)
    if hasattr(a, "path"):
        a.path = _unmangle(a.path)

    try:
        if a.cmd == "hosts":
            cfg = _load_config()
            dp = Deskport(a.url or cfg.get("url"),
                          insecure=a.insecure if a.insecure is not None else bool(cfg.get("insecure")))
            for h in dp.hosts():
                print(f"{h['name']}\t{h['host']}:{h['port']}\t{h['id']}")
            sys.exit(0)
        h = connect(a.host, a.url, a.insecure, a.cwd)
        if a.cmd == "run":
            r = h.run(a.command, timeout=a.timeout)
            sys.stdout.write(r.out); sys.stderr.write(r.err)
            if r.truncated:
                print("\n[deskport] 输出被截断（2 MiB 上限）", file=sys.stderr)
            sys.exit(r.exit if r.exit is not None else 124)
        elif a.cmd == "ls":
            for e in h.ls(a.path):
                print(f"{'d' if e.dir else '-'} {e.size:>10} {e.name}")
        elif a.cmd == "cat":
            sys.stdout.flush(); sys.stdout.buffer.write(h.read(a.path))
    except (DeskportError, ValueError, OSError, http.client.HTTPException) as e:
        print(f"deskport: {e}", file=sys.stderr); sys.exit(2)
