# -*- coding: utf-8 -*-
r"""审批边界判定器（R4-1/R4-2）：policy 校验与 evaluate 矩阵。

规格例子逐条回归：引号内的 \| 与 $( 不误判、重定向豁免、
deny 命中强制 ask、只读/allowPaths/allowCommands/default 各分支。
"""
import pytest

from modules import task_policy as tp


ALLOW = {'allowCommands': ['^grep\\b', '^head\\b', '^echo\\b', '^cat\\b',
                           '^cd\\b', '^curl\\b'],
         'denyCommands': ['6789'], 'default': 'ask'}


def exe(command):
    return 'execute', 'Bash', {'command': command}


# ---- 校验 ----

def test_validate_minimal_and_defaults():
    p = tp.validatePolicy({'allowCommands': ['^ls\\b']})
    assert p['default'] == 'ask' and p['allowPaths'] == []
    assert p['allowCommands'] == ['^ls\\b'] and p['timeoutSec'] is None
    assert tp.validatePolicy(None) == {}


@pytest.mark.parametrize('bad', [
    {'allowCommands': ['(unclosed']},
    {'denyCommands': ['[bad']},
    {'default': 'yolo'},
    {'allowPaths': 'E:\\x'},
    {'allowPaths': ['x'] * 21},
    {'timeoutSec': -1},
    {'timeoutSec': 'abc'},
])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        tp.validatePolicy(bad)


# ---- 规格例子 ----

def test_command_substitution_is_ask_even_inside_double_quotes():
    assert tp.evaluate(ALLOW, *exe('echo $(rm -rf x)'))[:2] == ('ask', '') or \
        tp.evaluate(ALLOW, *exe('echo $(rm -rf x)'))[0] == 'ask'
    assert tp.evaluate(ALLOW, *exe('echo `rm -rf x`'))[0] == 'ask'


def test_redirect_is_ask_but_exempt_forms_allow():
    assert tp.evaluate(ALLOW, *exe('cat a > b.py'))[0] == 'ask'
    assert tp.evaluate(ALLOW, *exe('grep -n "x" f 2>/dev/null | head'))[0] == 'allow'


def test_single_quoted_substitution_is_allowed():
    decision, rule = tp.evaluate(ALLOW, *exe("grep -n '$(x)' f"))
    assert decision == 'allow' and rule == 'allowCommands'


def test_piped_pipeline_with_quote_pipe_and_cd_segment():
    decision, rule = tp.evaluate(ALLOW, *exe('cd /tmp && grep -n "a\\|b" f | head'))
    assert decision == 'allow' and rule == 'allowCommands'


def test_deny_hit_forces_ask_even_if_allow_matches():
    decision, rule = tp.evaluate(ALLOW, *exe('curl http://127.0.0.1:6789/health'))
    assert decision == 'ask' and rule == 'deny:command'


# ---- 分支矩阵 ----

def test_readonly_tool_allows_without_command():
    assert tp.evaluate(ALLOW, 'read', 'Read', {'path': 'E:/a/f.py'})[0] == 'allow'
    assert tp.evaluate({}, 'read', 'Read', None)[0] == 'allow'
    assert tp.evaluate(ALLOW, '', 'Grep', {'pattern': 'x'})[0] == 'allow'


def test_write_inside_allow_paths_allows_outside_falls_to_default():
    p = {'allowPaths': ['E:\\事项\\远程\\web-platform-dev'], 'default': 'ask'}
    assert tp.evaluate(p, 'edit', 'Edit',
                       {'file_path': 'E:/事项/远程/web-platform-dev/app.py'}
                       )[0] == 'allow'
    assert tp.evaluate(p, 'edit', 'Edit',
                       {'file_path': 'E:/事项/其他/x.py'})[0] == 'ask'
    pDeny = dict(p, default='deny')
    assert tp.evaluate(pDeny, 'delete', 'Delete',
                       {'path': 'E:/别处/x'})[0] == 'deny'


def test_path_traversal_dotdot_escapes_allow_paths_is_ask():
    """R4-2 验收漏洞回归：`..` 折叠后越出 allowPaths 必须 ask（实际指向 prod）。"""
    p = {'allowPaths': ['E:\\事项\\远程\\web-platform-dev'], 'default': 'ask'}
    assert tp.evaluate(p, 'edit', 'Edit',
                       {'path': 'E:\\事项\\远程\\web-platform-dev\\..\\web-platform\\x.py'}
                       )[0] == 'ask'
    assert tp.evaluate(p, 'edit', 'Edit',
                       {'file_path': 'E:/事项/远程/web-platform-dev/../web-platform/x.py'}
                       )[0] == 'ask'
    # 目录边界语义保持：兄弟目录不因前缀相似而误放行
    assert tp.evaluate(p, 'edit', 'Edit',
                       {'path': 'E:\\事项\\远程\\web-platform-dev-evil\\x.py'}
                       )[0] == 'ask'
    # 折叠后仍在根内的 ../ 不受影响
    assert tp.evaluate(p, 'edit', 'Edit',
                       {'path': 'E:\\事项\\远程\\web-platform-dev\\modules\\..\\app.py'}
                       )[0] == 'allow'


def test_each_segment_must_match_allow_commands():
    assert tp.evaluate(ALLOW, *exe('grep -n foo bar'))[0] == 'allow'
    assert tp.evaluate(ALLOW, *exe('grep -n foo && rm -rf x'))[0] == 'ask'   # rm 无 allow 正则
    assert tp.evaluate(ALLOW, *exe('echo hi'))[0] == 'allow'


def test_no_policy_command_without_allow_regex_is_default():
    decision, _ = tp.evaluate({}, *exe('grep x'))
    assert decision == 'ask'


def test_default_allow_and_deny_branches():
    pAllow = {'default': 'allow'}
    assert tp.evaluate(pAllow, 'edit', 'Edit', None)[0] == 'allow'
    pDeny = {'default': 'deny'}
    assert tp.evaluate(pDeny, 'edit', 'Edit', None)[0] == 'deny'


def test_args_without_command_skip_command_dimension():
    """命令维度只在有命令文本时启用；Edit 无命令文本走路径分支。"""
    p = {'allowPaths': ['E:\\w'], 'allowCommands': ['^grep\\b'], 'default': 'ask'}
    assert tp.evaluate(p, 'edit', 'Edit', {'file_path': 'E:/w/a'})[0] == 'allow'
    assert tp.evaluate(p, 'edit', 'Edit', {'file_path': '/tmp/a'})[0] == 'ask'


# ---- R4-2b：真实链路形状回归（证据：prod 任务 4b1c65a28843 事件流）----

POLICY = {'allowPaths': ['e:/事项/远程/web-platform-dev'],
          'allowCommands': [r'\b(cat|grep|ls|git log|cd|echo|sed)\b'],
          'default': 'ask'}

# 真实事件形状：tool_call update 里 kind='edit'，request_permission 不带
# kind，args={'new_string','old_string','path'}（缺任何一环曾漏成人工审批）
REAL_EDIT_ARGS = {
    'new_string': '        lastSeq = task.get("seq", 0)\n        error = ...',
    'old_string': '        lastSeq = task.get("seq", 0)\n    return {...}',
    'path': 'E:\事项\远程\web-platform-dev\modules\tasks_api.py',
}


def test_real_edit_shape_kind_present():
    decision, rule = tp.evaluate(POLICY, 'edit', 'Edit', REAL_EDIT_ARGS)
    assert decision == 'allow' and rule == 'allowPaths'


def test_real_edit_shape_kind_missing_name_fallback():
    """request_permission.toolCall.kind 为空时按工具名 Edit 兜底。"""
    decision, rule = tp.evaluate(POLICY, '', 'Edit', REAL_EDIT_ARGS)
    assert decision == 'allow' and rule == 'allowPaths'


def test_write_outside_paths_still_ask():
    outside = dict(REAL_EDIT_ARGS, path='E:/事项/other/x.py')
    decision, _ = tp.evaluate(POLICY, '', 'Edit', outside)
    assert decision == 'ask'


# 重定向目标在 allowPaths 内 → 允许；相对路径按 cd 解析；解析不出 → ask

def test_redirect_append_inside_cwd_allows():
    cmd = ('cd "E:\事项\远程\web-platform-dev" && '
           "cat >> tests/test_x.py << 'EOF'\nhello\nEOF")
    decision, rule = tp.evaluate(POLICY, '', 'Bash',
                                          {'command': cmd}, cwd='')
    assert decision == 'allow' and rule == 'allowCommands'


def test_redirect_relative_no_cd_no_cwd_is_ask():
    decision, _ = tp.evaluate(POLICY, '', 'Bash',
                                       {'command': 'cat x > dev/内/文件'})
    assert decision == 'ask'


def test_redirect_outside_paths_is_ask():
    decision, rule = tp.evaluate(POLICY, '', 'Bash',
                                          {'command': 'cat x > /c/Windows/x'},
                                          cwd='E:\事项\远程\web-platform-dev')
    assert decision == 'ask' and rule == 'danger:redirect'


def test_redirect_posix_cwd_and_target_resolves():
    cmd = "cd /e/事项/远程/web-platform-dev && echo hi > notes/a.md"
    decision, rule = tp.evaluate(POLICY, '', 'Bash', {'command': cmd})
    assert decision == 'allow' and rule == 'allowCommands'


def test_substitution_still_ask_with_redirect():
    cmd = 'cd "E:\事项\远程\web-platform-dev" && echo $(pwd) > out.txt'
    decision, _ = tp.evaluate(POLICY, '', 'Bash', {'command': cmd})
    assert decision == 'ask'
