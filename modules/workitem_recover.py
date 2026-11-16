# -*- coding: utf-8 -*-
"""重启恢复（批次 G2）：中枢重启后工作项状态与"订阅"恢复，不重复建任务。

幂等键 = tasks_store 里的任务台账：工作项只记指针（implTask /
verifyTasks），重启后按 workItemId 从台账重建——崩在建任务与落指针
之间也不会重复建（台账里已有就不建）。集成作业线程随重启消失：仍在
integrating 的工作项转 needs_judgment（主管可 continue=重新集成）。

recover() 在 app 装配期跑一次（不 import Flask；watch 的循环随后接上）。
"""
import logging

from modules import tasks_store as store
from modules import workitem_state, workitem_store
from modules import workitem_core as core

log = logging.getLogger('workitem-recover')


def recover():
    """启动恢复：重建指针、处理重启残留。返回处理摘要（测试断言用）。"""
    workitem_store.repo.load()
    adopted = 0
    judged = 0
    for wi in workitem_store.repo.list():
        if _adoptTasks(wi):
            adopted += 1
        if (wi.get('state') or '') == workitem_state.INTEGRATING:
            # 集成作业线程没了：合并可能已提交也可能没有——不猜，交主管
            if core._toJudgment(wi, '服务重启时集成作业在飞，请主管判断'
                                    '（continue=重新集成，主仓工作树请先核对）'):
                judged += 1
    if adopted or judged:
        workitem_store.repo.save()
    log.info('workitem recover: adopted=%d judgment=%d', adopted, judged)
    return {'adopted': adopted, 'judged': judged}


def _adoptTasks(wi):
    """按台账重建 implTask/verifyTasks 指针（缺的补，坏的清）。"""
    store.repo.load()
    mine = [t for t in store.repo.list()
            if (t.get('workItemId') or '') == (wi.get('id') or '')]
    impls = sorted((t for t in mine if (t.get('expects') or '') == 'impl'),
                   key=lambda t: t.get('createdAt') or 0)
    verifies = sorted((t for t in mine if (t.get('expects') or '') == 'verify'),
                      key=lambda t: t.get('createdAt') or 0)
    changed = False
    wantImpl = impls[-1]['id'] if impls else ''
    if (wi.get('implTask') or '') != wantImpl:
        wi['implTask'] = wantImpl
        changed = True
    wantVerify = [t['id'] for t in verifies]
    if list(wi.get('verifyTasks') or []) != wantVerify:
        wi['verifyTasks'] = wantVerify
        changed = True
    for task in verifies:
        wi.setdefault('verifyMeta', {}).setdefault(
            task['id'], {'target': (task.get('result') or {}).get('target')
                         or '', 'base': '', 'attempt': 0})
    if changed:
        core._save(wi)
    return changed
