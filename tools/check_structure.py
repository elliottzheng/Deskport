# -*- coding: utf-8 -*-
"""代码结构门禁：文件预算、存量例外、重复工具、横向 import、window 全局。

用法: python tools/check_structure.py [--strict]
退出码: 预算/存量增长/未定义名违规 → 1；其它为 warning。
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGET = {'.py': 400, '.js': 500, '.css': 500}

# 存量例外: 只减不增（行数上限）
# 2026-11-05 补登: read_lines 曾恒返回 0, 预算检查空转, 这些文件从未真正受检。
# 上限取当前行数; 拆完后从本表移除 (拆分计划见 docs/design-review.md)。
LEGACY = {
    'modules/termcore.py': 568,
    'modules/ssh_sftp.py': 503,
    'modules/plugins.py': 439,
    'frontend/assets/js/term-client.js': 682,
    'frontend/assets/js/term-features.js': 467,
    'frontend/assets/js/ssh-sftp-ui.js': 618,
    'frontend/assets/js/files-methods-core.js': 523,
}

SKIP_DIRS = {'.git', '.venv', '__pycache__', 'vendor', 'node_modules', '.deploy-backup', '.run'}
FEATURES = {'aihandoff', 'command', 'control', 'files', 'screen', 'ssh', 'tasks',
            'tasks_api', 'tasks_runner', 'tasks_store', 'terminal'}
# 允许的“上游 → 下游”依赖；其余同层 import 视为违规
ALLOWED_FEATURE_IMPORTS = {('aihandoff', 'files'), ('tasks', 'tasks_api'),
                           ('tasks_api', 'tasks_runner'), ('tasks_api', 'tasks_store'),
                           ('tasks_runner', 'tasks_store')}
ALLOWED_WINDOW = {'termManager', 'termUI', 'createSshSession', 'SSHUI',
                  'AppShell', 'AppShellV2', 'Icons', 'SftpMemory',
                  'TermClient', 'TermFeatures', 'toggleMultiExec', 'WPCommon',
                  'FilesMethodsCore', 'FilesMethodsOps', 'FilesMethodsUi', 'FilesMethodsAi',
                  'SSHUiCtx', 'SSHSftpUI', 'WP_MSG', 'SSHHostStore', 'WP_BRAND',
                  'TermEnv', 'TermSessionUI', 'TermControls'}


def read_lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    return text, text.count('\n') + (0 if text.endswith('\n') or not text else 1)


def scan_budget(errors, warnings):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in BUDGET:
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace('\\', '/')
            text, n = read_lines(path)
            cap = LEGACY.get(rel)
            if cap is not None:
                if n > cap:
                    errors.append('%s 变长: %d > %d（存量只减不增）' % (rel, n, cap))
            elif n > BUDGET[ext]:
                errors.append('%s 超预算: %d > %d' % (rel, n, BUDGET[ext]))
    return


def scan_duplicate_helpers(warnings):
    names = ('fmtSize', 'fmtTime', 'escapeHtml')
    seen = {name: [] for name in names}
    js_dir = os.path.join(ROOT, 'frontend', 'assets', 'js')
    for name in os.listdir(js_dir):
        if not name.endswith('.js'):
            continue
        text, _ = read_lines(os.path.join(js_dir, name))
        for helper in names:
            if re.search(r'function\s+%s\s*\(' % helper, text):
                seen[helper].append(name)
    for helper, files in seen.items():
        if len(files) > 1:
            warnings.append('%s 重复实现: %s（应统一到 common.js）' % (helper, ', '.join(files)))


def scan_feature_imports(warnings):
    mod_dir = os.path.join(ROOT, 'modules')
    for name in os.listdir(mod_dir):
        if not name.endswith('.py') or name == '__init__.py':
            continue
        src = name[:-3]
        if src not in FEATURES:
            continue
        text, _ = read_lines(os.path.join(mod_dir, name))
        pairs = []
        for m in re.finditer(r'^\s*(?:from|import)\s+modules\.(\w+)', text, re.M):
            pairs.append(m.group(1))
        for m in re.finditer(r'^\s*from\s+modules\s+import\s+([^\n#]+)', text, re.M):
            for part in m.group(1).split(','):
                pairs.append(part.strip().split(' as ')[0].strip())
        for dst in pairs:
            if dst in FEATURES and dst != src and (src, dst) not in ALLOWED_FEATURE_IMPORTS:
                warnings.append('%s → %s 横向 import（应下沉共享层）' % (src, dst))


def scan_undefined_names(errors, warnings):
    """pyflakes：未定义名是错误；未使用 import/重复定义是 warning（noqa 行跳过）。"""
    targets = ['app.py', 'auth.py', 'config.py', 'sessiond.py', 'watchdog.py']
    mod_dir = os.path.join(ROOT, 'modules')
    targets += [os.path.join('modules', n) for n in sorted(os.listdir(mod_dir))
                if n.endswith('.py')]
    # tools/ 与 tests/ 同样过 pyflakes: 它们是发布门禁与回归集的一部分,
    # 未定义名往往要等跑测试/发布时才炸, 不如在静态阶段拦住
    for extra in ('tools', 'tests'):
        d = os.path.join(ROOT, extra)
        if os.path.isdir(d):
            targets += [os.path.join(extra, n) for n in sorted(os.listdir(d))
                        if n.endswith('.py')]
    try:
        proc = subprocess.run([sys.executable, '-m', 'pyflakes', *targets],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        warnings.append('pyflakes 不可用，跳过未定义名检查: %s' % e)
        return
    for line in (proc.stdout or '').splitlines():
        if 'undefined name' in line:
            errors.append(line)
            continue
        m = re.match(r'^(.+?):(\d+):', line)
        if m and '# noqa' in _source_line(m.group(1), int(m.group(2))):
            continue
        if line.strip():
            warnings.append(line)


def _source_line(path, lineno):
    try:
        with open(os.path.join(ROOT, path), encoding='utf-8', errors='replace') as f:
            for i, line in enumerate(f, 1):
                if i == lineno:
                    return line
    except OSError:
        pass
    return ''


def scan_window_globals(warnings):
    js_dir = os.path.join(ROOT, 'frontend', 'assets', 'js')
    for name in os.listdir(js_dir):
        if not name.endswith('.js'):
            continue
        text, _ = read_lines(os.path.join(js_dir, name))
        for m in re.finditer(r'window\.(\w+)\s*=', text):
            g = m.group(1)
            if g not in ALLOWED_WINDOW:
                warnings.append('新增 window.%s（%s）— 需登记或改为显式接口' % (g, name))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--strict', action='store_true', help='warning 也视为失败')
    args = parser.parse_args()
    errors, warnings = [], []
    scan_budget(errors, warnings)
    scan_duplicate_helpers(warnings)
    scan_feature_imports(warnings)
    scan_window_globals(warnings)
    scan_undefined_names(errors, warnings)
    for w in warnings:
        print('[warn] ' + w)
    for e in errors:
        print('[fail] ' + e)
    print('structure: %d errors, %d warnings' % (len(errors), len(warnings)))
    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())