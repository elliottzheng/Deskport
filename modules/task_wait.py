# -*- coding: utf-8 -*-
"""任务阻塞等待：R4-3 阻塞响应 + R4-12 断线续接的精炼快照。

从 tasks_api 拆出（结构门禁：tasks_api 是存量例外，只许变短）。本模块只
读任务/事件并等唤醒，不写任务状态；路由仍在 tasks_api。

阻塞实现：app.run(threaded=True)，每个等待请求占一个工作线程，
线程在 repo.evCond 上整体放锁休眠（事件驱动唤醒，无忙等）；
单用户场景等待方是个位数，不构成线程池压力。
"""
import json
import os
import time

from modules import tasks_store as store

WAIT_ENDERS = ('waiting', 'failed', 'closed', 'cancelled')


def _parseWaitSeconds(value, cap=600):
    """"wait": <秒> → 1..cap 的整数或 None（不等待）。"""
    if value in (None, '', False):
        return None
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(secs, cap))


def _lastAssistantAfter(task, after):
    """after 之后最后一条 assistant 的全文——事件卡里 text 截到 MAX_TEXT，
    全文只在 jsonl 流水里，读文件拿完整版。"""
    text = ''
    path = store.repo.log_path(task)
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get('seq', 0) > after and obj.get('type') == 'assistant' \
                            and obj.get('text'):
                        text = obj['text']
        except OSError:
            pass
    if not text:
        with store.repo.lock:
            for e in reversed(task.get('events', [])):
                if e.get('seq', 0) > after and e.get('type') == 'assistant' \
                        and e.get('text'):
                    text = e['text']
                    break
    return text


def _waitSnapshot(task, after, ignorePending=None):
    """wait 响应的精炼快照：不带事件全集，只带调用方决策所需。"""
    with store.repo.lock:
        events = [e for e in task.get('events', []) if e.get('seq', 0) > after]
        stats = {'events': len(events),
                 'tools': sum(1 for e in events if e.get('type') == 'tool'),
                 'autoApproved': sum(1 for e in events
                                     if e.get('type') == 'auto_approved'),
                 'errors': sum(1 for e in events if e.get('type') == 'error')}
        status = task.get('status') or 'queued'
        pendingPerm = task.get('pendingPermission') or ''
        agent = store.repo.agentOf(task['id'])
        perm = (agent.get('perm') or {}) if agent else {}
        pending = None
        if pendingPerm and pendingPerm != ignorePending:
            if perm.get('requestId') == pendingPerm:
                pending = {'requestId': pendingPerm,
                           'callId': perm.get('callId') or '',
                           'name': perm.get('title') or '',
                           'args': perm.get('args'),
                           'options': perm.get('options') or []}
            else:
                # perm 记账与 pendingPermission 标记不一致（fork/接手链路上
                # 曾实测漏报：标记在、perm 没了 → wait 空等到超时）。规格 R4-3b
                # #1：只要标记是 live 的就如实回报 pending，内容从最近一条
                # 同 id 的 permission_request 事件重建。只认 liveFrom 水位线
                # 之后的事件（R4-5a #3）：回放窗口的旧审批不是 live 的
                liveFrom = task.get('liveFrom', 0)
                lastReq = None
                for e in reversed(task.get('events', [])):
                    if e.get('seq', 0) < liveFrom:
                        break
                    if e.get('type') == 'permission_request' \
                            and str(e.get('requestId') or '') == pendingPerm:
                        lastReq = e
                        break
                if lastReq:
                    pending = {'requestId': pendingPerm,
                               'callId': lastReq.get('callId') or '',
                               'name': lastReq.get('name') or lastReq.get('title') or '',
                               'args': lastReq.get('args') or '',
                               'options': lastReq.get('options') or []}
        lastSeq = task.get('seq', 0)
        error = task.get('error', '')
    # taskId 必须在所有 wait 响应里（R4 复测缺陷：调用方拿不到自己刚建的任务）
    return {'taskId': task.get('id') or '',
            'status': 'permission' if pending else status,
            'finalText': _lastAssistantAfter(task, after),
            'pending': pending, 'stats': stats,
            'lastSeq': lastSeq, 'error': error}


def _blockWait(task, after, timeout, ignorePendingRequestId=None):
    """阻塞到「需要调用方决策」或回合结束；超时回当前态（queued/running
    统一映射 running，调用方带新 after 再来）。ignorePendingRequestId：
    刚被自己批掉的授权不算决策点（审批 wait 用）。"""
    deadline = time.monotonic() + max(1, min(int(timeout or 300), 600))
    snap = _waitSnapshot(task, after, ignorePendingRequestId)
    while not (snap['pending'] or snap['status'] in WAIT_ENDERS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if snap['status'] in ('queued', 'running'):
                snap['status'] = 'running'
            return snap
        with store.repo.lock:
            store.repo.evCond.wait(remaining)   # 放锁休眠，事件到达即醒
        snap = _waitSnapshot(task, after, ignorePendingRequestId)
    return snap
