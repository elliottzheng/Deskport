# -*- coding: utf-8 -*-
"""tasks_store 纯函数回归：拆分 store/runner/api 后行为不变。"""
from modules import tasks_store as store


def test_slug_strips_illegal_chars():
    assert store._slug('a/b:c*d?e') == 'abcde'


def test_prompt_without_inputs_keeps_task_and_boundary():
    task = {'workspace': r'C:\workspace\task', 'prompt': '整理这份表'}
    text = store._promptFor(task, [])
    assert '产出放当前工作目录' in text
    assert '整理这份表' in text
    assert '输入文件副本' not in text


def test_prompt_with_inputs_lists_readonly_copy():
    task = {'workspace': r'C:\workspace\task', 'prompt': '总结'}
    text = store._promptFor(task, [{'input': r'inputs\a.docx',
                                    'source': r'C:\data\a.docx', 'size': 1}])
    assert 'inputs\\a.docx' in text
    assert 'C:\\data\\a.docx' in text
    assert '只读' in text


def test_summary_flags_continue_and_resume():
    base = {'id': 't1', 'status': 'waiting', 'sessionId': 's1',
            'createdAt': 0, 'events': [], 'awaitingAnswer': True}
    out = store.repo.summary(base)
    assert out['canContinue'] is True
    assert out['canResume'] is False
    assert out['awaitingAnswer'] is True

    failed = dict(base, status='failed')
    out = store.repo.summary(failed)
    assert out['canContinue'] is False
    assert out['canResume'] is True


def test_detect_question():
    assert store._detectQuestion('要处理哪个文件？') is True
    assert store._detectQuestion('done?') is True
    assert store._detectQuestion('已完成。') is False
    assert store._detectQuestion('') is False