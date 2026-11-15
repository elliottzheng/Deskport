# -*- coding: utf-8 -*-
"""结构化结果核心（批次 G1）：expects 归一、提交体 schema 校验、结果文本、
提交（存储 + 事件 + 主管收件箱）、联邦镜像与自动追问判定。

交付形态（任务建卡时的 expects，POST /api/tasks 与技能包 --expects 传入）：
- impl：实施方交付——summary / commit / branch / tests / gates / risks / questions
- verify：验收方交付——verdict / target / mustFix / suggest / evidence / unverified
- 无 expects：只有 summary 必填，其余自由 data（≤8KB）

存储：任务快照只存最新结果的精简版（task['result']，resultCount 记提交次数）；
完整历史作为 type='result' 事件进 jsonl（关键变迁，append_event 即落盘）。
同一任务可多次提交：校验失败逐字段报错（执行方可重交），最新一次为准。

MCP 面（工具 schema / 会话令牌 / 注入）在 modules/task_result_mcp.py；本模块
不 import Flask，两侧（HTTP / MCP / runner）共用同一份校验与提交规则。
"""
import json
import logging
import re

from modules import leads_inbox
from modules import tasks_store as store

log = logging.getLogger('task-result')

EXPECTS = ('impl', 'verify')
SUMMARY_MAX = 300        # summary 上限（两种 expects 同）
RISK_MAX = 200           # impl.risks 每条上限
ITEM_MAX = 300           # 其余清单条目上限（evidence 规格即 300）
DATA_MAX = 8192          # 无 expects 时自由 data 的序列化上限
BRANCH_MAX = 100
COMMIT_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')
VERDICTS = ('pass', 'rework', 'needs_judgment')
GATE_VALUES = ('pass', 'fail')
NUDGE_BY = '平台追发'

# 固定追问文案（规格 §4）：每个任务只自动追一次
NUDGE_TEXT = ('本任务需要用 submit_result 提交结构化结果（expects=%s）。'
              '请现在调用 submit_result 工具提交；若会话里没有该工具，用技能包 '
              'deskport.py result --task <任务id> --json <文件> 提交。'
              '自由文本报告只作补充，不算交付。')

# 字段表：(名字, 类型, 必填, 上限)。类型驱动校验、联邦镜像取键两处，
# 规则只有这一份（MCP inputSchema 在 task_result_mcp，同源生成）。
_FIELDS = {
    'impl': (
        ('summary', 'str', True, SUMMARY_MAX),
        ('commit', 'commit', True, 0),
        ('branch', 'str', False, BRANCH_MAX),
        ('tests', 'tests', True, 0),
        ('gates', 'gates', False, 0),
        ('risks', 'strlist', False, RISK_MAX),
        ('questions', 'strlist', False, ITEM_MAX),
    ),
    'verify': (
        ('summary', 'str', False, SUMMARY_MAX),   # 规格未列；收件箱/卡片要一行摘要，收可选
        ('verdict', 'verdict', True, 0),
        ('target', 'commit', True, 0),
        ('mustFix', 'strlist', False, ITEM_MAX),
        ('suggest', 'strlist', False, ITEM_MAX),
        ('evidence', 'strlist', False, ITEM_MAX),
        ('unverified', 'strlist', False, ITEM_MAX),
    ),
}


# ---------------------------------------------------------------- 入口校验

def normalizeExpects(raw):
    """建卡时的 expects 归一：'' / None / 'impl' / 'verify'；其它值报错。"""
    value = str(raw or '').strip().lower()
    if not value:
        return '', ''
    if value not in EXPECTS:
        return '', 'expects 只能是 impl / verify（或留空）'
    return value, ''


def fieldNames(expects):
    """该 expects 的字段名清单（联邦镜像取键用）。"""
    if expects in _FIELDS:
        return [f for f, _k, _r, _l in _FIELDS[expects]]
    return ['summary', 'data']


def _cleanStr(value, field, required, limit, errors):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors[field] = '%s 必填' % field
        return ''
    if not isinstance(value, str):
        errors[field] = '%s 需为字符串' % field
        return ''
    text = value.strip()
    if limit and len(text) > limit:
        errors[field] = '%s 超长（上限 %d 字，当前 %d 字）' % (field, limit, len(text))
        return ''
    return text


def _cleanCommit(value, field, required, errors):
    text = _cleanStr(value, field, required, 0, errors)
    if text and not COMMIT_RE.match(text):
        errors[field] = '%s 需为 7–40 位十六进制（git commit）' % field
        return ''
    return text


def _cleanInt(value, field, errors):
    if isinstance(value, bool) or not isinstance(value, int):
        errors[field] = '%s 需为整数' % field
        return 0
    if value < 0:
        errors[field] = '%s 不能为负' % field
        return 0
    return value


def _cleanTests(value, errors):
    tests = {'passed': 0, 'failed': 0, 'skipped': 0}
    if not isinstance(value, dict):
        errors['tests'] = 'tests 必填：{passed, failed, skipped}（整数）'
        return tests
    for key in tests:
        if key not in value:
            errors['tests.%s' % key] = 'tests.%s 必填（整数）' % key
        else:
            tests[key] = _cleanInt(value[key], 'tests.%s' % key, errors)
    return tests


def _cleanGates(value, errors):
    gates = {}
    if value is None:
        return gates
    if not isinstance(value, dict):
        errors['gates'] = 'gates 需为对象：{structure, frontend}，取值 pass/fail'
        return gates
    for key in ('structure', 'frontend'):
        item = value.get(key)
        if item is None:
            continue
        if item not in GATE_VALUES:
            errors['gates.%s' % key] = 'gates.%s 只能是 pass / fail' % key
            continue
        gates[key] = item
    return gates


def _cleanStrList(value, field, limit, errors):
    if value is None:
        return []
    if not isinstance(value, list):
        errors[field] = '%s 需为字符串数组' % field
        return []
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors['%s[%d]' % (field, i)] = '%s[%d] 需为非空字符串' % (field, i)
            continue
        text = item.strip()
        if limit and len(text) > limit:
            errors['%s[%d]' % (field, i)] = '%s[%d] 超长（上限 %d 字）' % (field, i, limit)
            continue
        out.append(text)
    return out


def _cleanVerdict(value, errors):
    text = _cleanStr(value, 'verdict', True, 0, errors)
    if text and text not in VERDICTS:
        errors['verdict'] = 'verdict 只能是 pass / rework / needs_judgment'
        return ''
    return text


def validate(data, expects):
    """按 expects 校验提交体。返回 (clean, errors)：errors 为空才收。"""
    errors = {}
    if not isinstance(data, dict):
        return {}, {'body': '请求体必须是 JSON 对象'}
    if expects not in _FIELDS:
        # 无 expects：summary 必填 + 自由 data（≤8KB）
        clean = {'summary': _cleanStr(data.get('summary'), 'summary',
                                      True, SUMMARY_MAX, errors)}
        raw = data.get('data')
        if raw is not None:
            if not isinstance(raw, dict):
                errors['data'] = 'data 需为 JSON 对象'
            else:
                blob = json.dumps(raw, ensure_ascii=False)
                if len(blob.encode('utf-8')) > DATA_MAX:
                    errors['data'] = 'data 超过 %d 字节上限' % DATA_MAX
                else:
                    clean['data'] = raw
        return clean, errors
    clean = {}
    for field, kind, required, limit in _FIELDS[expects]:
        value = data.get(field)
        if kind == 'str':
            clean[field] = _cleanStr(value, field, required, limit, errors)
        elif kind == 'commit':
            clean[field] = _cleanCommit(value, field, required, errors)
        elif kind == 'verdict':
            clean[field] = _cleanVerdict(value, errors)
        elif kind == 'tests':
            clean[field] = _cleanTests(value, errors)
        elif kind == 'gates':
            clean[field] = _cleanGates(value, errors)
        elif kind == 'strlist':
            clean[field] = _cleanStrList(value, field, limit, errors)
    return clean, errors


# ---------------------------------------------------------------- 呈现

def needsJudgment(clean, expects):
    """本次结果是否需要主管判断（impl 的 questions / verify 的 needs_judgment）。"""
    if expects == 'impl':
        return bool(clean.get('questions'))
    if expects == 'verify':
        return clean.get('verdict') == 'needs_judgment'
    return False


def resultText(clean, expects):
    """事件流与收件箱共用的一行摘要。"""
    lines = (clean.get('summary') or '').splitlines()
    summary = (lines[0] if lines else '')[:120]
    if expects == 'impl':
        tests = clean.get('tests') or {}
        bits = ['commit %s' % (clean.get('commit') or '')[:12],
                '测试 %d过/%d败/%d跳过' % (tests.get('passed', 0),
                                          tests.get('failed', 0),
                                          tests.get('skipped', 0))]
        gates = clean.get('gates') or {}
        if gates:
            bits.append('门禁 ' + '/'.join('%s=%s' % kv for kv in sorted(gates.items())))
        if clean.get('risks'):
            bits.append('风险 %d 条' % len(clean['risks']))
        return '实施结果：%s（%s）' % (summary, ' · '.join(bits))
    if expects == 'verify':
        bits = ['verdict=%s' % (clean.get('verdict') or ''),
                'target %s' % (clean.get('target') or '')[:12]]
        if clean.get('mustFix'):
            bits.append('mustFix %d 条' % len(clean['mustFix']))
        return '验收结果：%s（%s）' % (summary, ' · '.join(bits))
    return '结果：%s' % summary


def inboxText(clean, expects):
    """主管收件箱条目文本（≤300，含 verdict/commit/summary）。"""
    text = resultText(clean, expects)
    if needsJudgment(clean, expects):
        if expects == 'impl':
            text += '；待判断：' + '；'.join(clean.get('questions') or [])[:120]
        else:
            text += '；需主管判断后流转'
    return text[:300]


def latestFor(task):
    """GET /api/tasks/<id> 的 result 与 resultCount。"""
    return task.get('result') or None, int(task.get('resultCount') or 0)


# ---------------------------------------------------------------- 提交核心

def _storeResult(task, clean, expects, via):
    """落最新精简结果 + resultCount + 一条 result 事件（关键变迁，立即落盘）。"""
    brief = dict(clean)
    brief['expects'] = expects
    brief['via'] = via
    brief['at'] = store._now()
    with store.repo.lock:
        task['result'] = brief
        task['resultCount'] = int(task.get('resultCount') or 0) + 1
        count = task['resultCount']
        store.repo.save()
    store.repo.append_event(task, 'result', text=resultText(clean, expects),
                            via=via, **clean)
    return brief, count


def submit(task, data, via='http'):
    """校验并落一次结果。返回 (payload, status)：400 带逐字段 errors。"""
    expects = task.get('expects') or ''
    clean, errors = validate(data, expects)
    if errors:
        return ({'error': '结果不符合要求，请按字段改后重交', 'fields': errors}), 400
    brief, count = _storeResult(task, clean, expects, via)
    kind = 'task_judgment' if needsJudgment(clean, expects) else 'task_result'
    leads_inbox.emit(task, kind, inboxText(clean, expects))
    log.info('result submitted task=%s expects=%s via=%s count=%d',
             task.get('id'), expects or '-', via, count)
    return ({'ok': True, 'result': brief, 'resultCount': count}), 200


def adoptMirrored(task, event):
    """联邦镜像：对端（同版本成员中枢）的 result 事件照搬成本任务结果。

    对端已用同一份 schema 校验过，这里只做取键与落库，不重复校验；
    via='federation' 供追溯。
    """
    expects = task.get('expects') or ''
    clean = {k: event[k] for k in fieldNames(expects) if k in event}
    if not clean:
        return False
    _storeResult(task, clean, expects, 'federation')
    kind = 'task_judgment' if needsJudgment(clean, expects) else 'task_result'
    leads_inbox.emit(task, kind, inboxText(clean, expects))
    return True


# ---------------------------------------------------------------- 自动追问

def nudgeText(task):
    """轮末追问：有 expects、还没有结果、没追过 → 返回固定文案（并标记已追）。

    有结果 / 无 expects / 已追过 → None（调用方据此走 result_missing 判定）。
    """
    if not task.get('expects') or task.get('result') \
            or task.get('resultNudged'):
        return None
    task['resultNudged'] = True
    store.repo.touch()   # 非关键标记：随轮末 WAITING 变迁的 save 一起落盘
    return NUDGE_TEXT % (task.get('expects') or '')


def noteMissing(task):
    """追过一次仍没有结果 → 主管收件箱 result_missing（需要判断），只投一次。"""
    if not task.get('expects') or task.get('result') \
            or not task.get('resultNudged') or task.get('resultMissing'):
        return
    task['resultMissing'] = True
    store.repo.touch()
    leads_inbox.emit(task, 'result_missing',
                     '任务 %s（expects=%s）未提交结构化结果：已追发一次仍无提交，'
                     '请主管判断（催办 / 关闭 / 接手）'
                     % (task.get('id') or '', task.get('expects')))
