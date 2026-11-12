# -*- coding: utf-8 -*-
"""跨层 JSON 契约（TypedDict）：只描述键名，不改变序列化。

运行时仍是普通 dict；作用是把前端/日志/WS 依赖的字段集中到一处，
避免 summary/events/hello 三套形状各写各的。
"""
from typing import Literal, Protocol, TypedDict


class AcpClient(Protocol):
    """智能体会话客户端契约。

    两个实现共同满足：KimiAcp（web 进程内 stdio 托管，回退路径）与
    RemoteAcpClient（HTTP → agent node，本机/远端统一路径）。
    方法签名靠本 Protocol 与 tests/test_acp_parity.py 的对拍测试约束，
    新增方法必须两边同步，否则对拍测试或静态检查报警。
    """
    stderr_lines: list[str]

    def start(self, session_id: str | None = None) -> str: ...
    def prompt(self, text: str, timeout: float | None = None) -> dict: ...
    def set_mode(self, mode: str) -> None: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...
    def poll(self) -> int: ...


class TaskSummary(TypedDict):
    id: str
    title: str
    prompt: str
    status: str
    createdAt: float
    owner: str
    createdLabel: str
    startedAt: float
    endedAt: float
    workspace: str
    cwd: str
    engine: str
    lastText: str
    resultFiles: list[dict]
    error: str
    sessionId: str
    mode: str
    model: str
    modelEffective: str
    kimiSessionId: str
    kimiSessionFork: bool
    policy: dict
    node: str
    remoteCwd: str
    workline: dict
    resume: bool
    canContinue: bool
    canResume: bool
    awaitingAnswer: bool
    pendingPermission: str
    lastActiveAt: float
    lastActiveLabel: str


class TaskEvent(TypedDict, total=False):
    """任务卡事件。

    事件为不可变追加（append-only，seq 单调递增）：
    assistant 事件在回合结束时以单条完整文本落卡（tasks_runner._flushAssistant），
    流式 chunk 只进 log（/log 端点），不进入本事件流——
    因此按 seq 去重的增量订阅者（轮询/SSE）拿到的 assistant 文本总是完整终版。
    """
    seq: int
    at: float
    type: str
    text: str
    name: str
    args: str
    callId: str
    status: str
    options: list[dict]
    truncated: bool


class HelloFrame(TypedDict):
    type: Literal['hello']
    session: str
    resumed: bool
    label: str
    kind: str
    cwd: str | None
    cwdLabel: str
    foreground: str
    foregroundCwd: str
    foregroundCwdLabel: str
    proto: int
    caps: list[str]


class ErrorFrame(TypedDict):
    type: Literal['error']
    msg: str


class ExitFrame(TypedDict):
    type: Literal['exit']
    code: int | None


class PreemptedFrame(TypedDict):
    type: Literal['preempted']


class ReplayEndFrame(TypedDict):
    type: Literal['replay-end']


class PongFrame(TypedDict):
    type: Literal['pong']


TermFrame = (HelloFrame | ErrorFrame | ExitFrame | PreemptedFrame
             | ReplayEndFrame | PongFrame)
