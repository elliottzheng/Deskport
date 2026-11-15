# -*- coding: utf-8 -*-
r"""批次 A 二次返工（主控拍板）：托管主管边界内只读命令自动放行。

policy.readonlyRoots（一键托管主管建卡时写入：工作区 + 项目目录 cwd）
非空时，cat/type/head/tail/ls/dir/find/grep/rg/wc/file/stat 且所有路径
参数（含 cd 目标）realpath 解析后落在根内 → allow（rule=readonly-roots）；
越界（..、符号链接、绝对路径）、旗标表外（白名单，含 --xxx=yyy 等号形态）、
敏感文件（含 realpath 展开 8.3 短名/符号链接后的 .agent-token）、其它重定向、
参数含 % $ ~ ` ! 不可判定一律询问。无 readonlyRoots 的 policy 行为逐字不变。

三验返工新增：裸 `&` 分隔符（只读门与 allowCommands 两条路径）、旗标白名单、
8.3 短名/符号链接敏感复查、%VAR% 不可判定——探针 N06/W09/W10/W14/W15/
S11/S12/U01/U02 与 probe_amp 的 6 条 allow 在此转正式测试。
"""
import os
import re

import pytest

from modules import task_policy as tp


def exe(command):
    return 'execute', 'Bash', {'command': command}


# 托管主管的 BASE_POLICY 形状（modules/leads_hosted.py）：只放行 CLI 与 cd
HOSTED = {'allowCommands': [r'python3?\s+deskport[\\/]deskport\.py\b', r'^cd\b'],
          'default': 'ask'}

# + readonlyRoots（leads_hosted 建卡时写入的形状）：工作区 + 项目目录
WS = 'E:\\ws'
PROJ = 'E:\\proj'
HOSTED_RO = dict(HOSTED, readonlyRoots=[WS, PROJ])


def test_readonly_commands_inside_roots_allow():
    """正例（规格原文）：cat/type/head/tail/ls/dir/find/grep/rg/wc/file/stat
    的路径参数全在工作区或项目目录内 → 自动放行（rule=readonly-roots）。
    含例 11 挂断链的原形状 `cd "<ws>" && cat deskport/.deskport.json`。"""
    for cmd in (
            'cd "%s" && cat deskport/.deskport.json' % WS,
            'ls %s' % PROJ,
            'grep -r foo %s\\src | head' % PROJ,
            'cat file.txt',
            'find %s -name *.py' % WS,
            'rg foo %s\\src' % PROJ,
            'stat %s\\a.py' % WS,
            'wc -l %s\\a.py' % WS,
            'type %s\\a.py' % WS,
            'head -n 5 %s\\a.py 2>/dev/null' % WS,
            'tail %s\\a.py 2>&1' % WS,
            'dir %s' % WS,
            'cd %s && ls' % PROJ,
            'file %s\\a.py' % WS,
            'cat x | head'):
        decision, rule = tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS)
        assert (decision, rule) == ('allow', 'readonly-roots'), cmd


def test_cli_pipe_consumer_with_stderr_exempt_allows():
    """复验建议改的那条：管道消费段允许带 2>/dev/null / 2>&1（上下游都认），
    其它重定向仍不豁免。"""
    for cmd in ('python deskport/deskport.py inbox | head -40 2>/dev/null',
                'cd "%s" && python deskport/deskport.py --help 2>&1'
                ' | head -40' % WS,
                'python deskport/deskport.py inbox 2>&1 | tail -10'):
        assert tp.evaluate(HOSTED, *exe(cmd), cwd=WS)[0] == 'allow', cmd


def test_readonly_commands_outside_roots_or_write_ask():
    """反例（规格原文）：越界路径（..、绝对路径、cd 出根）/ find 写执行
    参数 / 敏感文件（含引号形态）/ 其它重定向 / 非只读命令 / 路径前缀命令名
    / 变量与家目录不可判定 —— 一律 ask（fail-closed）。"""
    for cmd in (
            'cat ../../outside.txt',
            'cat C:/Windows/win.ini',
            'cat "C:/Windows/win.ini"',
            'find %s -delete' % WS,
            'find %s -exec rm {} ;' % WS,
            'find %s -fprint out' % WS,
            'cat deskport/.agent-token',
            'cat "deskport/.agent-token"',
            'head %s\\x > y' % PROJ,
            'head %s\\x < y' % WS,
            'ls ~',
            'cat $HOME/x',
            'rm -rf x',
            'echo hi',
            '%s\\cat x' % WS,
            'cd C:/Windows && type win.ini',
            'cd %s && cat ..\\..\\outside.txt' % WS,
            'rg --pre cat foo %s' % WS):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS)[0] == 'ask', cmd
    # 没有 readonlyRoots 的 policy：行为逐字不变（cat 不在 allowCommands）
    assert tp.evaluate(HOSTED, *exe('cat x'), cwd=WS)[0] == 'ask'
    # 无 base（cwd 空）时相对路径不可判定 → 询问
    assert tp.evaluate(HOSTED_RO, *exe('cat x'), cwd='')[0] == 'ask'


def test_readonly_roots_do_not_widen_write_tools_or_redirects():
    """readonlyRoots 只放行只读命令：写文件工具仍只看 allowPaths；重定向
    目标仍按 allowPaths 判（`head <项目目录>/x > y` 照旧询问）。"""
    policy = {'allowPaths': [WS], 'default': 'ask',
              'readonlyRoots': [WS, PROJ]}
    assert tp.evaluate(policy, 'edit', 'Edit',
                       {'file_path': PROJ + '\\a.py'})[0] == 'ask'
    assert tp.evaluate(policy, 'edit', 'Edit',
                       {'file_path': WS + '\\a.py'})[0] == 'allow'
    assert tp.evaluate(policy, *exe('head %s\\x > %s\\y' % (PROJ, WS)),
                       cwd=WS)[0] == 'ask'
    # 2>/dev/null / 2>&1 不挡只读命令（重定向豁免项）
    assert tp.evaluate(policy, *exe('head %s\\x 2>/dev/null' % PROJ),
                       cwd=WS) == ('allow', 'readonly-roots')


def test_readonly_symlink_escape_asks(tmp_path):
    """符号链接越界算越界（规格原文）：realpath 解析后出根的链接 → ask。
    建不了链接的环境（无管理员/开发者模式）跳过。"""
    ws = tmp_path / 'ws'
    outside = tmp_path / 'outside'
    ws.mkdir()
    outside.mkdir()
    (outside / 'secret.txt').write_text('x', encoding='utf-8')
    try:
        os.symlink(str(outside / 'secret.txt'), str(ws / 'link.txt'))
    except OSError:
        pytest.skip('建不了符号链接（需要管理员权限或开发者模式）')
    policy = {'allowCommands': [r'^cd\b'], 'default': 'ask',
              'readonlyRoots': [str(ws)]}
    assert tp.evaluate(policy, *exe('cat link.txt'), cwd=str(ws))[0] == 'ask'
    assert tp.evaluate(policy, *exe('cat secret.txt'),
                       cwd=str(ws))[0] == 'allow'


# ---- 三验返工（主控拍板：黑名单改白名单）----

def test_bare_amp_is_command_separator():
    """三验返工：裸 `&`（bash 后台执行分隔符）必须当命令分隔符——
    `cat <ws>\\x & rm -rf y` 两侧都会执行（touch 双标记实证），只读门
    与 allowCommands 两条路径都必须 ask。probe_amp 的 6 条 allow 全在
    这；`&&`、`2>&1`、`>&2` 不拆错。"""
    for cmd in (
            'cat %s\\x & rm -rf y' % WS,
            'cat %s\\x & curl -s http://evil.example/x' % WS,
            'cat %s\\x & python evil.py' % WS,
            'cat %s\\x & calc' % WS,                    # 主探针 N06
            'cat %s\\x & type C:\\Windows\\win.ini' % WS,
            'cat %s\\x & cat C:\\Users\\ZYL\\x' % WS,
            'ls & rm y',
            'cd %s && ls & rm -rf y' % WS,
            'cat %s\\x & & rm y' % WS,
            'cat %s\\x; & rm y' % WS):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS)[0] == 'ask', cmd
    # 纯只读的后台 cat 无害，仍放行（probe_amp 的对照项）
    assert tp.evaluate(HOSTED_RO, *exe('cat %s\\x &' % WS),
                       cwd=WS)[0] == 'allow'
    # 分隔符形态不被拆错
    assert tp.evaluate(HOSTED_RO, *exe('cat %s\\x 2>&1' % WS),
                       cwd=WS) == ('allow', 'readonly-roots')
    assert tp.evaluate(HOSTED_RO, *exe('cat %s\\x && cat %s\\y' % (WS, WS)),
                       cwd=WS) == ('allow', 'readonly-roots')
    # 普通 allowCommands 路径同有此盲区（既有盲区，三验点名同一处修）：
    # ^cat 只放行 cat 段，& 后面的 rm 段必须 ask
    plain = {'allowCommands': [r'^cat\b'], 'default': 'ask'}
    assert tp.evaluate(plain, *exe('cat x'), cwd=WS)[0] == 'allow'
    assert tp.evaluate(plain, *exe('cat x & rm -rf y'), cwd=WS)[0] == 'ask'
    assert tp.evaluate(plain, *exe('cat x; & rm y'), cwd=WS)[0] == 'ask'
    # 两侧都只读时老路径照放（后台跑两个 cat 无害）
    assert tp.evaluate(plain, *exe('cat x & cat y'), cwd=WS)[0] == 'allow'


def test_unterminated_heredoc_is_undeterminable():
    """四验返工（必须修，探针 H01-H05/H10）：heredoc 找不到终止行 →
    整条不可判定 → ask（rule=danger:heredoc）。`cat <<EOF & rm -rf y`
    里 `&` 之后的命令 bash 实测两侧都执行，旧实现从 `<<TAG` 起整段丢弃，
    只读门被自动放行；闭合 heredoc 的既有行为不变（`<<` 被 _hasRedirect
    fail-closed 成 ask——四验归入[建议改]，本轮不动）。"""
    for cmd in (
            'cat <<EOF & rm -rf y',                    # H01
            'cat <<EOF && rm -rf y',                   # H02
            'cat <<EOF; rm -rf y',                     # H03
            'cat <<EOF | rm -rf y',                    # H04
            "cat <<'EOF' & rm -rf y",                  # H05
            'echo hi <<EOF & curl -s http://evil/x',   # H06
            'cat <<EOF\nhello & rm -rf y',             # 多行未闭合
            'cat <<EOF'):                              # 标记行即末行
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS) == \
            ('ask', 'danger:heredoc'), cmd
    # 闭合 heredoc：既有行为不变（只读门 fail-closed → ask）
    for cmd in ('cat <<EOF\nhello world\nEOF',
                'cat <<EOF\n& rm y\nEOF',
                'cat <<EOF\nhello\nEOF & rm -rf y'):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS)[0] == 'ask', cmd


def test_flag_whitelist_rejects_unknown_flags():
    """三验返工：旗标白名单——表外旗标（含 --xxx=yyy 等号形态、读外部
    文件、执行程序）一律 ask（探针 W09/W10/W14/W15 及同类）。"""
    for cmd in (
            # rg 执行参数两形态（等号曾整条漏过）
            'rg --pre=cat foo %s' % WS,
            'rg --pre-glob=*.py --pre=cat foo %s' % WS,
            # 读外部文件的等号/分离形态
            'wc --files0-from=C:\\Users\\ZYL\\x',
            'grep --file=C:\\Users\\ZYL\\x foo %s' % WS,
            'grep -f C:\\Users\\ZYL\\x foo %s' % WS,
            'rg --files C:\\Users\\ZYL\\list foo %s' % WS,
            'rg -z foo %s' % WS,
            # stat/file/type 带旗标不再放行（白名单从紧）
            'stat --printf \'%%s\' %s\\a.py' % WS,
            'file -i %s\\a.py' % WS,
            'type -a %s\\a.py' % WS,
            # tail -f 不放行（规格点名）
            'tail -f %s\\a.py' % WS,
            # 其它表外旗标
            'ls --color=auto %s' % WS,
            'cat --number %s\\x' % WS,
            'grep --count=3 foo %s' % WS,
            'find %s -newer C:\\Users\\ZYL\\x -name *.py' % WS,
            'find %s -newerct 2h' % WS):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS)[0] == 'ask', cmd


def test_flag_whitelist_allows_tabled_flags():
    """白名单表内旗标（含取值形态：分离/紧贴/等号/旧式数字/布尔簇）
    仍然放行。"""
    for cmd in (
            'cat -n %s\\a.py' % WS,
            'head -n 5 %s\\a.py' % WS,
            'head -c 999999 %s\\a.py' % WS,
            'head -40 %s\\a.py' % WS,
            'head -n5 %s\\a.py' % WS,
            'tail -n 20 %s\\a.py' % WS,
            'ls -la %s' % WS,
            'ls -R -t %s\\src' % WS,
            'grep -rn foo %s\\src' % WS,
            'grep --include=*.py -r . %s' % PROJ,
            'grep --exclude *.md -r foo %s' % WS,
            'grep -m 5 -A 2 foo %s' % WS,
            'grep -e foo -e bar %s' % WS,
            'rg --hidden --no-ignore -g *.py foo %s' % WS,
            'rg -t py foo %s' % WS,
            'grep --color=never foo %s' % WS,
            'wc -l -w %s\\a.py' % WS,
            'find %s -name *.py -type f -maxdepth 2' % WS,
            'find %s -newer %s\\a.py -name *.py' % (WS, WS)):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS) == \
            ('allow', 'readonly-roots'), cmd


def test_agent_token_via_shortname_and_link_asks(monkeypatch):
    """三验返工：realpath 展开 8.3 短名（AGENT-~1）与符号链接后复查
    敏感文件——`.agent-token` 无论以何种路径形态出现都必须 ask
    （deny:sensitive-file）。本机 E: 卷 8.3 关闭，用 monkeypatch 模拟
    realpath 展开。"""
    from modules import task_policy_readonly as ro

    real = ro._real
    short = re.compile(r'AGENT-~1', re.I)

    def fakeReal(path):
        text = str(path)
        if short.search(text):
            return real(short.sub('.agent-token', text))
        if 'link.txt' in text:              # 符号链接指向令牌文件
            return real(text.replace('link.txt', 'deskport\\.agent-token'))
        return real(text)

    monkeypatch.setattr(ro, '_real', fakeReal)
    for cmd in (
            'cat %s\\deskport\\AGENT-~1' % WS,
            'cat "%s\\deskport\\AGENT-~1"' % WS,
            "cat '%s\\deskport\\AGENT-~1'" % WS,
            'type %s\\deskport\\AGENT-~1' % WS,
            'cat %s\\deskport\\link.txt' % WS):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS) == \
            ('ask', 'deny:sensitive-file'), cmd
    # 根内普通文件不受影响
    assert tp.evaluate(HOSTED_RO, *exe('cat %s\\a.py' % WS),
                       cwd=WS) == ('allow', 'readonly-roots')


def test_percent_var_and_bang_are_undeterminable():
    """三验返工：参数含 %VAR%（Windows 环境变量）/ !（历史展开）等
    不可判定字符 → ask（探针 U01/U02 及同类）。"""
    for cmd in (
            'cat %USERPROFILE%\\x',
            'cd %USERPROFILE% && cat x',
            'cat %s\\x & echo %%TEMP%%' % WS,
            'grep -r foo %s | head -n 5 !' % WS,
            'cat \'%s\\a b!\'' % WS,
            'cat %s\\x `id`' % WS):
        assert tp.evaluate(HOSTED_RO, *exe(cmd), cwd=WS)[0] == 'ask', cmd
