# -*- coding: utf-8 -*-
"""跨层 JSON 契约（TypedDict）：只描述键名，不改变序列化。

运行时仍是普通 dict；作用是把前端/日志/WS 依赖的字段集中到一处，
避免 summary/events/hello 三套形状各写各的。
"""
from typing import Literal, TypedDict


class TaskSummary(TypedDict):
    id: str
    title: str
    prompt: str
    status: str
    createdAt: float
    createdLabel: str
    startedAt: float
    endedAt: float
    workspace: str
    engine: str
    lastText: str
    resultFiles: list[dict]
    error: str
    mode: str
    resume: bool
    canContinue: bool
    canResume: bool
    awaitingAnswer: bool
    pendingPermission: str
    lastActiveAt: float
    lastActiveLabel: str


class TaskEvent(TypedDict, total=False):
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
