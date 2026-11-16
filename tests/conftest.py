import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

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


def pytest_configure(config):
    """--basetemp 指向不存在的父目录也照跑。

    pytest 拿到 --basetemp 后自己只做一次不带 parents 的 mkdir，父目录缺失
    会 FileNotFoundError（WinError 3）——跑个测试还要先手工建父目录，没道理。
    这里在任何 fixture/用例之前先建好；basetemp 缺省时不插手。
    """
    given = getattr(config.option, 'basetemp', None)
    if given:
        Path(given).mkdir(parents=True, exist_ok=True)


def _freshConfigRoot():
    """每次会话一个全新的空配置目录（%TEMP% 下 mkdtemp，天然不撞名）。

    旧实现用 tmp_path_factory.mktemp('wp-test-config')，它依赖 .pytest-tmp
    的状态：上次跑挂留下的残留目录会让 mkdir 抛 FileExistsError
    （WinError 183），整套测试 20 个 ERROR，看起来像代码坏了，其实只是
    脏目录。mkdtemp 每次都是新目录，basetemp 被占用/脏了都伤不到它；
    会话结束由 fixture 整棵删掉，不在 %TEMP% 留垃圾。
    "占用也自动换新目录、不静默失败"由 tests/test_isolated_config.py 自检。
    """
    return Path(tempfile.mkdtemp(prefix='wp-test-config-'))


@pytest.fixture(scope='session', autouse=True)
def isolatedConfig():
    """所有测试统一读 tmp 里的最小 config.json，状态目录也指过去。

    session 级：create_app 有些在模块级 fixture 里装配（test_route_params），
    必须在任何 app 装配之前生效。个别用例要自定义配置时仍可函数级
    monkeypatch CONFIG_PATH 覆盖（现有 test_config/test_branding 等即如此）。
    根目录每次会话新建（_freshConfigRoot），不复用任何已有目录。
    """
    root = _freshConfigRoot()
    cfgfile = root / 'config.json'
    cfgfile.write_text(json.dumps(_TEST_CONFIG), encoding='utf-8')
    mp = pytest.MonkeyPatch()
    mp.setattr(config, 'CONFIG_PATH', str(cfgfile))
    mp.setenv('APPDATA', str(root))
    _resetConfigCache()
    yield
    # 批次 L L6：create_app 会取状态目录所有者锁（持锁句柄开在会话的 tmp
    # 状态目录里）——先放掉再删目录，否则 rmtree 被占用文件挡住
    try:
        import modules.state_owner as stateOwner
        if stateOwner._ownerHandle is not None:
            stateOwner._ownerHandle.close()
            stateOwner._ownerHandle = None
            stateOwner._mode = ''
    except Exception:
        pass
    mp.undo()
    _resetConfigCache()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def freshConfigCache():
    """config.load 按 mtime 缓存；每用例前后清一次，杜绝跨用例串味。"""
    _resetConfigCache()
    yield
    _resetConfigCache()


@pytest.fixture(scope='session', autouse=True)
def idleTaskHealthLoop():
    """批次 L：task_health 的巡检线程在测试进程里空转。

    线上它 5s 一跳（重启 60s 后自动续接 + 工具卡住报警）；测试里这些状态
    都由用例自己搭、直接调 task_health.tick()，后台那一跳只会和用例竞态
    （如重启回归集造的 paused 任务被它中途续掉）。把线程目标换成空操作，
    tick 本体保持可调。
    """
    import modules.task_health as taskHealth
    original = taskHealth._loop
    taskHealth._loop = lambda: None
    yield
    taskHealth._loop = original


@pytest.fixture(scope='session', autouse=True)
def idleWorkitemWatchLoop():
    """批次 G2：workitem_watch 的后台巡检在测试进程里空转。

    app.py 模块级就 create_app()——巡检线程在收集期（任何 fixture 之前）
    已经起来了，改 _loop 拦不住已经开跑的线程；把每跳前的开关关掉，线程
    空转。线上 2s 一跳（起不来重试/卡死/提前收工/上下文换手）；测试里这些
    状态由用例自搭、显式调 workitem_watch.sweep() 驱动（开关不影响直调）。
    """
    import modules.workitem_watch as workitemWatch
    workitemWatch._enabled = False
    yield
    workitemWatch._enabled = True
