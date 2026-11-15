# -*- coding: utf-8 -*-
"""主管侧的系统提醒（批次 L）：提问直达收件箱（L2）+ 反应超时提醒（L1）。

L2（下属提问直达主管）：下属任务的 permission_request 若是 AskUserQuestion
（工具名或参数形如 {questions:[…]}），由 tasks_runner 在挂起前调
emitQuestion——作为 kind='question' 写进主管收件箱（问题文本 + 选项 label
列表 + requestId 引用），并立即唤醒托管主管（不等合并窗口，与 human 条目
同款旁路）。答复走 POST /api/tasks/<id>/answer（tasks_runner.answerPermission）。

L1（主管反应超时提醒）：收件箱里下属消息超过 leadResponseSec 既没投递
（inboxSeq）也没被外部主管读（readSeq）→ 往该主管 down 方向写一条 notice，
指挥台「等你处理」因此可见；托管主管另有第二条分支——条目已投递但任务
waiting、收件箱唤醒之后没有任何新事件（主管根本没反应），同样提醒。同一
主管每阈值最多一条（leads_store.responseNoticedAt 节流）。已落定的
escalation（审批出过结果，leads_inbox.entryResolved）不算待处理——否则
驱动几秒批掉的审批会在收件箱里堆成"主管好久没处理 50 多条"（主控追加）。

时钟可注入：responseNotice(lead, now) 的 now 由 sweeper 传入（测试据此造
超时），缺省走墙上时钟。

N1（一事一条 / 条件解除即收回）：down 侧用户可见提醒不再堆屏。同一去重族
（noticeFamily：ref 带 lead = L1 反应超时，ref 带 task = 主管自己待批）只留
最新一条——读侧 entriesAfter 的 down 过滤按族保留最新，写盘照旧只追加，
文案里的数字天然是最新那条；条件解除（主管又读了/审批落定了）由 sweeper
每轮 retractNotices 复算（responseNoticeLive 与写入口同一套判定），不再
成立的族在主管记录上记 noticeCleared[族]=seq，读侧据此收回，不需要用户点。
历史 escalation 的落定补标在 leads_escalation.backfillResolved（启动时跑）。
"""
import re
import time

import config
from modules import leads_inbox, leads_store
from modules import tasks_store as store
from modules.lead_inbox_store import readAll

# 算「下属消息」的条目类型。notice 是系统提示（无动静提示等），不算——
# 否则会和 P3 的 idle 提示互相刷屏。question 是 L2 新增：提问正是最等不得
# 的一类（规格 L2 的事故原型是互等 20 分钟）
RESPONSE_KINDS = ('report', 'failure', 'escalation', 'human', 'question')
# 收件箱唤醒摘要里的条目范围（_summaryText 生成：「seq X–Y」）——托管未
# 反应分支据此算出"这次唤醒送过去的几条"
_WAKE_RANGE_RE = re.compile(r'seq (\d+)[–-](\d+)')


def _int0(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _id8(entry):
    """条目指针里的任务 id 前 8 位（ref 形状 {'task': id, ...}）；
    没有任务指针的条目（human 等）退化成 seq，别在文案里留空。"""
    ref = entry.get('ref')
    task = ref.get('task') if isinstance(ref, dict) else ref
    return str(task or '')[:8] or ('#' + str(entry.get('seq') or '?'))


def questionText(title, args):
    """提问的收件箱正文：问题文本 + 选项 label 列表（Kimi AskUserQuestion 形状）。

    参数形如 {"questions": [{"question": "...", "options": [{"label": "..."}]}]}；
    拿不到 questions（只按工具名命中）时退化成工具名。
    """
    questions = []
    if isinstance(args, dict) and isinstance(args.get('questions'), list):
        questions = [q for q in args['questions'] if isinstance(q, dict)]
    parts = []
    for q in questions[:3]:
        text = str(q.get('question') or q.get('header') or '').strip()
        if not text:
            continue
        labels = [str(o.get('label') or '').strip()
                  for o in (q.get('options') or []) if isinstance(o, dict)]
        labels = [l for l in labels if l]
        parts.append(text + ('（选项：%s）' % ' / '.join(labels[:6])
                             if labels else ''))
    if not parts:
        parts.append(str(title or '提问')[:120])
    if len(questions) > 3:
        parts.append('…等 %d 个问题' % len(questions))
    return '；'.join(parts)


def emitQuestion(task, text, requestId):
    """L2：提问进主管收件箱（kind='question'，ref 带 requestId）并立即唤醒
    托管主管。返回写了哪个主管的箱（'' = 没写）。"""
    leadId = leads_inbox.emit(task, 'question', text,
                              {'requestId': str(requestId or '')})
    if leadId:
        leads_inbox.wakeNow(leadId)
    return leadId


# ---------------------------------------------------------------- L1

def _staleEntries(lead, now, sec):
    """分支一：既没投递也没被读、且年龄超阈值的下属消息。

    已落定的 escalation（审批出过结果）不算——线上实测驱动几秒批掉审批，
    收件箱堆几十条未读 escalation，L1 一直提醒"主管好久没处理"（主控追加）。
    """
    leadId = str(lead.get('id') or '')
    entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
    cursor = max(_int0(lead.get('inboxSeq')), _int0(lead.get('readSeq')))
    return [e for e in entries
            if _int0(e.get('seq')) > cursor
            and (e.get('kind') or '') in RESPONSE_KINDS
            and not leads_inbox.entryResolved(lead, e)
            and now - float(e.get('at') or 0) >= sec]


def _hostedUnreacted(lead, now, sec):
    """分支二：条目已投递但托管主管没反应。

    可达形态（都经过实测链路）：唤醒投递走了持久化 messageQueue（轮中入队
    或轮间被挂起审批挡住 drain 闸门），任务回到 waiting 之后那条唤醒还躺在
    队列里没人消费，且超过阈值没有任何新事件。返回本次唤醒覆盖的条目清单
    （文案取最早一条），不命中返回 None。
    """
    if str(lead.get('kind') or '') != 'hosted' or not lead.get('taskId'):
        return None
    if _int0(lead.get('inboxSeq')) <= 0:
        return None
    store.repo.load()
    task = store.repo.get(str(lead.get('taskId') or ''))
    if task is None or (task.get('status') or '') != 'waiting':
        return None
    if now - float(task.get('lastActiveAt') or 0) < sec:
        return None
    mq = task.get('messageQueue') or []
    wake = next((m for m in mq if m.get('by') == leads_inbox.INBOX_BY), None)
    if wake is None:
        return None          # 没有堵着没消费的唤醒：主管反应过了（或在干活）
    leadId = str(lead.get('id') or '')
    entries, _last = leads_inbox.entriesAfter(leadId, 0, 'up')
    m = _WAKE_RANGE_RE.search(str(wake.get('text') or ''))
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        covered = [e for e in entries if lo <= _int0(e.get('seq')) <= hi]
    else:
        covered = [e for e in entries
                   if _int0(e.get('seq')) <= _int0(lead.get('inboxSeq'))]
    covered = [e for e in covered if (e.get('kind') or '') in RESPONSE_KINDS
               and not leads_inbox.entryResolved(lead, e)]
    return covered or None


def responseNotice(lead, now=None):
    """L1：主管超时未处理收件箱 → down 一条 notice（指挥台「等你处理」可见）。

    同一主管每 leadResponseSec 最多一条（节流照旧）。N1：连写多条时读侧
    （entriesAfter 的 down 过滤）只显示最新那条——堆叠不再占屏。返回写下的
    seq（0 = 没写）。
    """
    now = now if now is not None else time.time()
    sec = config.getLeadResponseSec()
    if sec <= 0:
        return 0
    leadId = str(lead.get('id') or '')
    if not leadId:
        return 0
    if now - float(lead.get('responseNoticedAt') or 0) < sec:
        return 0
    stale = _staleEntries(lead, now, sec)
    covered = None if stale else _hostedUnreacted(lead, now, sec)
    if not stale and not covered:
        return 0
    rows = stale or covered
    text = ('主管 %s 有 %d 条下属消息超过 %d 分钟未处理（最早一条：%s %s）'
            % (lead.get('name') or leadId, len(rows), sec // 60,
               _id8(rows[0]), rows[0].get('kind') or ''))
    seq = leads_inbox.append(leadId, 'down', 'notice', text, {'lead': leadId})
    if seq:
        leads_store.markResponseNoticed(leadId, now)
    return seq


# ---------------------------------------------------------------- N1：一事一条 / 条件解除即收回

# notice 去重族（down 侧用户可见提醒的单位）：同一族同一主管只留最新一条。
# 族按 ref 形状判——responseNotice 写 {'lead': …}（L1 反应超时提醒），
# leads_escalation.noticeOwnPermission 写 {'task': …}（主管自己命令待批
# 提示）。ref 不带这两种指针的 notice（合成数据/未来新提醒）没有族：不被
# 取代也不被收回，原样透传（test_command_view 的 kind 用例即此形状）。
FAMILY_RESPONSE = 'response'
FAMILY_OWN_PERM = 'ownPerm'


def noticeFamily(entry):
    """down 侧 notice 的去重族；不是 notice 或没有族返回 None。"""
    if (entry or {}).get('kind') != 'notice':
        return None
    ref = entry.get('ref')
    if not isinstance(ref, dict):
        return None
    if ref.get('lead'):
        return FAMILY_RESPONSE
    if ref.get('task'):
        return FAMILY_OWN_PERM
    return None


def responseNoticeLive(lead, now=None):
    """L1 提醒的存续条件现在还成立吗（N1 条件解除即收回的复算面）。

    与 responseNotice 写入口同一套判定（_staleEntries / _hostedUnreacted），
    不另造逻辑：超时条目没了（主管读了/审批落定了/任务结束了）→ False，
    sweeper 据此收回。leadResponseSec<=0（功能关停）时同样 False——老提醒
    不该继续挂在用户面前。
    """
    sec = config.getLeadResponseSec()
    if sec <= 0:
        return False
    if not str((lead or {}).get('id') or ''):
        return False
    now = now if now is not None else time.time()
    return bool(_staleEntries(lead, now, sec) or _hostedUnreacted(lead, now, sec))


def _latestDownNotices(leadId):
    """每族最新的一条 down notice（原始视图）：{family: entry}。

    读侧的 entriesAfter 已把收走的过滤掉，retract 要看真现场，直接读文件
    （lead_inbox_store.readAll；只追加顺序即 seq 顺序，后者覆盖前者）。
    """
    rows = {}
    for entry in readAll(leadId):
        if entry.get('dir') != 'down' or (entry.get('kind') or '') != 'notice':
            continue
        family = noticeFamily(entry)
        if family:
            rows[family] = entry
    return rows


def retractNotices(lead, now=None):
    """条件解除即收回（N1）：sweeper 每轮对每位主管复算各 notice 族。

    族里最新那条的条件不再成立 → 主管记录上记 noticeCleared[族]=seq，读侧
    （leads_inbox.entriesAfter 的 down 过滤）不再返回它——不需要用户点。
    按 seq 记：同族来了更新的 notice 自动脱标（新事实优先）。返回收回了几条。
    """
    leadId = str((lead or {}).get('id') or '')
    if not leadId:
        return 0
    latest = _latestDownNotices(leadId)
    if not latest:
        return 0
    cleared = 0
    for family, entry in latest.items():
        if _noticeLive(family, entry, lead, now):
            continue
        _rec, err = leads_store.markNoticeCleared(leadId, family,
                                                  entry.get('seq'))
        if not err:
            cleared += 1
    return cleared


def _noticeLive(family, entry, lead, now):
    """族存续条件的分派：L1 反应超时 / 主管自己待批（条件跟在各自写入方）。"""
    if family == FAMILY_RESPONSE:
        return responseNoticeLive(lead, now)
    if family == FAMILY_OWN_PERM:
        from modules import leads_escalation   # 延迟导入：leads_escalation
        # 模块级 import leads_inbox，而本模块被 leads_inbox 延迟导入——
        # 放函数内才不成环
        return leads_escalation.ownPermissionLive(entry)
    return True
