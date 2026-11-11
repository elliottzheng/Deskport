# -*- coding: utf-8 -*-
"""模型可用性呈现与门禁（R4-10）。

探测本体在 agent_node/model_probe.py（node 侧托管，24h 缓存）；本模块只做
web 侧的三件事：
- nodeModelStatus(name): 执行点的 {modelStatus, usableModels}（来自
  /health 缓存；本机 stdio 直跑时由 web 内探测托管兜底）
- probeNodeModels(name, models): 手动触发探测，转发到 node / 联邦成员
- modelGate(node, model): 建任务门禁——已知 usable=false 拒绝，没探测过的放行
"""
import os
import shutil
import threading
import time

import config


def _nullEntry():
    return {'usable': None, 'checkedAt': 0, 'error': ''}


def nodeModelStatus(name):
    """执行点的探测状态；探测能力未上线（旧 node/成员）= 全部 usable=null。"""
    from modules.agent_nodes import _healthModelsCached, nodeModelsFor
    wanted = str(name or '').strip() or 'local'
    models, _default = nodeModelsFor(wanted)
    if wanted == 'local' and config.getLocalAgentNode() is None:
        status = _stdioProber().status(models)   # 本机 stdio 直跑：web 自己托管
    else:
        status = _healthModelsCached().get(wanted, {}).get('modelStatus') or {}
    modelStatus = {m: dict(status.get(m) or _nullEntry()) for m in models}
    usableModels = sorted(m for m, v in modelStatus.items() if v['usable'] is True)
    return {'modelStatus': modelStatus, 'usableModels': usableModels}


def modelGate(node, model):
    """建任务门禁。model 已知 false → (拒绝理由, 可用性)；否则 (None, 可用性)。

    只拦「显式指定」的模型——没传 model 时落执行点默认，不在此拦截
    （规格如此：指定的 model 才 400）。usable=null（没探测/过期）放行。
    """
    model = str(model or '').strip()
    if not model:
        return None, {'modelStatus': {}, 'usableModels': []}
    avail = nodeModelStatus(node)
    item = avail['modelStatus'].get(model)
    if item and item.get('usable') is False:
        reason = ('执行点 %s 的模型 %s 24h 内探测失败：不可用（%s）；已验证可用: %s'
                  % (str(node or 'local'), model, item.get('error') or '原因未知',
                     ', '.join(avail['usableModels']) or '（无）'))
        return reason, avail
    return None, avail


def probeNodeModels(name, models=None):
    """手动触发探测；返回 (status_code, data)。models 空 = 该执行点声明全部。"""
    wanted = str(name or '').strip() or 'local'
    payload = {'models': [str(m) for m in (models or [])]}
    from modules.agent_nodes import findNode, nodeRequest
    node = findNode(wanted)
    if node is None:
        local = config.getLocalAgentNode()
        if local is not None and local['name'] == wanted:
            node = local
    if node is not None:
        return nodeRequest(node, 'POST', '/api/probe-models', payload, timeout=15)
    from modules.tasks_federation import _memberCall, findFederationMember
    member = findFederationMember(wanted)
    if member is not None:
        # 成员转发到它自己的 local 执行点（成员同版才有该路由，旧版 404 透传）
        return _memberCall(member, 'POST', '/api/tasks/nodes/local/probe',
                           payload, timeout=15)
    if wanted == 'local' and config.getLocalAgentNode() is None:
        from modules.agent_nodes import localKimiModels
        declared = localKimiModels()['models']
        prober = _stdioProber()
        target = [str(m).strip() for m in (models or [])] or declared
        queued = prober.probe(target, force=True)
        return 200, {'queued': queued, 'modelStatus': prober.status(declared),
                     'usableModels': prober.usableModels(declared)}
    return 404, {'error': '节点不存在'}


# ---- 本机 stdio 直跑（localAgentNodePort=0）时的 web 内探测托管 ----

_stdioLock = threading.Lock()
_stdioProberInstance = None


def _stdioProber():
    global _stdioProberInstance
    with _stdioLock:
        if _stdioProberInstance is None:
            import importlib.util
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'agent_node', 'model_probe.py')
            spec = importlib.util.spec_from_file_location('deskport_model_probe', path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            kimi = os.environ.get('KIMI_BIN') or shutil.which('kimi') or ''
            data_dir = os.path.join(os.path.expanduser('~'), '.deskport',
                                    'model-probe')
            _stdioProberInstance = mod.ModelProber(kimi, data_dir)
            from modules.agent_nodes import localKimiModels
            declared = localKimiModels()
            default = [m for m in (declared['default'],) if m]
            _stdioProberInstance.probe(default + recentTaskModels('local'),
                                       force=False)
        return _stdioProberInstance


def recentTaskModels(node, days=7, cap=8):
    """web 任务库里该执行点近 7 天用过的模型（最新优先、去重、限量）。

    覆盖本机/agent node/联邦成员的建任务历史；node 侧自扫只看得到自己的
    会话存档，两处口径互补。
    """
    try:
        from modules import tasks_store as store
        store.repo.load()
        cutoff = time.time() * 1000 - days * 86400 * 1000
        with store.repo.lock:
            items = sorted(store.repo.list(),
                           key=lambda x: -(x.get('createdAt') or 0))
        seen = []
        for t in items:
            tnode = t.get('node') or ('local' if (t.get('engine') or 'acp') == 'acp'
                                      else '')
            if tnode != node:
                continue
            model = str(t.get('model') or t.get('modelEffective') or '').strip()
            if model and model not in seen and (t.get('createdAt') or 0) >= cutoff:
                seen.append(model)
        return seen[:cap]
    except Exception:
        return []
