# -*- coding: utf-8 -*-
"""tasks_store 纯函数回归：拆分 store/runner/api 后行为不变。"""
import json
import pathlib
import subprocess
import sys

from modules import tasks_store as store


def test_ps2_atexit_flushes_repos_created_directly(tmp_path):
    """PS2：直接 new 的 TaskRepository（非模块级单例）正常退出时，最后一拍
    非关键变更也落盘——atexit 冲全部已注册仓库，不只模块级单例（生产只用
    单例不受影响；测试/脚本里 new 的仓库原来会丢这一拍）。"""
    storePath = tmp_path / 'agent_tasks.json'
    logDir = tmp_path / 'logs'
    logDir.mkdir()
    worker = tmp_path / 'ps2_worker.py'
    worker.write_text(
        'import sys\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'import config\n'
        'config.stateDir = lambda: sys.argv[2]\n'
        'from modules import tasks_store\n'
        'repo = tasks_store.TaskRepository(sys.argv[3], sys.argv[4])\n'
        "task = {'id': 't-ps2', 'title': '退出前最后一拍', 'status': 'waiting'}\n"
        'repo.put(task)\n'
        "repo.append_event(task, 'assistant', text='只置脏，等退出 flush')\n",
        encoding='utf-8')
    proc = subprocess.run(
        [sys.executable, str(worker),
         str(pathlib.Path(__file__).resolve().parents[1]),
         str(tmp_path), str(storePath), str(logDir)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert storePath.is_file()               # 没有 atexit 兜底这文件不存在
    saved = json.loads(storePath.read_text(encoding='utf-8'))
    assert saved['t-ps2']['lastText'] == '只置脏，等退出 flush'


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