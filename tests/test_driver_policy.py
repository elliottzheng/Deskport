# -*- coding: utf-8 -*-
"""驱动模板的边界判定（纯函数，不连网）：越界只看真正执行的部分、档位、豁免。"""
import importlib.util
import pathlib

import pytest

DRIVER_PATH = (pathlib.Path(__file__).resolve().parents[1]
               / '.agents' / 'skills' / 'deskport' / 'driver.py')
_spec = importlib.util.spec_from_file_location('deskport_driver', DRIVER_PATH)
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

CFG = {
    'root': 'E:/proj/app',
    'danger': r'git\s+(push|reset)\b|\bkill\b|deploy|rm\s+-rf',
    'safe': r'^\s*(ls|cat|grep|git (status|diff|log))\b',
    'profiles': {'commit': r'git\s+-c\s+\S+\s+(add|commit)'},
    'extraWritable': ['/tmp/build/'],
}


@pytest.fixture
def policy():
    return driver.Policy(CFG)


def ev(command=None, path=None, name='bash', profile='', **kw):
    args = {}
    if command:
        args['command'] = command
    if path:
        args['path'] = path
    return dict(name=name, args=args, _profile=profile, **kw)


def test_executable_part_strips_heredoc_and_quotes():
    """正文/引号里的危险词不算越界——这是审批别啰嗦的关键。"""
    cmd = 'python -c "print(\'rm -rf /\')" && echo done'
    assert 'rm -rf' not in driver.executablePart(cmd)
    heredoc = 'cat > x.sh <<\'EOF\'\nrm -rf /\nkill -9 1\nEOF\nls'
    assert 'kill' not in driver.executablePart(heredoc)


def test_split_segments_keeps_escaped_pipe():
    """grep 正则里的 \\| 不是管道。"""
    segs = driver.splitSegments(r'grep -n "a\|b" f.py')
    assert len(segs) == 1
    assert len(driver.splitSegments('cd /x && ls; cat f')) == 3


def test_inside_does_not_prefix_match():
    assert driver.inside('E:/proj/app/src/a.py', 'E:/proj/app')
    assert driver.inside('/tmp/build/out.o', 'E:/proj/app', ['/tmp/build/'])
    assert not driver.inside('E:/proj/application/a.py', 'E:/proj/app')
    assert not driver.inside('/srv/other/a.py', 'E:/proj/app')


def test_classify_boundary_cases(policy):
    assert policy.classify(ev('git status --short')) == ('allow', '只读/测试命令')
    assert policy.classify(ev('git push origin main'))[0] == 'ask'
    assert policy.classify(ev('ls -la'))[0] == 'allow'
    # 引号里的危险词：放行
    assert policy.classify(ev('echo "rm -rf /"'))[0] == 'allow'
    # 边界内的写：不命中 danger 就放行（不逐条盯）
    assert policy.classify(ev('python -m pytest -q'))[0] == 'allow'


def test_classify_profile_preauth(policy):
    """规格里点名的动作不该再停一次（§8c）。"""
    assert policy.classify(ev('git -c x=y add -A', profile='commit')) == ('allow', '规格预授权')
    assert policy.classify(ev('git push', profile='commit'))[0] == 'ask'


def test_cleanup_of_own_artifacts(policy):
    """cd 进边界后 rm -rf 相对路径 = 清自己的产物；碰绝对路径/上级目录照旧拦。"""
    assert policy.classify(ev('cd E:/proj/app && rm -rf build dist'))[0] == 'allow'
    assert policy.classify(ev('rm -rf /'))[0] == 'ask'
    assert policy.classify(ev('cd E:/proj/app && rm -rf ../other'))[0] == 'ask'
    assert policy.classify(ev('cd /srv/other && rm -rf build'))[0] == 'ask'


def test_classify_paths_and_tools(policy):
    assert policy.classify(ev(path='E:/proj/app/docs/a.md', name='write'))[0] == 'allow'
    assert policy.classify(ev(path='/etc/passwd', name='write')) == ('ask', '边界外路径')
    assert policy.classify(ev(name='grep_search')) == ('allow', '只读工具')
    assert policy.classify(ev(name='weird_tool')) == ('ask', '无法识别')


def test_pick_option_ids(policy):
    evt = {'options': [{'optionId': 'a1', 'kind': 'allow_once'},
                       {'optionId': 'r1', 'kind': 'reject_once'}]}
    assert driver.Policy.pick(evt, True) == 'a1'
    assert driver.Policy.pick(evt, False) == 'r1'
    assert driver.Policy.pick({'options': []}, True) == ''


def test_server_policy_shape(policy):
    p = policy.serverPolicy()
    assert p['allowPaths'] == ['E:/proj/app'] and p['default'] == 'ask'
    assert p['denyCommands'] and 'push' in p['denyCommands'][0]
    assert driver.Policy(dict(CFG, serverPolicy=False)).serverPolicy() == {}