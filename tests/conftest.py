import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pytest 的 tmp_path 每次都会建 <name>current 符号链接；本机 Windows 上
# Path.symlink_to 会阻塞约 31 秒（实测），而该链接只用于人工查看最新目录。
# 测试不依赖它，直接禁掉，避免每个用到 tmp_path 的用例都卡半分钟。
import _pytest.pathlib  # noqa: E402

import pytest  # noqa: E402

import config  # noqa: E402


def _noop_force_symlink(root, target, link_to):
    pass


_pytest.pathlib._force_symlink = _noop_force_symlink

# R5-6：测试不依赖本机的 config.json 与运行环境。
# 全 session 共享一份最小配置：sessiond 指向不可达地址（需要代理路由的
# 测试自行打桩 _proxyJson/_proxyJsonData，不会真发请求），本机智能体节点
# 端口置 1（不可达，建卡类测试已打桩 _startTask）。stateDir 依赖的 APPDATA
# 一并指到同一个临时目录，%APPDATA%\<stateDirName> 里的真实状态（令牌文件、
# agent_tasks.json、ssh 主机库）完全不参与测试。
_TEST_CONFIG = {
    'port': 6789,
    'sessiondUrl': 'http://127.0.0.1:63991',
    'sshSessiondUrl': 'http://127.0.0.1:63992',
    'localAgentNodePort': 1,
}


def _resetConfigCache():
    config._cache.update(mtime=None, data=None)


@pytest.fixture(scope='session', autouse=True)
def isolatedConfig(tmp_path_factory):
    """所有测试统一读 tmp 里的最小 config.json，状态目录也指过去。

    session 级：create_app 有些在模块级 fixture 里装配（test_route_params），
    必须在任何 app 装配之前生效。个别用例要自定义配置时仍可函数级
    monkeypatch CONFIG_PATH 覆盖（现有 test_config/test_branding 等即如此）。
    """
    root = tmp_path_factory.mktemp('wp-test-config')
    cfgfile = root / 'config.json'
    cfgfile.write_text(json.dumps(_TEST_CONFIG), encoding='utf-8')
    mp = pytest.MonkeyPatch()
    mp.setattr(config, 'CONFIG_PATH', str(cfgfile))
    mp.setenv('APPDATA', str(root))
    _resetConfigCache()
    yield
    mp.undo()
    _resetConfigCache()


@pytest.fixture(autouse=True)
def freshConfigCache():
    """config.load 按 mtime 缓存；每用例前后清一次，杜绝跨用例串味。"""
    _resetConfigCache()
    yield
    _resetConfigCache()
