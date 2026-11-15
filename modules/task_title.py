# -*- coding: utf-8 -*-
"""任务标题（批次 T）：显式标题归一、提示词默认标题推导、输出用标题。

纯函数模块（不 import Flask / 不碰存储）：tasks_api 建任务时定标题，
tasks_store 摘要输出时给老任务补推标题，规则由 tests/test_task_title.py 钉死。

默认标题规则（规格 §2）：
    取第一行非空文本 → 去掉开头的标记（【…】[…]/ Markdown #-*> / 序号
    1. 1、 / 2–4 字加冒号的标签）→ 在第一个句末标点处截断 → 首行只有标记
    则取下一行 → 最长 24 字（超出截到 23 再加 …）→ 全空用「未命名任务」。
"""
import re

MAX_GIVEN = 40          # 显式标题上限（规格：1–40 字，超长截断）
MAX_DERIVED = 24        # 默认标题上限（按字符算，中英文都算 1）
ELLIPSIS = '…'
UNNAMED = '未命名任务'
SENTENCE_END = '。！？；!?;'
SEP = ' · '             # 工作线标题与默认标题的连接符

# 开头的标记（一个或多个，反复剥）：【…】/ […] / Markdown # * > - /
# 序号 1. 1、 / 2–4 字加冒号的标签（「目标：」「任务：」这类）/ 空白。
# 标签的字数类排除句末标点与冒号本身，「只回答两个字：」这种 6 字前缀不会误剥。
_MARKER = re.compile(
    r'^(?:'
    r'【[^【】]*】'
    r'|\[[^\[\]]*\]'
    r'|[#>*\-]+'
    r'|\d+[.、]'
    r'|[^\s:：。！？；!?;\[\]【】#>*\-]{2,4}[：:]'
    r'|[\s\u3000]+'
    r')+'
)


def cleanTitle(raw):
    """显式标题归一：去首尾空白、换行折成空格、超长截到 40。

    空（或只有空白）返回 ''——调用方按「没给标题」走默认规则。
    """
    title = re.sub(r'\s+', ' ', str(raw or '')).strip()
    return title[:MAX_GIVEN]


def _cutSentence(body):
    """在第一个句末标点处截断（不含标点本身）。"""
    for i, ch in enumerate(body):
        if ch in SENTENCE_END:
            return body[:i].strip()
    return body


def deriveTitle(prompt):
    """从提示词推默认标题（规则见模块 docstring）；全空返回「未命名任务」。"""
    for line in str(prompt or '').splitlines():
        body = _cutSentence(_MARKER.sub('', line).strip())
        if body:
            return body[:MAX_DERIVED - 1] + ELLIPSIS \
                if len(body) > MAX_DERIVED else body
    return UNNAMED


def worklineTitle(wlTitle, derived):
    """工作线派活的标题：`<工作线标题>`；与默认标题不同则
    `<工作线标题> · <默认标题>`，总长仍 ≤ 40。返回 (title, source)。"""
    name = str(wlTitle or '').strip()
    if not name:
        return derived, 'derived'
    if not derived or derived == name or derived == UNNAMED:
        return name[:MAX_GIVEN], 'derived'
    combined = (name + SEP + derived)[:MAX_GIVEN]
    return combined, 'derived'


def resolveTitle(given, prompt, workline=None):
    """建任务时定标题。返回 (title, titleSource)。

    given 非空 = 显式标题（given）；否则按提示词推默认标题（derived）——
    带工作线时套工作线标题规则（见 worklineTitle）。
    """
    if given is not None and str(given).strip():
        return cleanTitle(given), 'given'
    derived = deriveTitle(prompt)
    if workline is not None:
        return worklineTitle((workline or {}).get('title'), derived)
    return derived, 'derived'


def titleFor(task):
    """摘要输出用标题：有 titleSource 原样用存储标题；老任务（无该键）
    按新规则从 prompt 推导——只改输出，不改存储。"""
    task = task or {}
    if task.get('titleSource'):
        return task.get('title') or ''
    return deriveTitle(task.get('prompt') or '') or (task.get('title') or '')
