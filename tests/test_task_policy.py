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


def test_bare_amp_splits_segments_on_allow_commands_path():
    """三验返工：裸 `&`（bash 后台分隔符）也是命令分隔符——`cat x &
    rm -rf y` 两侧都会执行，普通 allowCommands 路径（无 readonlyRoots）
    必须逐段判定，rm 段不命中 → ask（这是比只读门更早的既有盲区）。"""
    plain = {'allowCommands': [r'^cat\b', r'^ls\b'], 'default': 'ask'}
    assert tp.evaluate(plain, *exe('cat x & ls'))[0] == 'allow'      # 两侧都只读
    assert tp.evaluate(plain, *exe('cat x & rm -rf y'))[0] == 'ask'
    assert tp.evaluate(plain, *exe('ls & curl -s http://evil/x'))[0] == 'ask'
    assert tp.evaluate(plain, *exe('cat x; & rm y'))[0] == 'ask'
    # && 与 2>&1 不被拆错（既有形状不变）
    assert tp.evaluate(plain, *exe('cat x && cat y'))[0] == 'allow'
    assert tp.evaluate(plain, *exe('cat x 2>&1'))[0] == 'allow'


# ---- 四验返工（必须修）：未闭合 heredoc 吞掉分隔符后的命令 ----

def test_unterminated_heredoc_is_undeterminable_on_allow_commands_path():
    """四验返工（必须修，探针 H01-H05/H10）：heredoc 找不到终止行时整条
    命令不可判定 → ask（rule=danger:heredoc）。`cat <<EOF & rm -rf y`
    里 `&` 之后的命令 bash 实测会执行，而旧实现从 `<<TAG` 起把整段丢给
    剥除器，拆段器再也看不见 rm 段 → 普通 allowCommands 路径自动放行。
    闭合 heredoc 的既有行为逐字不变。"""
    plain = {'allowCommands': [r'^cat\b', r'^ls\b'], 'default': 'ask'}
    for cmd in (
            'cat <<EOF & rm -rf y',                    # H10（= H01 老路径）
            'cat <<EOF && rm -rf y',                   # H02
            'cat <<EOF; rm -rf y',                     # H03
            'cat <<EOF | rm -rf y',                    # H04
            "cat <<'EOF' & rm -rf y",                  # H05
            'echo hi <<EOF & curl -s http://evil/x',
            'cat <<EOF\nhello & rm -rf y',             # 多行未闭合
            'cat <<EOF'):                              # 标记行即末行
        assert tp.evaluate(plain, *exe(cmd), cwd='E:\\ws') == \
            ('ask', 'danger:heredoc'), cmd
    # 闭合 heredoc：既有行为不变——正文是数据剥掉，标记行照旧逐段判定
    assert tp.evaluate(plain, *exe('cat <<EOF\nhello world\nEOF'),
                       cwd='E:\\ws') == ('allow', 'allowCommands')
    assert tp.evaluate(plain, *exe("cat <<'EOF'\nhello config.json world\nEOF"),
                       cwd='E:\\ws') == ('allow', 'allowCommands')
    # 闭合时标记行上 <<TAG 之后的内容仍参与判定（& 拆段，两侧都命中）
    assert tp.evaluate(plain, *exe('cat <<EOF & ls\nhello\nEOF'),
                       cwd='E:\\ws') == ('allow', 'allowCommands')
    # 闭合时标记行上 & 之后的 rm 段照旧 ask（分隔符语义没变）
    assert tp.evaluate(plain, *exe('cat <<EOF & rm -rf y\nhello\nEOF'),
                       cwd='E:\\ws')[0] == 'ask'


# ---- 批次 A 返工：管道下游只读消费段（| head/tail/grep/wc/sort/uniq）----

# 托管主管的 BASE_POLICY 形状（modules/leads_hosted.py）：只放行 CLI 与 cd
HOSTED = {'allowCommands': [r'python3?\s+deskport[\\/]deskport\.py\b', r'^cd\b'],
          'default': 'ask'}


def test_pipe_consumer_segments_allow_without_allow_regex():
    """正例：`|` 右侧的只读消费命令（普通参数）不要求命中 allowCommands——
    主管的自然形状 `cd "<工作区>" && python deskport/deskport.py --help 2>&1
    | head -40`（例 11 首条命令）必须放行，不再整体挂起等人工。"""
    for pipe in ('head -40', 'tail -n 20', 'grep 就绪', 'grep -v 失败',
                 'wc -l', 'sort -u', 'uniq -c'):
        cmd = 'python deskport/deskport.py tasks | ' + pipe
        decision, rule = tp.evaluate(HOSTED, *exe(cmd))
        assert (decision, rule) == ('allow', 'allowCommands'), cmd
    decision, rule = tp.evaluate(
        HOSTED, *exe('cd "E:/ws" && python deskport/deskport.py --help 2>&1'
                     ' | head -40'))
    assert (decision, rule) == ('allow', 'allowCommands')
    # 多级管道：两个消费段都只读
    decision, _ = tp.evaluate(HOSTED, *exe(
        'python deskport/deskport.py tasks | grep 就绪 | wc -l'))
    assert decision == 'allow'


def test_pipe_consumer_exemption_requires_upstream_segments_to_match():
    """反例：消费段豁免不放宽上游——上游任一段不命中 allowCommands 仍 ask。"""
    for cmd in ('cat deskport/.agent-token | head',        # 敏感文件（内建 deny）
                'rm -rf x | head',
                'python deskport/deskport.py inbox | sh',  # sh 不是只读消费命令
                'curl -s http://127.0.0.1:6799/x | head',
                'python other.py | head',
                'python deskport/deskport.py --help | head -40 ; rm -rf x',
                'python deskport/deskport.py --help | tee x'):
        assert tp.evaluate(HOSTED, *exe(cmd))[0] == 'ask', cmd


def test_pipe_consumer_plain_args_only():
    """反例：消费段带引号参数 / 写向量 / 重定向一律不豁免（引号剥离会吃掉
    引号内容，只能看未剥引号的原文判定）。"""
    for cmd in ('python deskport/deskport.py --help | head "config.json"',
                'python deskport/deskport.py --help | head ../../config.json',
                'python deskport/deskport.py --help | head < config.json',
                'python deskport/deskport.py --help | sort -o out.txt',
                'python deskport/deskport.py --help | uniq in out',
                'python deskport/deskport.py --help | head -n $(id)'):
        assert tp.evaluate(HOSTED, *exe(cmd))[0] == 'ask', cmd


def test_pipe_consumer_only_after_pipe_not_after_and():
    """`&&` 后继段不是管道下游：仍须命中 allowCommands（head 不在白名单）。"""
    assert tp.evaluate(HOSTED, *exe('python deskport/deskport.py tasks'
                                    ' && head -40'))[0] == 'ask'
    # 管道后的 && 段同样不豁免
    assert tp.evaluate(HOSTED, *exe('python deskport/deskport.py tasks'
                                    ' | head -40 && rm -rf x'))[0] == 'ask'


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


# 2026-11-13 探针实测：request_permission.toolCall 没有 rawInput，Edit 的
# 目标路径只在 content[] 的 diff 块里（{"type":"diff","path":...}）。

def test_diff_paths_allow_without_rawinput():
    paths = ['E:/事项/远程/web-platform-dev/modules/tasks_api.py']
    decision, rule = tp.evaluate(POLICY, 'edit', 'Edit', {}, paths=paths)
    assert decision == 'allow' and rule == 'allowPaths'


def test_diff_paths_outside_still_ask():
    decision, _ = tp.evaluate(POLICY, 'edit', 'Edit', {},
                              paths=['E:/事项/other/a.py'])
    assert decision == 'ask'


def test_diff_paths_multiple_one_outside_ask():
    inside = 'E:/事项/远程/web-platform-dev/a.py'
    decision, _ = tp.evaluate(POLICY, '', 'Write', {},
                              paths=[inside, 'E:/Windows/x.ini'])
    assert decision == 'ask'


def test_permission_paths_from_diff_and_locations():
    params = {'paths': ['E:/a.py'],
              'toolCall': {'content': [{'type': 'diff', 'path': 'E:/b.py'},
                                       {'type': 'content',
                                        'content': {'type': 'text',
                                                    'text': 'E:/not-a-path'}}],
                           'locations': [{'path': 'E:/c.py'},
                                         {'path': 'E:/a.py'}]}}
    assert tp.permissionPaths(params) == ['E:/a.py', 'E:/b.py', 'E:/c.py']
    assert tp.permissionPaths({}) == []


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
