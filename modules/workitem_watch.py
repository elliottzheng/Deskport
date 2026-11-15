# -*- coding: utf-8 -*-
"""流水线巡检（批次 G2）：任务起不来 / 执行卡死 / 提前收工。

单 daemon 2s 一跳（bindApp 注入后自起，先例 task_health/leads_inbox）；
不 import Flask——动作全走 workitem_pipeline.tick 与 workitem_actions
的公开面。所有判定都从任务卡实况推导（易失标记只在内存），重启后第一跳
照常生效。

三类异常（规格 §2，全部来自 2026-11-15 主控实遇）：
- 起不来：会话创建失败（任务没有 sessionId 就终态）→ 退避 10s/60s 重试
  两次 → 仍不行 needs_judgment；
- 卡死：running 超 10 分钟无任何事件且无工具在跑 → 先 cancel，30s 回不到
  可发消息状态就 close + kimiSessionId 接回原会话重发未完成指令；
- 提前收工：一轮结束但 0 工具 0 文本 / 本轮有会话自带后台任务 / 会话进程
  中途退出 → 前两种追发固定文案，接回后追发；连续 2 次 → 换项目备选模型
  新开会话接棒（附上一位临时文件路径）；仍不行 needs_judgment。
（执行结束无结果在 pipeline 的 tick 里判——G1 的 resultNudged 是信号。）
"""
import logging
import re
import threading
import time

from modules import tasks_store as store   # noqa: F401（探针/测试引用）
from modules import workitem_prompts, workitem_state, workitem_store
from modules import workitem_core as core
from modules import workitem_pipeline, workitem_actions

log = logging.getLogger('workitem-watch')

SWEEP_SEC = 2.0
START_RETRY_SEC = (10, 60)      # 起不来的退避（规格 §2）
STALL_SEC = 600                 # 10 分钟无任何事件且无工具在跑
STALL_SETTLE_SEC = 30            # cancel 后等它回到可发消息状态的时长
EARLY_FINISH_LIMIT = 2           # 连续几次仍提前收工 → 换备选模型
BACKGROUND_RE = re.compile(r'"run_in_background"\s*:\s*true', re.I)
TURN_WINDOW = 400               # 提前收工判定看最近多少条事件
_started = False
_enabled = True                 # 巡检开关：测试置 False 让后台那跳空转
                                 # （app.py 模块级 create_app 会在收集期起线程，
                                 #  改 _loop 拦不住已经开跑的线程；用例仍可直调 sweep）


def bindApp(app):
    """create_app 注入后拉起巡检线程（core 收下 app，供建任务的上下文用）。"""
    global _started
    core.bindApp(app)
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True,
                         name='workitem-watch').start()


def _loop():
    while True:
        try:
            if _enabled:
                sweep(time.time())
        except Exception as e:      # 巡检失败不能带走线程，下一跳重试
            log.warning('workitem watch sweep failed: %s', e)
        time.sleep(SWEEP_SEC)


def sweep(now=None):
    """扫一跳（测试可直接调，不起线程）：异常检测 + 流水线 tick。"""
    now = now if now is not None else time.time()
    for wi in workitem_store.repo.list():
        try:
            with core._lock:
                _sweepOne(wi, now)
        except Exception as e:
            log.warning('workitem watch %s failed: %s', wi.get('id'), e)
    workitem_pipeline.tick()


def _sweepOne(wi, now):
    state = wi.get('state') or ''
    if state not in (workitem_state.IMPLEMENTING, workitem_state.VERIFYING):
        return
    verifying = state == workitem_state.VERIFYING
    task = core._verifyTask(wi) if verifying else core._implTask(wi)
    if task is None:
        return                      # 任务不见了：pipeline 的 tick 转判断
    if core._ended(task):
        _endedWithoutResult(wi, task, verifying)
        return
    if task.get('startedAt') and wi.get('startFails'):
        wi['startFails'] = 0        # 会话真起来过：起不来计数清零
        core._save(wi)
    if task.get('_wiStallAt'):
        _stallSettle(wi, task, verifying)
        return
    if task.get('status') == 'running':
        _checkStall(wi, task, now, verifying)
        return
    if task.get('status') == 'waiting' and not task.get('result'):
        _checkEarlyFinish(wi, task, verifying)


# ---------------------------------------------------------------- 起不来

def _endedWithoutResult(wi, task, verifying):
    """任务终态且没有结果：起不来（没起过会话）/ 进程中途退出 / 人关的。"""
    if task.get('result'):
        return                      # 有结果：等 pipeline 的 tick 处理
    if task.get('status') not in ('failed', 'interrupted'):
        # closed / cancelled：不擅自接回（收尾语义：人关的），交主管判断
        core._toJudgment(wi, '%s任务已%s且未交付，请主管判断'
                         % ('验收' if verifying else '实施',
                            task.get('status')))
        return
    if not task.get('sessionId'):
        _startFailed(wi, task, verifying)
        return
    # 会话进程中途退出（"ACP 进程已退出"）：接回原会话重发未完成指令
    instruction = _lastUserMessage(task)
    _noteIncident(wi)               # ③也计入连续事故：连续 2 次换备选模型
    if _escalate(wi, task):
        if verifying:
            core._toJudgment(wi, '验收会话连续中途退出，请主管判断')
        else:
            workitem_actions.switchModel(wi)
        return
    if verifying:
        workitem_actions._revive(wi, task, instruction, '会话进程中途退出',
                                 kind='verify')
    else:
        workitem_actions._revive(wi, task, instruction, '会话进程中途退出')


def _startFailed(wi, task, verifying):
    """会话创建失败：退避 10s / 60s 重试两次，仍失败 → needs_judgment。"""
    if verifying:
        core._toJudgment(wi, '验收会话没起来（%s），请主管判断'
                         % (task.get('error') or '')[:120])
        return
    fails = int(wi.get('startFails') or 0)
    if fails >= len(START_RETRY_SEC):
        core._toJudgment(wi, '实施会话连续 %d 次没起来：%s'
                         % (fails, (task.get('error') or '')[:160]))
        return
    wait = START_RETRY_SEC[fails]
    last = float(wi.get('startFailAt') or 0)
    if last and time.time() - last < wait:
        return                      # 退避窗口内：等下一跳
    wi['startFails'] = fails + 1
    wi['startFailAt'] = time.time()
    core._save(wi)
    workitem_actions.retryStart(wi)


# ---------------------------------------------------------------- 卡死

def _checkStall(wi, task, now, verifying):
    """running 超 10 分钟无任何事件且无工具在跑 → cancel（一次一标记）。"""
    lastActive = float(task.get('lastActiveAt') or 0)
    if now - lastActive < STALL_SEC or task.get('_currentTool'):
        return
    task['_wiStallAt'] = now         # 内存标记（不落盘）：这一轮只处理一次
    wi['stalls'] = int(wi.get('stalls') or 0) + 1
    core._save(wi)
    log.info('workitem %s: task %s 疑似卡死（%ds 无事件）',
             wi.get('id'), task.get('id'), int(now - lastActive))
    _cancelTurn(task)
    _noteIncident(wi)


def _stallSettle(wi, task, verifying):
    """cancel 之后：30s 内回到可发消息状态就重发未完成指令；仍卡着就
    close + 接回；接回失败 → needs_judgment。"""
    if time.time() - float(task.get('_wiStallAt') or 0) < STALL_SETTLE_SEC:
        return
    task['_wiStallAt'] = 0
    status = task.get('status') or ''
    instruction = _lastUserMessage(task)
    resend = '【平台重发】上一轮疑似卡死已取消。%s' % instruction
    if status in ('waiting', 'paused'):
        workitem_actions._sendToTask(task, wi, resend, '卡死恢复',
                                     kind='verify' if verifying else 'impl')
        return
    if verifying:
        core._toJudgment(wi, '验收任务卡死（cancel 后 %s），请主管判断' % status)
        return
    workitem_actions._revive(wi, task, instruction, '卡死 cancel 无效')


def _cancelTurn(task):
    """停当前这一轮（与 /cancel 同链路：拒挂起审批 + client.cancel）。"""
    try:
        from modules import tasks_api
        payload, _code = tasks_api.cancelTask(task.get('id') or '')
        return payload
    except Exception as e:
        log.info('cancel %s failed: %s', task.get('id'), e)
        return None


# ---------------------------------------------------------------- 提前收工

def _noteIncident(wi):
    """记一次"这个执行出过状况"：上下文换手阈值从 50% 降到 30%。"""
    wi['earlyFinish'] = int(wi.get('earlyFinish') or 0) + 1
    wi['earlyStreak'] = int(wi.get('earlyStreak') or 0) + 1
    core._save(wi)


def _escalate(wi, task):
    """连续事故达到上限 → 该换模型了（换手计数由调用方决定去向）。"""
    return int(wi.get('earlyStreak') or 0) >= EARLY_FINISH_LIMIT


def _checkEarlyFinish(wi, task, verifying):
    """轮末判定：0 工具 0 文本 / 本轮有会话自带后台任务 → 追发固定文案；
    连续 2 次 → 换项目备选模型新开会话接棒。正常轮清零连续计数。"""
    seq = int(task.get('seq') or 0)
    checked = int((wi.get('earlyCheckedAt') or {}).get(task.get('id') or '') or 0)
    if seq <= checked:
        return                      # 这一轮已经判过（追发也会抬 seq）
    wi.setdefault('earlyCheckedAt', {})[task.get('id') or ''] = seq
    turn = task.get('lastTurn') or {}
    if not (turn.get('empty') or _sawBackground(task)):
        if wi.get('earlyStreak'):
            wi['earlyStreak'] = 0   # 正常轮：连续计数清零
            core._save(wi)
        return
    _noteIncident(wi)
    if _escalate(wi, task):
        if verifying:
            core._toJudgment(wi, '验收方连续提前收工，请主管判断')
        else:
            workitem_actions.switchModel(wi)
        return
    core._save(wi)
    workitem_actions._sendToTask(task, wi, workitem_prompts.NUDGE_TEXT,
                                 '提前收工追发',
                                 kind='verify' if verifying else 'impl')


def _lastUserMessage(task):
    for event in reversed(task.get('events') or []):
        if event.get('type') == 'user' and (event.get('text') or '').strip():
            return event['text'].strip()[:2000]
    return ''


def _sawBackground(task):
    """本轮是否有工具调用带 run_in_background: true（会话自带后台任务）。

    看最近 TURN_WINDOW 条事件里的 tool 事件——参数是落卡的 JSON 文本
    （流式残片也可能带这个键，故用正则而非 parse）。
    """
    seq0 = max(int(task.get('seq') or 0) - TURN_WINDOW, 0)
    for event in task.get('events') or []:
        if event.get('type') != 'tool' \
                or int(event.get('seq') or 0) <= seq0:
            continue
        if BACKGROUND_RE.search(str(event.get('args') or '')):
            return True
    return False
