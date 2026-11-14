# -*- coding: utf-8 -*-
"""续接撞忙（节点 409）与回合投递策略：RemoteAcpClient 的 mixin。

背景（调查 §3 形态 A，5 例中 4 例）：web 重启时回合正卡在工具调用上，节点侧
kimi 进程继续跑；主控发消息触发续接 → 新会话线程 attach 到节点上仍在 running
的同一会话 → prompt 被节点 enqueuePrompt 以 409 拒绝 → 任务直接 failed、新
消息丢失，之后旧回合的 tool/tool_result 还在继续到达。

本模块把"跟完旧回合再投"的策略与客户端传输层（agent_nodes）分开：
RemoteAcpClient 继承本 mixin，prompt() 见到 409 转进来。等待不设总时限
（常驻会话语义，与 prompt_timeout=0 一致），但 close/cancel 置位后立刻跳出。
"""
import time

# 首次 409 之后最多重投 3 次，第 3 次仍 409 才失败；running 为真时等旧回合
# 结束（turn_end 事件或轮询转假）再投，running 为假却仍 409（调查 §7.2 不明
# 例同形）按固定间隔重投
BUSY_RETRY_MAX = 3
BUSY_POLL_SEC = 2
BUSY_STATUS_TEXT = '节点上一轮仍在执行，等它结束后再投递这条消息'


class BusyPromptMixin:
    """回合投递 + 撞忙重投。宿主（RemoteAcpClient）须提供：

    _node()/node_sid（寻址）、_turnEvent/_stopReason/_closed/_cancelAsked
    （回合与停止信号）、on_status（状态事件回调）、prompt_timeout、exit_code。
    HTTP 经 _nodeRequest 走 agent_nodes.nodeRequest——测试对
    nodesMod.nodeRequest 的打桩通过这层生效。
    """

    def _nodeRequest(self, method, path, payload=None, timeout=30):
        # 延迟导入：agent_nodes 加载本模块作基类，模块级反向 import 成环
        from modules import agent_nodes
        return agent_nodes.nodeRequest(self._node(), method, path,
                                       payload, timeout=timeout)

    # ---- 派活与回合等待 ----

    def postPrompt(self, text):
        return self._nodeRequest(
            'POST', '/api/sessions/%s/prompt' % self.node_sid,
            {'text': text}, timeout=30)

    def awaitTurn(self, timeout):
        wait = timeout or self.prompt_timeout or None
        if wait == 0:
            wait = None   # 常驻会话：不限时等待回合结束
        if not self._turnEvent.wait(wait):
            raise RuntimeError('远端回合超时')
        if self.exit_code is not None and not self._stopReason:
            raise RuntimeError('远端 ACP 进程已退出 (code=%s)' % self.exit_code)
        return {'stopReason': self._stopReason}

    # ---- 409 撞忙：先跟完旧回合再投 ----

    def promptWhenBusy(self, text, timeout):
        """节点会话忙（409）时的续接派活：跟完旧回合再投，不判失败。

        按节点会话状态分流：running 为真 → 落一条 status 事件并等旧回合
        结束，再重投；running 为假却仍 409 按 2 秒间隔重投。最多
        BUSY_RETRY_MAX 次，仍 409 才失败（持续忙带已等待秒数；假 409 带
        节点返回原文）。
        """
        waited = 0.0
        running = None
        lastData = {}
        for _retry in range(BUSY_RETRY_MAX):
            running = self.sessionRunning()
            if running:
                self.on_status(BUSY_STATUS_TEXT)
                waited += self.waitOldTurnEnd()
                if self._stopped():
                    return {'stopReason': ''}
            else:
                if not self._sleepUnlessStop(BUSY_POLL_SEC):
                    return {'stopReason': ''}
                waited += BUSY_POLL_SEC
            # 旧回合的 turn_end 可能已经到达（那正是等它的信号）：投新消息前
            # 必须清掉，否则新回合会被旧回合的结束事件瞬间放行、半路返回
            self._turnEvent.clear()
            self._stopReason = ''
            status, data = self.postPrompt(text)
            lastData = data or {}
            if status == 202:
                return self.awaitTurn(timeout)
            if status != 409:
                raise RuntimeError('远端派活失败(%s): %s'
                                   % (status, lastData.get('error') or lastData))
        if running:
            raise RuntimeError('节点会话持续忙，已等待 %.0f 秒' % waited)
        raise RuntimeError('远端会话持续繁忙：%s'
                           % (lastData.get('error') or lastData))

    def sessionRunning(self):
        """节点会话是否仍在执行回合；查询失败按"不在跑"处理（走定时重试）。"""
        try:
            status, data = self._nodeRequest(
                'GET', '/api/sessions/%s' % self.node_sid, timeout=15)
        except Exception:
            return False
        if status != 200:
            return False
        session = (data or {}).get('session') or data or {}
        return bool(session.get('running'))

    def waitOldTurnEnd(self):
        """等旧回合结束：turn_end/exit 事件（事件流线程推来）或轮询 running
        转假，任一即止；轮询只是兜底，间隔 BUSY_POLL_SEC，不设总时限。

        等待期间旧回合的 update/审批经事件流线程照常落卡（replay 门禁只拦
        session/load 回放，不拦 live），新的审批请求正常挂起可批。
        """
        waited = 0.0
        sincePoll = 0.0
        while not self._stopped():
            if self._turnEvent.wait(0.2):
                return waited
            waited += 0.2
            sincePoll += 0.2
            if sincePoll >= BUSY_POLL_SEC:
                sincePoll = 0.0
                if not self.sessionRunning():
                    return waited
        return waited

    def _sleepUnlessStop(self, seconds):
        """分段睡；close/cancel 置位即返回 False（不等满）。"""
        deadline = time.monotonic() + seconds
        while not self._stopped():
            remain = deadline - time.monotonic()
            if remain <= 0:
                return True
            self._turnEvent.wait(min(0.2, remain))
        return False

    def _stopped(self):
        """close/cancel 置位即视为停止等待（close 不能卡在续接等待上）。"""
        return self._closed or self._cancelAsked


def taskStatusEmitter(task):
    """把客户端的等待/重试状态落成任务卡 status 事件（续接撞忙时，主控看得见
    为什么这条消息还没投递）。内部兜异常——提示不能带崩任务主链路。"""
    def emit(text):
        try:
            from modules import tasks_store as store
            store.repo.append_event(task, 'status', text=text)
        except Exception:
            pass
    return emit
