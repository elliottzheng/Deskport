import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pytest 的 tmp_path 每次都会建 <name>current 符号链接；本机 Windows 上
# Path.symlink_to 会阻塞约 31 秒（实测），而该链接只用于人工查看最新目录。
# 测试不依赖它，直接禁掉，避免每个用到 tmp_path 的用例都卡半分钟。
import _pytest.pathlib  # noqa: E402


def _noop_force_symlink(root, target, link_to):
    pass


_pytest.pathlib._force_symlink = _noop_force_symlink
