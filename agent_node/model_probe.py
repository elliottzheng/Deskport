# -*- coding: utf-8 -*-
"""模型可用性探测（R4-10）：对每个模型做一次最小调用，结果缓存 24h。

为什么用 `kimi -p` 而不是 ACP 会话：判定信号干净——退出码 0 = 可用，
别名不存在/后端故障 = 非零退出 + stderr，后端挂死 = 超时杀掉；而 ACP 里
"模型坏了"可能只是 turn 内一条错误 update，语义含混。且 -p 进程自然退出，
无需托管 JSON-RPC。代价：每次探测留一条 kimi 会话记录（kimi 没有
session 删除接口），量级 = 探测次数，靠"默认只测 defaultModel + 近 7 天
用过的"控制（全量留给手动触发）。

并发 ≤2 的后台 worker；缓存落盘 model_status.json，node 重启不丢（24h
内不重复花钱）。/health 只读内存缓存，绝不为探测阻塞。
纯标准库，与 agent_node 其余部分同约束。
"""
import json
import os
import subprocess
import threading
import time
from collections import deque

PROBE_PROMPT = 'ok'
PROBE_TIMEOUT_SEC = 30
CACHE_TTL_SEC = 24 * 3600
MAX_WORKERS = 2
MAX_QUEUE_MODELS = 200
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def nowMs():
    return int(time.time() * 1000)


def probeOnce(kimi_bin, model, cwd, timeout=PROBE_TIMEOUT_SEC):
    """一次最小调用：kimi -p ok --model <别名>。返回 (usable, error)。"""
    if not kimi_bin:
        return False, 'kimi 不可执行'
    cmd = [kimi_bin, '-p', PROBE_PROMPT, '--model', model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout,
                              cwd=cwd, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, '探测超时(%ds)' % int(timeout)
    except OSError as e:
        return False, 'kimi 启动失败: %s' % e
    if proc.returncode == 0:
        return True, ''
    out = '\n'.join(x for x in ((proc.stderr or '').strip(),
                                (proc.stdout or '').strip()) if x)
    return False, (out[:300] or ('退出码 %s' % proc.returncode))


class ModelProber:
    """模型可用性缓存 + ≤2 并发后台探测。

    缓存条目 {usable: bool, checkedAt: epoch_ms, error: str}；对外查询按
    24h TTL 判定，过期条目 usable 报 None（等同没探测过，可放行可复测）。
    """

    def __init__(self, kimi_bin, data_dir, probe_fn=None):
        self.kimi_bin = kimi_bin
        self.data_dir = data_dir
        self.probe_fn = probe_fn or probeOnce
        self.probe_cwd = os.path.join(data_dir, 'probe-cwd')
        self._path = os.path.join(data_dir, 'model_status.json')
        self._cond = threading.Condition()
        self._status = {}       # model -> 原始条目（未判过期）
        self._queued = set()    # 已排队/在测，防止重复调用
        self._queue = deque()
        self._load()
        os.makedirs(self.probe_cwd, exist_ok=True)
        for _ in range(MAX_WORKERS):
            threading.Thread(target=self._worker, daemon=True).start()

    # ---- 存取 ----
    def _load(self):
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for model, item in (data or {}).items():
            if isinstance(item, dict) and isinstance(item.get('usable'), bool):
                self._status[str(model)] = {
                    'usable': item['usable'],
                    'checkedAt': int(item.get('checkedAt') or 0),
                    'error': str(item.get('error') or '')[:300]}

    def _save(self):
        with self._cond:
            snapshot = {k: dict(v) for k, v in self._status.items()}
        tmp = self._path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except OSError:
            pass

    # ---- 查询 ----
    def _effective(self, model):
        item = self._status.get(model)
        if item is None:
            return {'usable': None, 'checkedAt': 0, 'error': ''}
        if nowMs() - item['checkedAt'] > CACHE_TTL_SEC * 1000:
            return {'usable': None, 'checkedAt': item['checkedAt'], 'error': ''}
        return dict(item)

    def status(self, declared=()):
        """{model: {usable: true|false|null, checkedAt, error}}。

        declared 里的未探测模型也带 usable=null 条目——调用方据此区分
        「还没探测」与「探测过可用」。
        """
        models = set(self._status) | {str(m or '').strip() for m in declared}
        with self._cond:
            return {m: self._effective(m) for m in sorted(m for m in models if m)}

    def usableModels(self, declared=()):
        return [m for m, item in self.status(declared).items()
                if item['usable'] is True]

    def isUsable(self, model):
        """true / false / None（没探测或已过期）。"""
        return self._effective(str(model or '').strip())['usable']

    # ---- 触发 ----
    def probe(self, models, force=True):
        """把模型排进后台队列，返回实际入队数。

        force=False 用于默认扫描：24h 内测过的直接跳过。已在队列/在测的
        模型不去重判断 force，一律跳过（避免同一模型并发双份调用）。
        """
        now = nowMs()
        queued = 0
        with self._cond:
            for raw in models or []:
                model = str(raw or '').strip()
                if not model or len(model) > 200 or model in self._queued:
                    continue
                if not force:
                    item = self._status.get(model)
                    if item and now - item['checkedAt'] <= CACHE_TTL_SEC * 1000:
                        continue
                self._queued.add(model)
                self._queue.append(model)
                queued += 1
            if queued:
                self._cond.notify_all()
        return queued

    def pending(self):
        with self._cond:
            return len(self._queued)

    # ---- worker ----
    def _worker(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                model = self._queue.popleft()
            try:
                usable, error = self.probe_fn(self.kimi_bin, model, self.probe_cwd)
            except Exception as e:   # 探测函数本身不许弄死 worker
                usable, error = False, '探测异常: %s' % e
            with self._cond:
                self._status[model] = {'usable': bool(usable),
                                       'checkedAt': nowMs(),
                                       'error': str(error or '')[:300]}
                self._queued.discard(model)
                self._save()   # 写盘也算在 pending 里：pending==0 即已持久化

    def recentModels(self, sessions, days=7, cap=8):
        """最近 N 天用过的模型（来自 node 会话存档）：最新优先、去重、限量。"""
        cutoff = nowMs() - days * 86400 * 1000
        seen = []
        for s in sorted((x for x in sessions if isinstance(x, dict)),
                        key=lambda x: -(x.get('lastActive') or 0)):
            model = str(s.get('model') or '').strip()
            if model and model not in seen and (s.get('lastActive') or 0) >= cutoff:
                seen.append(model)
        return seen[:cap]
