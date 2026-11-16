# -*- coding: utf-8 -*-
"""工作项状态机（批次 G2）：状态集合、迁移表、创建校验与足迹相交判定。

纯函数、不 import Flask、不 import 存储——工作项字典由调用方传入，
迁移经 transition() 落 history。状态字符串同时存进工作项字典（兼容
前端与既有形状），所有写入都走这里，避免流水线/API 各自解释状态。

状态图（规格 §1 + §2b）：
    queued ──▶ implementing ──▶ verifying ──┬─▶ integrating ──▶ integrated
      ▲             ▲   ▲                    │      │
      │             │   └── rework ──────────┘      └── 同步冲突/集成失败
      │             │                                （回 implementing）
      │             └── 任务起不来/卡死/提前收工/换手失败
      └── needs_judgment ◀── 实施有 questions / 验收 needs_judgment /
                             attempt 超限 / 执行结束无结果 / 主管叫停
    failed / cancelled 为旁路终态；integrated 是 v1 终态（上线留待后续批次，
    迁移函数签名与 history 记录先留口，见 startRelease）。
"""
import fnmatch
import logging
import time

log = logging.getLogger('workitem-state')

QUEUED = 'queued'                # §2b：足迹有交集，等待别的工作项完成
IMPLEMENTING = 'implementing'
VERIFYING = 'verifying'
INTEGRATING = 'integrating'
INTEGRATED = 'integrated'
NEEDS_JUDGMENT = 'needs_judgment'
FAILED = 'failed'
CANCELLED = 'cancelled'

STATES = (QUEUED, IMPLEMENTING, VERIFYING, INTEGRATING, INTEGRATED,
          NEEDS_JUDGMENT, FAILED, CANCELLED)

TERMINAL = (INTEGRATED, FAILED, CANCELLED)

TRANSITIONS = {
    QUEUED: {IMPLEMENTING, NEEDS_JUDGMENT, FAILED, CANCELLED},
    IMPLEMENTING: {VERIFYING, NEEDS_JUDGMENT, FAILED, CANCELLED},
    VERIFYING: {INTEGRATING, IMPLEMENTING, NEEDS_JUDGMENT, FAILED, CANCELLED},
    INTEGRATING: {INTEGRATED, IMPLEMENTING, NEEDS_JUDGMENT, FAILED, CANCELLED},
    INTEGRATED: set(),
    NEEDS_JUDGMENT: {IMPLEMENTING, VERIFYING, INTEGRATING, CANCELLED, FAILED},
    FAILED: {CANCELLED},
    CANCELLED: set(),
}

TITLE_MAX = 40
SPEC_TEXT_MAX = 8000
FOOTPRINT_MAX_ITEMS = 20
FOOTPRINT_MAX_LEN = 200
MAX_ATTEMPTS_DEFAULT = 3
MAX_ATTEMPTS_CAP = 10
HISTORY_MAX = 200

# 阶段进度（指挥台一行展示用）：实施 / 验收 / 集成
STAGE_IMPL, STAGE_VERIFY, STAGE_INTEGRATE = 'impl', 'verify', 'integrate'
STAGE_BY_STATE = {
    QUEUED: 'queue', IMPLEMENTING: STAGE_IMPL, VERIFYING: STAGE_VERIFY,
    INTEGRATING: STAGE_INTEGRATE, INTEGRATED: 'done',
    NEEDS_JUDGMENT: 'judge', FAILED: 'fail', CANCELLED: 'cancelled',
}


def stageOf(state):
    return STAGE_BY_STATE.get(state or '', 'impl')


def canTransition(state, to):
    return to in TRANSITIONS.get(state or '', set())


def transition(wi, to, reason='', **ref):
    """迁移工作项状态并写 history（时间、原因、关联任务/提交）。

    非法迁移不改状态、不写 history，返回错误串（调用方据此止损）；
    同态迁移是空操作，返回 ''。history 只保留最近 HISTORY_MAX 条。
    """
    current = wi.get('state') or ''
    if current == to:
        return ''
    if not canTransition(current, to):
        log.warning('非法工作项状态迁移 %s -> %s (%s)',
                    current or '(空)', to, wi.get('id') or '')
        return '状态不允许迁移 %s -> %s' % (current or '(空)', to)
    wi['state'] = to
    entry = {'at': time.time(), 'from': current, 'to': to,
             'reason': str(reason or '')[:300]}
    for key, value in ref.items():
        if value not in (None, '', []):
            entry[key] = value
    history = wi.setdefault('history', [])
    history.append(entry)
    if len(history) > HISTORY_MAX:
        del history[:len(history) - HISTORY_MAX]
    return ''


# ---------------------------------------------------------------- 创建校验

def _cleanModel(value, field, errors):
    text = str(value or '').strip()
    if not text:
        errors[field] = '%s 必填' % field
        return ''
    if len(text) > 100 or any(c < ' ' for c in text):
        errors[field] = '%s 不合法（≤100 字符，无控制字符）' % field
        return ''
    return text


def _cleanFootprint(value, errors):
    if not isinstance(value, list) or not value:
        errors['footprint'] = 'footprint 必填：至少声明一个预计改动的文件/目录（支持 glob）'
        return []
    if len(value) > FOOTPRINT_MAX_ITEMS:
        errors['footprint'] = 'footprint 最多 %d 项' % FOOTPRINT_MAX_ITEMS
        return []
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors['footprint[%d]' % i] = '需为非空字符串（glob）'
            continue
        text = item.strip().replace('\\', '/')
        if len(text) > FOOTPRINT_MAX_LEN:
            errors['footprint[%d]' % i] = '单项过长（≤%d 字符）' % FOOTPRINT_MAX_LEN
            continue
        out.append(text)
    return out


def validateCreate(data):
    """创建入参校验（规格 §1）。返回 (clean, errors)：errors 为空才收。"""
    errors = {}
    data = data if isinstance(data, dict) else {}
    title = str(data.get('title') or '').strip()
    if len(title) > TITLE_MAX:
        errors['title'] = '标题超长（上限 %d 字）' % TITLE_MAX
    specPath = str(data.get('specPath') or '').strip()
    specText = str(data.get('specText') or '').strip()
    if bool(specPath) == bool(specText):
        errors['spec'] = 'specPath 与 specText 必须二选一（传文件路径或全文）'
    if specText and len(specText) > SPEC_TEXT_MAX:
        errors['specText'] = '规格全文超长（上限 %d 字，请改传 specPath）' % SPEC_TEXT_MAX
    implModel = _cleanModel(data.get('implModel'), 'implModel', errors)
    verifyModel = _cleanModel(data.get('verifyModel'), 'verifyModel', errors)
    if implModel and verifyModel and implModel == verifyModel:
        errors['verifyModel'] = 'verifyModel 必须与 implModel 不同（同一模型 = 同一种盲区）'
    try:
        maxAttempts = int(data.get('maxAttempts') or MAX_ATTEMPTS_DEFAULT)
    except (TypeError, ValueError):
        maxAttempts = MAX_ATTEMPTS_DEFAULT
    if not 1 <= maxAttempts <= MAX_ATTEMPTS_CAP:
        errors['maxAttempts'] = 'maxAttempts 需在 1–%d 之间' % MAX_ATTEMPTS_CAP
    clean = {'title': title, 'specPath': specPath, 'specText': specText,
             'implModel': implModel, 'verifyModel': verifyModel,
             'maxAttempts': maxAttempts,
             'footprint': _cleanFootprint(data.get('footprint'), errors),
             'forceParallel': bool(data.get('forceParallel'))}
    return clean, errors


def canRework(wi):
    """还能不能再返工一次（attempt 上限，规格 §5）。"""
    try:
        attempt = int((wi or {}).get('attempt') or 0)
        cap = int((wi or {}).get('maxAttempts') or MAX_ATTEMPTS_DEFAULT)
    except (TypeError, ValueError):
        return False
    return attempt < cap


# ---------------------------------------------------------------- 足迹（§2b）

def _staticHead(pattern):
    """glob 的静态前缀：第一个通配符之前的路径部分（含结尾分隔符）。

    `modules/workitem_*.py` → `modules/`；`README.md` → `README.md`。
    两个模式只要静态前缀有包含关系，就存在同时命中的文件名。
    """
    cut = len(pattern)
    for i, ch in enumerate(pattern):
        if ch in '*?[':
            cut = i
            break
    head = pattern[:cut]
    if cut < len(pattern) and head and not head.endswith('/'):
        head = head.rsplit('/', 1)[0] + '/'
    return head or '/'


def _pathPrefix(short, long):
    if short == long:
        return True
    if not short.endswith('/'):
        short += '/'
    return long.startswith(short) or short == '/'


def _literalDisjoint(restA, restB):
    """同静态前缀下的尾部是否可证不相交：两边都以非通配符开头且首字符
    不同 → 命中集不重叠（modules/a*.py 与 modules/b*.py）。有一边以通配符
    开头就证不了 —— 按相交处理（宁多排队，不漏撞车）。"""
    if not restA or not restB:
        return False
    if restA[0] in '*?[' or restB[0] in '*?[':
        return False
    return restA[0] != restB[0]


def footprintsIntersect(patternA, patternB):
    """两个足迹 glob 是否可能命中同一文件（§2b 调度判定）。

    判定：相同；静态前缀互相包含（一方目录覆盖另一方文件）；前缀相同则
    看尾部是否可证不相交。fnmatch 的 * 跨分隔符，故 `a/*/b.py` 与
    `a/c/d/b.py` 算相交（宁可多排队，不漏判）。
    """
    a = str(patternA or '').strip().replace('\\', '/')
    b = str(patternB or '').strip().replace('\\', '/')
    if not a or not b:
        return False
    if a == b:
        return True
    headA, headB = _staticHead(a), _staticHead(b)
    if not (_pathPrefix(headA, headB) or _pathPrefix(headB, headA)):
        return False
    if headA != headB:
        return True         # 一方的静态段落在另一方通配区里：必相交
    return not _literalDisjoint(a[len(headA):], b[len(headB):])


def footprintHits(footprint, path):
    """path 是否落在 footprint 任一条目下（ glob 或目录前缀 ）。"""
    target = str(path or '').strip().replace('\\', '/')
    if not target:
        return False
    for pattern in footprint or []:
        pat = str(pattern or '').strip().replace('\\', '/')
        if not pat:
            continue
        if fnmatch.fnmatchcase(target, pat) or _pathPrefix(pat, target):
            return True
    return False


# ---------------------------------------------------------------- 后续批次接口

def startRelease(wi, deployCmd='', smokeCmd=''):
    """§4 留给后续批次：integrated → releasing 的迁移签名与 history 记录。

    v1 不启用（缺省 deployCmd/smokeCmd 为空即终态 integrated），这里只把
    形状定下来：迁移函数收部署/冒烟命令，history 记 release 条目。
    """
    if not deployCmd and not smokeCmd:
        return '未配置 deployCmd/smokeCmd：v1 到 integrated 即终态'
    if (wi.get('state') or '') != INTEGRATED:
        return '只有 integrated 才能进入发布'
    return ''
