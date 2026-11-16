# -*- coding: utf-8 -*-
"""批次 G2 段 1：工作项状态机——每条迁移、创建校验、attempt 上限、足迹相交。

纯函数单测（不建存储、不起 app）：workitem_state 是迁移与校验的唯一出处，
流水线/API 只消费它。非法迁移必须被拒（返回错误串、不写 history）——流水线
最怕"状态悄悄往前走一步"。
"""
import pytest

from modules import workitem_state as ws


def newWi(**kw):
    wi = {'id': 'wi-test01', 'state': ws.QUEUED, 'attempt': 0,
          'maxAttempts': 3, 'history': []}
    wi.update(kw)
    return wi


# ---------------------------------------------------------------- 迁移表

def test_every_legal_transition():
    """规格 §5：每条迁移都走得通，且写 history（时间/原因/关联）。"""
    edges = [
        (ws.QUEUED, ws.IMPLEMENTING, 'start', {'task': 't1'}),
        (ws.IMPLEMENTING, ws.VERIFYING, 'impl delivered', {'task': 't1'}),
        (ws.VERIFYING, ws.INTEGRATING, 'verdict pass', {'task': 't2'}),
        (ws.INTEGRATING, ws.INTEGRATED, 'gates+tests green', {'commit': 'abc1234'}),
        # rework 回同一实施会话：verifying → implementing
        (ws.VERIFYING, ws.IMPLEMENTING, 'rework mustFix', {'task': 't1'}),
        # 集成时 main 又动了：回到同步路径（implementing）
        (ws.INTEGRATING, ws.IMPLEMENTING, 'sync conflict', {}),
        # 旁路
        (ws.IMPLEMENTING, ws.NEEDS_JUDGMENT, 'questions', {}),
        (ws.VERIFYING, ws.NEEDS_JUDGMENT, 'verdict needs_judgment', {}),
        (ws.QUEUED, ws.NEEDS_JUDGMENT, 'footprint conflict unresolved', {}),
        (ws.NEEDS_JUDGMENT, ws.IMPLEMENTING, 'lead continue', {}),
        (ws.NEEDS_JUDGMENT, ws.CANCELLED, 'lead cancel', {}),
        (ws.IMPLEMENTING, ws.FAILED, 'unrecoverable', {}),
        (ws.FAILED, ws.CANCELLED, 'cleanup', {}),
        (ws.INTEGRATING, ws.CANCELLED, 'lead cancel', {}),
    ]
    wi = newWi()
    for src, dst, reason, ref in edges:
        wi['state'] = src
        err = ws.transition(wi, dst, reason, **ref)
        assert err == '', '%s -> %s 应合法: %s' % (src, dst, err)
        assert wi['state'] == dst
        entry = wi['history'][-1]
        assert entry['from'] == src and entry['to'] == dst
        assert entry['reason'] == reason and entry['at'] > 0
        for key, value in ref.items():
            assert entry.get(key) == value


def test_illegal_transitions_rejected():
    """非法迁移：不改状态、不写 history、返回错误串。"""
    cases = [
        (ws.QUEUED, ws.VERIFYING),          # 没实施不能验收
        (ws.QUEUED, ws.INTEGRATING),
        (ws.IMPLEMENTING, ws.INTEGRATING),  # 没验收不能集成
        (ws.IMPLEMENTING, ws.INTEGRATED),
        (ws.VERIFYING, ws.INTEGRATED),
        (ws.INTEGRATED, ws.IMPLEMENTING),   # 终态
        (ws.CANCELLED, ws.IMPLEMENTING),
        (ws.FAILED, ws.IMPLEMENTING),       # failed 只允许 cancelled 收尾
    ]
    for src, dst in cases:
        wi = newWi(state=src)
        before = len(wi['history'])
        err = ws.transition(wi, dst, '越级')
        assert err, '%s -> %s 应被拒' % (src, dst)
        assert wi['state'] == src
        assert len(wi['history']) == before


def test_same_state_is_noop():
    wi = newWi(state=ws.IMPLEMENTING)
    assert ws.transition(wi, ws.IMPLEMENTING, 'same') == ''
    assert wi['history'] == []


def test_history_capped():
    wi = newWi()
    for i in range(ws.HISTORY_MAX + 30):
        ws.transition(wi, ws.IMPLEMENTING if i % 2 else ws.VERIFYING, 'x')
    assert len(wi['history']) == ws.HISTORY_MAX


def test_stage_of():
    assert ws.stageOf(ws.IMPLEMENTING) == 'impl'
    assert ws.stageOf(ws.VERIFYING) == 'verify'
    assert ws.stageOf(ws.INTEGRATING) == 'integrate'
    assert ws.stageOf(ws.INTEGRATED) == 'done'
    assert ws.stageOf(ws.NEEDS_JUDGMENT) == 'judge'
    assert ws.stageOf(ws.QUEUED) == 'queue'


# ---------------------------------------------------------------- 创建校验

def test_create_ok():
    clean, errors = ws.validateCreate({
        'title': '工作项存储', 'specPath': 'E:/x.md',
        'implModel': 'kimi-k2', 'verifyModel': 'kimi-k2-thinking',
        'footprint': ['modules/*.py'], 'maxAttempts': 3})
    assert errors == {}
    assert clean['implModel'] != clean['verifyModel']
    assert clean['footprint'] == ['modules/*.py']
    assert clean['maxAttempts'] == 3


def test_create_rejects_same_models():
    """规格 §1/§5：verifyModel 必须与 implModel 不同，创建时校验。"""
    _clean, errors = ws.validateCreate({
        'specText': '做 X', 'implModel': 'm1', 'verifyModel': 'm1',
        'footprint': ['a.py']})
    assert 'verifyModel' in errors
    assert '不同' in errors['verifyModel']


def test_create_rejects_missing_models_and_spec():
    _clean, errors = ws.validateCreate({'footprint': ['a.py']})
    assert 'implModel' in errors and 'verifyModel' in errors
    assert 'spec' in errors          # specPath/specText 二选一都没给
    _clean, errors = ws.validateCreate({'specPath': 'a.md', 'implModel': 'm',
                                        'verifyModel': 'n', 'footprint': ['a']})
    assert errors == {}                # 只传 specPath：正常
    _clean, errors = ws.validateCreate({'specPath': 'a.md', 'specText': '全文',
                                        'implModel': 'm', 'verifyModel': 'n',
                                        'footprint': ['a']})
    assert 'spec' in errors          # 两个都给了也不行
    _clean, errors = ws.validateCreate({'specPath': 'a.md', 'implModel': 'm',
                                        'verifyModel': 'n'})
    assert 'footprint' in errors     # §2b：足迹必须声明


def test_create_validates_limits():
    _c, errors = ws.validateCreate({'title': 'x' * 41, 'specPath': 'a',
                                    'implModel': 'm', 'verifyModel': 'n',
                                    'footprint': ['a']})
    assert 'title' in errors
    _c, errors = ws.validateCreate({'specText': 'x' * 8001, 'implModel': 'm',
                                    'verifyModel': 'n', 'footprint': ['a']})
    assert 'specText' in errors
    _c, errors = ws.validateCreate({'specPath': 'a', 'implModel': 'm',
                                    'verifyModel': 'n', 'footprint': ['a'],
                                    'maxAttempts': 99})
    assert 'maxAttempts' in errors
    _c, errors = ws.validateCreate({'specPath': 'a', 'implModel': 'm',
                                    'verifyModel': 'n',
                                    'footprint': ['x' * 201]})
    assert 'footprint[0]' in errors


# ---------------------------------------------------------------- attempt 上限

def test_attempt_cap_enters_judgment():
    """规格 §5：attempt 上限 → needs_judgment（流水线据此停返工）。"""
    wi = newWi(state=ws.VERIFYING, attempt=2, maxAttempts=3)
    assert ws.canRework(wi)            # 第 3 次（attempt=2 → 3）还可返
    wi['attempt'] = 3
    assert not ws.canRework(wi)        # 到顶：流水线转 needs_judgment


# ---------------------------------------------------------------- 足迹（§2b）

@pytest.mark.parametrize('a,b,hit', [
    ('modules/*.py', 'modules/workitem_*.py', True),     # 前缀包含
    ('modules/', 'modules/workitem_store.py', True),     # 目录覆盖文件
    ('modules/workitem_store.py', 'modules/workitem_store.py', True),
    ('README.md', 'README.md', True),
    ('modules/*.py', 'frontend/*', False),
    ('modules/*.py', 'README.md', False),
    ('frontend/assets/js/*.js', 'modules/*.py', False),
    ('tools/run_tests.py', 'tools/*.py', True),
    ('', 'modules/*.py', False),                          # 空足迹不判相交
    ('a/*/b.py', 'a/c/b.py', True),                     # 中间通配
    ('a/*/b.py', 'a/c/d/b.py', True),                   # fnmatch 的 * 跨分隔符
    ('modules/a*.py', 'modules/b*.py', False),          # 同前缀、可证不相交
    ('modules/workitem_*.py', 'modules/task_*.py', False),
    ('modules/a*.py', 'modules/a?*.py', True),          # 证不了不相交 → 按相交
])
def test_footprints_intersect(a, b, hit):
    assert ws.footprintsIntersect(a, b) is hit


def test_footprint_hits():
    fp = ['modules/*.py', 'docs/']
    assert ws.footprintHits(fp, 'modules/workitem_store.py')
    assert ws.footprintHits(fp, 'docs/api.md')
    assert not ws.footprintHits(fp, 'frontend/tasks.html')
    assert not ws.footprintHits(fp, '')


def test_footprint_normalizes_backslash():
    assert ws.footprintsIntersect('modules\\*.py', 'modules/workitem_*.py')


# ---------------------------------------------------------------- 后续批次接口

def test_start_release_signature_only():
    """§4：只留口——没配 deployCmd/smokeCmd 时拒绝进入发布。"""
    wi = newWi(state=ws.INTEGRATED)
    assert ws.startRelease(wi, '', '') != ''
    assert ws.startRelease(wi, 'deploy.cmd', 'smoke.cmd') == ''
    assert wi['state'] == ws.INTEGRATED        # v1 不真的迁
