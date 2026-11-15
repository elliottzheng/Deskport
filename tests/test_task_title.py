# -*- coding: utf-8 -*-
"""批次 T 任务标题（纯函数）：deriveTitle 规则表、显式标题归一、工作线标题组合。

不碰存储与 Flask——modules/task_title.py 是纯函数模块，规则由本文件钉死；
接口层（建卡/派活/改名/分享/老任务输出）在 test_task_title_api.py。
"""
import pytest

from modules import task_title


# ---------------------------------------------------------------- deriveTitle 规则表

@pytest.mark.parametrize('prompt,expected', [
    ('【目标】实施「批次 Q：主管记录 + 令牌绑定 + 任务自动记归属」。',
     '实施「批次 Q：主管记录 + 令牌绑定 + 任…'),
    ('只回答两个字：就绪。不要读写任何文件。', '只回答两个字：就绪'),
    ('# 整理简历\n把这份简历整理成一页纸', '整理简历'),
    ('【目标】\n独立验收批次 R', '独立验收批次 R'),
    ('目标：修复登录页的白屏问题，然后…', '修复登录页的白屏问题，然后…'),
    ('   ', '未命名任务'),
])
def test_derive_title_table(prompt, expected):
    """规格 §2 的例子（就是单测）：每一行原样钉死。"""
    assert task_title.deriveTitle(prompt) == expected


def test_derive_title_strips_combined_markers():
    """一类里多个标记连着也剥：`- 【任务】1. 修复白屏` → 修复白屏。"""
    assert task_title.deriveTitle('- 【任务】1. 修复白屏') == '修复白屏'


def test_derive_title_label_needs_two_to_four_chars():
    """6 字前缀加冒号不是标签（「只回答两个字：」不剥）。"""
    assert task_title.deriveTitle('任务说明啊：做吧') == '任务说明啊：做吧'


def test_derive_title_max_24_chars():
    """24 字原样；25 字截到 23 再加 …（中英文都算 1）。"""
    assert task_title.deriveTitle('字' * 24) == '字' * 24
    long = '字' * 25
    assert task_title.deriveTitle(long) == '字' * 23 + '…'
    assert len(task_title.deriveTitle(long)) == 24
    assert task_title.deriveTitle('a' * 30) == 'a' * 23 + '…'   # 英文同样按 1 字符


def test_derive_title_cuts_at_sentence_punct():
    """第一个句末标点处截断（不含标点）；问号/叹号/分号同样算。"""
    # 「做三件事：」是 4 字加冒号的标签 → 先剥掉，再在「。」处截断
    assert task_title.deriveTitle('做三件事：甲、乙、丙。然后收尾') \
        == '甲、乙、丙'
    assert task_title.deriveTitle('能做完吗？做不完就说') == '能做完吗'


def test_derive_title_all_lines_are_markers():
    """每行都只有标记 → 未命名任务（不是空串）。"""
    assert task_title.deriveTitle('【目标】\n【任务】') == '未命名任务'
    assert task_title.deriveTitle('') == '未命名任务'
    assert task_title.deriveTitle(None) == '未命名任务'


# ---------------------------------------------------------------- 归一与组合

def test_clean_title_normalizes():
    """显式标题：去首尾空白、换行折空格、超长截 40。"""
    assert task_title.cleanTitle('  验收 R  ') == '验收 R'
    assert task_title.cleanTitle('第一行\n第二行') == '第一行 第二行'
    assert task_title.cleanTitle('x' * 45) == 'x' * 40
    assert task_title.cleanTitle('   ') == ''


def test_resolve_title_given_and_derived():
    """给了用给的（given）；空/None 走推导（derived）。"""
    assert task_title.resolveTitle('验收 R', '别的提示词') \
        == ('验收 R', 'given')
    assert task_title.resolveTitle('  ', '【目标】做甲事。') \
        == ('做甲事', 'derived')
    assert task_title.resolveTitle(None, '【目标】做甲事。') \
        == ('做甲事', 'derived')


def test_resolve_title_workline_composition():
    """工作线标题规则：同名只留工作线标题；不同名加 · 连接且 ≤40。"""
    wl = {'title': '批次 T'}
    assert task_title.resolveTitle(None, '批次 T', wl) == ('批次 T', 'derived')
    assert task_title.resolveTitle(None, '【目标】改造标题。', wl) \
        == ('批次 T · 改造标题', 'derived')
    long = {'title': '线' * 40}
    title, source = task_title.resolveTitle(None, '改造标题。', long)
    assert len(title) == 40 and source == 'derived'
    assert task_title.resolveTitle('显式标题', '改造标题。', wl) \
        == ('显式标题', 'given')
