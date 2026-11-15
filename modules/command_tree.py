# -*- coding: utf-8 -*-
"""指挥树（批次 U2）：把主管/任务/派活人拼成一棵树，供指挥台一屏渲染。

为什么单独一个模块：command_view.py 已贴 400 行预算（docs/code-structure.md：
Python 模块 ≤400 行，超出先拆），合并 main 后它还要装 L4 的 stall 行。这里只做
「树怎么长」——节点形状、父子挂载、逐级汇总、超深折层；数据仍由 command_view
只读聚合后传进来（本模块不碰存储、不 import Flask）。

树的一级节点五形（形状与计数口径见 docs/api.md 的 overview 一节）：
    lead    非 retired 主管；节点本身即它的托管任务（ownTask 从子项剔除，
            但计入该节点计数——与旧 leads[].counts 同口径）；
    owner   没有主管记录却派过任务的令牌（按 owner 分组）；
    direct  面板/网页会话自己派的（owner='面板'，与 tasks_api 同一字面量）；
    orphan  主管记录已停用/被删、任务还挂着——并进树，不让人从视野里消失；
    more    超过 TREE_DEPTH 层的主管折成一个汇总节点，不再展开。
任务节点只带 TASK_NODE_KEYS 里的字段（不带 counts）：树与旧字段都带全量 brief
会让 300 个任务超 200KB。
"""
TREE_DEPTH = 3                        # 指挥树最多 3 层，更深折成「更多层级 N」
PANEL_OWNER = '面板'                  # 面板自己派的活（tasks_api 同一字面量）
TASK_NODE_KEYS = ('id', 'title', 'status', 'node', 'model', 'modelEffective',
                  'lastActiveAt')


def taskNode(brief):
    """任务节点：TASK_NODE_KEYS 里的字段 + type/key（行内只用状态点与标题）。"""
    return dict({k: brief.get(k) for k in TASK_NODE_KEYS}, type='task',
                key='task:' + str(brief.get('id') or ''))


def leadNode(row):
    """旧 leads 行 → 树的 lead 节点：托管任务是节点本身，不在子项里重复。"""
    own = row.get('taskId') or ''
    # 全量 briefs 留给旧字段；counts 必须拷副本——_attach 的逐级汇总是就地
    # 累加，浅拷贝会让树的汇总写进与旧字段 leads[].counts 共享的同一个 dict
    # （旧字段供回滚与对账，语义不能被静默改掉）
    node = {k: v for k, v in row.items() if k != 'tasks'}
    node['counts'] = dict(row.get('counts') or {})
    return dict(node, type='lead', key='lead:' + (row.get('id') or ''),
                children=[taskNode(b) for b in row.get('tasks') or []
                          if b.get('id') != own])


def _attach(node, byLead, kids, depth, counts):
    """挂下一层主管并递归（最多 TREE_DEPTH 层），后代计数逐级汇总进本节点。"""
    def tasksOf(kid):                   # 该主管子树里的全部任务（含子主管的）
        out = list(byLead.get(kid['id'], []))
        for sub in kids.get(kid['id'], []):
            out += tasksOf(sub)
        return out

    deeper = kids.get(node['id'], [])
    if depth >= TREE_DEPTH:
        if deeper:      # 超出的层折成一个汇总节点：只给计数，不再展开
            node['children'].append(dict(
                type='more', key=node['key'] + ':more', id='', children=[],
                name='更多层级 %d' % len(deeper),
                counts=counts([t for kid in deeper for t in tasksOf(kid)])))
    else:
        for kid in deeper:
            node['children'].append(_attach(kid, byLead, kids, depth + 1, counts))
    for kid in node['children']:        # 逐级汇总：子主管/折层节点的计数并进来
        for k, v in (kid.get('counts') or {}).items():
            node['counts'][k] = node['counts'].get(k, 0) + v
    return node


def _groupOf(task, retiredLeads):
    """无主管记录的任务 → (分组 key, 分组)。"""
    leadId = str(task.get('lead') or '')
    if leadId:
        return 'lead:' + leadId, {'id': leadId, 'type': 'orphan',
                                  'name': retiredLeads.get(leadId)
                                  or ('已停用的主管 ' + leadId)}
    owner = str(task.get('owner') or '') or PANEL_OWNER
    if owner == PANEL_OWNER:
        return 'direct', {'id': '', 'type': 'direct', 'name': '你直接派的'}
    return 'owner:' + owner, {'id': owner, 'name': owner, 'type': 'owner'}


def build(leads, leadRows, byLead, tasks, retiredLeads, counts, splitTasks):
    """拼指挥树。leadRows 是 command_view 聚合好的旧字段行（含 tasks briefs）；
    counts/splitTasks 由调用方传入（那边还有 pending 行在用，不复制实现）。
    """
    leadNodes = {row['id']: leadNode(row) for row in leadRows}
    kids, roots = {}, []               # 主管 id → 下一层主管节点 / 一级节点
    for lead in leads:
        node = leadNodes.get(str(lead.get('id') or ''))
        if node is None:               # retired 的主管不进树
            continue
        parent = str(lead.get('parent') or '')
        if parent in ('', 'human') or parent not in leadNodes:
            roots.append(node)
        else:
            kids.setdefault(parent, []).append(node)
    tree = [_attach(node, byLead, kids, 1, counts) for node in roots]

    groups = {}                        # key → 分组（保首次出现顺序）
    for task in tasks:
        if str(task.get('lead') or '') in leadNodes:
            continue                   # 有主任务已归主管节点
        key, bucket = _groupOf(task, retiredLeads)
        groups.setdefault(key, bucket).setdefault('tasks', []).append(task)
    flat = []      # 一级顺序：未登记/已停用在前，面板自己派的排最后
    for kind in ('orphan', 'owner', 'direct'):
        for key, bucket in groups.items():
            if bucket['type'] != kind:
                continue
            bucketTasks = bucket.pop('tasks')   # 只用于裁剪/计数，不进响应
            briefs, hidden = splitTasks(bucketTasks)
            flat.append(dict(bucket, key=key, closedHidden=hidden,
                             counts=counts(bucketTasks),
                             children=[taskNode(b) for b in briefs]))
    return tree + flat
