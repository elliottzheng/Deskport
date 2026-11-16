# -*- coding: utf-8 -*-
"""任务生命周期巡检（批次 L）：重启后自动续接（L3）+ 卡住的工具调用（L4）。

单 daemon 5s 一跳（bindApp 注入后自起，先例 leads_inbox.bindApp）；不
import Flask——续接委托 runner._resumeWithMessage（它自己起会话线程），
报警只写事件与收件箱。

L3：web 启动时被打成 paused 且 pausedBy='restart'（tasks_store.load 只在
这条路径设）的任务，启动 60 秒后自动发一条续接消息，每个任务只自动续一次
（发前即清标记，失败也不重试——用户主动暂停/线程猝停都不带这个标记，
构造上就不会被续）。属于主管的往收件箱写一条 notice。

L4：任务 running 且当前工具调用超过 toolStallSec 没有任何事件 → 一条
status 事件；属于主管的下属往收件箱写 failure，无主管的留一个 _toolStall
标记进指挥台「等你处理」（command_view 读它，工具收尾即清）。每次工具
调用只报一次（_currentTool.stalled 标记）。
"""
import logging
import threading
import time

import config
from modules import leads_inbox
from modules import tasks_runner as runner
from modules import tasks_store as store

log = logging.getLogger('task-health')
SWEEP_SEC = 5
AUTO_RESUME_DELAY_SEC = 60        # web 启动后多久才自动续（给手动续接留窗口）
_startedAt = time.time()          # 模块导入≈web 启动；测试可 monkeypatch
_started = False


def bindApp(app):
    """create_app 注入后拉起巡检线程（照 leads_inbox.bindApp；app 本身不用）。"""
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True,
                         name='task-health').start()


def _loop():
    while True:
        try:
            tick(time.time())
        except Exception as e:      # 巡检失败不能带走线程，下一跳重试
            log.warning('task health tick failed: %s', e)
        time.sleep(SWEEP_SEC)


def tick(now=None):
    """扫一跳（测试可直接调）：L3 自动续接 + L4 工具卡住报警。"""
    now = now if now is not None else time.time()
    _autoResume(now)
    _toolStall(now)


# ---------------------------------------------------------------- L3

# 驱动（driver.py）的「接着跑」不发消息、只重新等；这里是平台代发的续接
# 文案，口径同 COLLABORATION.md §7：中断后先查实况，别凭记忆假设上一步
AUTO_RESUME_TEXT = ('Deskport 服务重启过，本任务已自动续接。先查状态与产物'
                    '实况（别凭记忆假设上一步成败），再接着跑。')


def _autoResume(now):
    """重启打断的任务：启动 60s 后自动续一条（只一次，发前清标记）。"""
    if not config.getAutoResumeAfterRestart():
        return
    if now - _startedAt < AUTO_RESUME_DELAY_SEC:
        return
    store.repo.load()
    for task in store.repo.list():
        if (task.get('status') or '') != 'paused' \
                or task.get('pausedBy') != 'restart':
            continue
        with store.repo.lock:
            if task.get('pausedBy') != 'restart':     # 锁内复查：只续一次
                continue
            task['pausedBy'] = ''
            task['autoResumedAt'] = now
            store.repo.save()
        log.info('auto resume after restart: %s', task['id'])
        try:
            runner._resumeWithMessage(task, AUTO_RESUME_TEXT)
        except Exception as e:
            # 发起失败也只算一次（标记已清）：任务回 paused 由唤醒看门狗兜底
            log.warning('auto resume %s failed: %s', task['id'], e)
            continue
        leads_inbox.emit(task, 'notice', '任务 %s 因重启被打断，已自动续接'
                         % task['id'][:8])


# ---------------------------------------------------------------- L4

def _waitedText(sec):
    """无输出时长的文案：≥60s 说分钟，<60s 说秒（toolStallSec 可以配成
    30 这种小值，分钟数会是 0——返工：指挥台因此显示过「? 分钟」）。"""
    return '%d 分钟' % (sec // 60) if sec >= 60 else '%d 秒' % sec


def _toolStall(now):
    """running 任务里超过 toolStallSec 没有任何事件的工具调用：报警一次。"""
    sec = config.getToolStallSec()
    if sec <= 0:
        return
    store.repo.load()
    for task in store.repo.list():
        if (task.get('status') or '') != 'running':
            continue
        with store.repo.lock:
            tool = task.get('_currentTool')
            if not isinstance(tool, dict) or tool.get('stalled'):
                continue
            since = float(tool.get('since') or 0)
            if now - since < sec \
                    or now - float(task.get('lastActiveAt') or 0) < sec:
                continue
            name = str(tool.get('name') or 'tool')
            tool['stalled'] = True
            if not str(task.get('lead') or ''):
                # 无主管：留标记给指挥台「等你处理」（command_view 读它）
                task['_toolStall'] = {'name': name, 'since': since,
                                      'at': now, 'sec': sec}
            store.repo.save()
        store.repo.append_event(
            task, 'status',
            text='工具 %s 已 %s无输出（疑似卡住；要停就取消这一轮）'
                 % (name, _waitedText(sec)))
        if str(task.get('lead') or ''):
            leads_inbox.emit(task, 'failure',
                             '任务 %s 的 %s 已 %s无输出'
                             % (task['id'][:8], name, _waitedText(sec)))
