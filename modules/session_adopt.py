# -*- coding: utf-8 -*-
"""会话接回（批次 DR）：kimiSessionId 接手遇 "in use" 的善后。

节点侧残留进程（任务已关闭、但 node 的 kimi 还持锁）会让接手立刻失败：
"This session is in use by another Kimi Code CLI instance"——fork 副本与
直接接管都认这把锁。这里按节点已有的结束会话接口（DELETE /api/sessions）
清场后重试一次；仍有未关闭任务在用时不杀，错误里点名是谁。
"""
import time

from modules import tasks_store as store

KILL_WAIT_SEC = 2.0      # 进程终止后等锁释放的秒数（kimi 锁随进程死亡释放）
UNCLOSED = ('closed', 'cancelled')   # 这两个状态不算占用


def holdersOf(kimiSessionId, excludeTaskId='', includeClosed=False):
    """还在占用该 kimi 会话的任务（默认 close/cancelled 不算占用）。

    excludeTaskId：接回任务自己的卡——start() 重试路径上任务卡已建，
    不排除会把自己当成占用者，永远接不回来。
    includeClosed：DR-S2 重试仍失败时点名用——节点侧进程可能没随任务
    关闭而退出，已关闭/已取消的任务也算占用者，一并列出来。
    """
    wanted = str(kimiSessionId or '').strip()
    if not wanted:
        return []
    store.repo.load()
    with store.repo.lock:
        out = []
        for t in store.repo.list():
            if t.get('id') == excludeTaskId:
                continue
            if not includeClosed and t.get('status') in UNCLOSED:
                continue
            if wanted in ((t.get('sessionId') or ''),
                          (t.get('kimiSessionId') or '')):
                out.append({'id': t.get('id') or '',
                            'title': t.get('title') or '',
                            'status': t.get('status') or '',
                            'node': t.get('node') or ''})
        return out


def _holdersError(kimiSessionId, holders):
    who = '、'.join('%s（%s，%s）' % (h['id'][:8], h['title'] or '无标题',
                                     h['status']) for h in holders[:3])
    if len(holders) > 3:
        who += ' 等 %d 个' % len(holders)
    return RuntimeError(
        '会话 %s 正被未关闭任务占用：%s——先关闭占用任务，或改用 fork 副本接手'
        % (kimiSessionId, who))


def _retryHoldersError(kimiSessionId, excludeTaskId, original):
    """DR-S2：清场后重试仍 in use——点名占用者（closed/cancelled 也算，
    节点侧进程可能没退），不再只抛节点原文。"""
    holders = holdersOf(kimiSessionId, excludeTaskId, includeClosed=True)
    who = '、'.join('%s（%s，%s）' % (h['id'], h['title'] or '无标题',
                                     h['status']) for h in holders[:3])
    if len(holders) > 3:
        who += ' 等 %d 个' % len(holders)
    if holders:
        return RuntimeError(
            '会话 %s 清场后重试仍 in use：节点侧进程未退出（占用任务：%s）——'
            '稍后重试，或先关闭这些任务' % (kimiSessionId, who))
    return RuntimeError(
        '会话 %s 清场后重试仍 in use，且没有任务占用记录（节点侧残留进程，'
        '原文：%s）——稍后重试' % (kimiSessionId, original))


def ensureSessionFree(nodeName, kimiSessionId, excludeTaskId='', waitSec=None):
    """接回前清场：有未关闭任务在用 → 抛错（不强杀）；没有 → 经节点结束该
    会话的残留进程，等锁释放后返回，供调用方重试。"""
    holders = holdersOf(kimiSessionId, excludeTaskId)
    if holders:
        raise _holdersError(kimiSessionId, holders)
    from modules.agent_nodes import shutdownNodeSession
    shutdownNodeSession(nodeName or 'local', kimiSessionId)
    if waitSec is None:
        waitSec = KILL_WAIT_SEC
    if waitSec > 0:
        time.sleep(waitSec)


def startSession(client, task):
    """会话线程的 client.start() 包装：遇节点 "in use"（残留持锁进程）→
    按未关闭占用判定清场（经节点结束会话）后重试一次；仍失败 → 抛错点名
    占用者（DR-S2：含已关闭/已取消，节点进程可能没退）。
    非接回场景（无 kimiSessionId）原样放行。"""
    try:
        return client.start(session_id=task.get('sessionId') or None)
    except Exception as e:
        if 'in use' not in str(e) or not getattr(client, 'kimi_session_id', ''):
            raise
        kimiSessionId = getattr(client, 'kimi_session_id', '')
        ensureSessionFree(task.get('node') or 'local', kimiSessionId,
                          excludeTaskId=task.get('id') or '')
        try:
            return client.start(session_id=task.get('sessionId') or None)
        except Exception as retry:
            if 'in use' not in str(retry):
                raise
            raise _retryHoldersError(kimiSessionId, task.get('id') or '',
                                     retry)
