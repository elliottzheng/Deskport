# -*- coding: utf-8 -*-
"""防掉队门禁（批次 D）：文档跟不上指挥链的机检。

主控 2026-11-15 核查结论「没有跟上」的四类缺口，各有一条机检：

1. `deskport.py` CLI 的每个子命令，都要出现在 `SKILL.md` 与 `docs/agent-kit.md`；
2. `driver.py` 的每个子命令（含 `inbox`、`resume`），都要出现在
   `docs/agent-kit.md` 与 `COLLABORATION.md`；
3. Flask 注册的每个 `/api/agent/` 路由（从 app 的 url_map 取），都要出现在
   `docs/agent-api.md`；
4. 公开面四份文档（`agent-handbook.md` / `SKILL.md` / `COLLABORATION.md` /
   `onboarding-external-agent.md`）不得出现私有地址。

子命令清单从源码里取（CLI 解析 `sub.add_parser`），不靠手抄——驱动没有
argparse，用常量并在源码里核对存在性。文档惰性加载：文件缺失本身就是
一类缺口（红），而不是收集期报错。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, '.agents', 'skills', 'deskport')

# driver.py 的子命令（main 的 argv 分派；inbox/resume 为批次 D 新增）
DRIVER_CMDS = ('new', 'y', 'n', 'c', 'm', 'st', 'inbox', 'resume')

# 公开面四份文档禁止出现的私有地址（部署细节只进带地址的私有接入说明）
PRIVATE_ADDRS = ('frp-leg', '40963', '10.126.', '10.110.', '192.168.')


def _read(rel):
    """读一份文档；缺失即门禁失败（文件不存在是比缺内容更硬的缺口）。"""
    path = os.path.join(ROOT, rel)
    assert os.path.exists(path), '文档不存在：%s' % rel
    with open(path, encoding='utf-8') as f:
        return f.read()


def _cliSubcommands():
    """deskport.py 的 CLI 子命令：main 里 `sub.add_parser("<名字>"...)`。"""
    with open(os.path.join(SKILL_DIR, 'deskport.py'), encoding='utf-8') as f:
        src = f.read()
    return sorted(set(re.findall(r'sub\.add_parser\("([a-z][\w-]*)"', src)))


@pytest.fixture(scope='module')
def agentRules():
    """Flask 实际注册的 /api/agent/ 路由（url_map，不靠文档手抄）。"""
    import tempfile
    from app import create_app
    nav = tempfile.mkdtemp(prefix='d5-nav-')
    app, _sock = create_app(pluginRoots=[], pluginNavDir=nav)
    return sorted({r.rule for r in app.url_map.iter_rules()
                   if r.rule.startswith('/api/agent/')})


def test_cli_subcommands_in_skill_and_kit():
    """CLI 每个子命令都要在 SKILL.md 与 agent-kit.md 里出现。"""
    cmds = _cliSubcommands()
    assert cmds, '没从 deskport.py 解析出子命令（门禁自身失效）'
    for rel in ('.agents/skills/deskport/SKILL.md', 'docs/agent-kit.md'):
        doc = _read(rel)
        missing = [c for c in cmds if c not in doc]
        assert not missing, '%s 缺少 CLI 子命令：%s' % (rel, '、'.join(missing))


def test_driver_subcommands_in_kit_and_collab():
    """驱动每个子命令都要在 agent-kit.md 与 COLLABORATION.md 里出现，
    且确实存在于 driver.py 源码（常量与实现一致）。"""
    for rel in ('docs/agent-kit.md', '.agents/skills/deskport/COLLABORATION.md'):
        doc = _read(rel)
        missing = [c for c in DRIVER_CMDS if c not in doc]
        assert not missing, '%s 缺少驱动子命令：%s' % (rel, '、'.join(missing))
    with open(os.path.join(SKILL_DIR, 'driver.py'), encoding='utf-8') as f:
        src = f.read()
    gone = [c for c in DRIVER_CMDS if not re.search(r'"%s"' % re.escape(c), src)]
    assert not gone, 'driver.py 已没有子命令：%s（同步改本门禁常量）' % '、'.join(gone)


def test_agent_routes_documented(agentRules):
    """注册的每个 /api/agent/ 路由都要在 docs/agent-api.md 里出现。"""
    assert agentRules, '没从 url_map 取到 /api/agent/ 路由（门禁自身失效）'
    doc = _read('docs/agent-api.md')
    missing = [r for r in agentRules if r not in doc]
    assert not missing, 'docs/agent-api.md 缺少路由：\n  %s' % '\n  '.join(missing)


@pytest.mark.parametrize('rel', [
    'docs/agent-handbook.md',
    '.agents/skills/deskport/SKILL.md',
    '.agents/skills/deskport/COLLABORATION.md',
    'docs/onboarding-external-agent.md',
])
def test_no_private_addresses(rel):
    """公开面文档不出现私有地址（域名/隧道端口/内网网段）。"""
    doc = _read(rel)
    hits = [a for a in PRIVATE_ADDRS if a in doc]
    assert not hits, '%s 出现私有地址：%s' % (rel, '、'.join(hits))
