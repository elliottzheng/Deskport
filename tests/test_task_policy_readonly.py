# -*- coding: utf-8 -*-
r"""批次 A 二次返工（主控拍板）：托管主管边界内只读命令自动放行。

policy.readonlyRoots（一键托管主管建卡时写入：工作区 + 项目目录 cwd）
非空时，cat/type/head/tail/ls/dir/find/grep/rg/wc/file/stat 且所有路径
参数（含 cd 目标）规范化后落在根内 → allow（rule=readonly-roots）；
越界（..、符号链接、绝对路径）、写执行参数、敏感文件、其它重定向、
不可判定一律询问。无 readonlyRoots 的 policy 行为逐字不变。
"""
import os

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
