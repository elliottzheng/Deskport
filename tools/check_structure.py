# -*- coding: utf-8 -*-
"""代码结构门禁：文件预算、存量例外、重复工具、横向 import、window 全局、
跨模块私有接缝、执行点字段（对外只认 node/cwd）。

用法: python tools/check_structure.py [--strict]；退出码: 预算/存量增长/未定义名/横向 import/跨模块私有名违规 → 1，其它为 warning。
"""
import argparse
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from structure_data import (BUDGET, LEGACY, SKIP_DIRS, FEATURES, ALLOWED_FEATURE_IMPORTS,
                         INFRA, ALLOWED_WINDOW, PRIVATE_SEAM_ALLOWED, NODE_FIELD_ALLOWED)


def read_lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    return text, text.count('\n') + (0 if text.endswith('\n') or not text else 1)


def scan_execution_point_fields(errors):
    exts = ('.py', '.js', '.html', '.css')
    for base_rel in ('modules', 'frontend'):
        base = os.path.join(ROOT, base_rel)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != 'vendor']
            for name in files:
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, ROOT).replace('\\', '/')
                if rel in NODE_FIELD_ALLOWED:
                    continue
                text, _ = read_lines(path)
                if '引擎' in text:
                    errors.append('%s 出现「引擎」：统一叫执行点（node）' % rel)
                m = re.search(r'\bengine\b', text, re.I)
                if m:
                    errors.append('%s:%d 出现 engine 字段/入参：对外只有 node/cwd'
                                  '（白名单见 NODE_FIELD_ALLOWED）'
                                  % (rel, text.count('\n', 0, m.start()) + 1))


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


def scan_feature_imports(errors):
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
                errors.append('%s → %s 横向 import（应下沉共享层或登记 '
                              'ALLOWED_FEATURE_IMPORTS 并附方向理由）' % (src, dst))


def scan_infra_dependencies(warnings):
    """基础设施层不得 import 特性层（防止共享内核被业务倒灌）。"""
    mod_dir = os.path.join(ROOT, 'modules')
    for name in sorted(os.listdir(mod_dir)):
        if not name.endswith('.py') or name == '__init__.py':
            continue
        src = name[:-3]
        if src not in INFRA:
            continue
        text, _ = read_lines(os.path.join(mod_dir, name))
        pairs = []
        for m in re.finditer(r'^\s*(?:from|import)\s+modules\.(\w+)', text, re.M):
            pairs.append(m.group(1))
        for m in re.finditer(r'^\s*from\s+modules\s+import\s+([^\n#]+)', text, re.M):
            for part in m.group(1).split(','):
                pairs.append(part.strip().split(' as ')[0].strip())
        for dst in pairs:
            if dst in FEATURES:
                warnings.append('基础设施 %s → 特性 %s（依赖倒灌，应下沉或反转）' % (src, dst))


def scan_private_seams(errors):
    """跨模块私有接缝（批次 C2）：跨模块 import 不得出现单下划线前缀。

    管两种 import 形态：引私有名（from modules.X import _name）与公有名起
    私有别名（import name as _alias——fsutil.decodeBase64 as _decodeB64 这种）。
    只扫 modules/ 生产代码：tests 里探私有助手是回归集的自由，不在本门禁内。
    存量内部接缝登记在 PRIVATE_SEAM_ALLOWED（每条附理由，只减不增）。
    已知缺口: 「import modules.X as m」后的 m._name 属性式私有访问暂不拦
    （存量面大，公开化另开批次），新增请走公开名。
    """
    mod_dir = os.path.join(ROOT, 'modules')
    for fname in sorted(os.listdir(mod_dir)):
        if not fname.endswith('.py'):
            continue
        src = fname[:-3]
        rel = 'modules/' + fname
        text, _ = read_lines(os.path.join(mod_dir, fname))
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue   # 语法错误由 pyflakes 报，这里不重复
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            if node.module and node.module.startswith('modules.') \
                    and node.module.count('.') == 1:
                dst = node.module.split('.')[1]
            elif node.module == 'modules':
                dst = None
            else:
                continue
            for alias in node.names:
                name, asname = alias.name, alias.asname
                if dst is None:
                    if name.startswith('_'):
                        errors.append('%s:%d 包级引用私有名 modules._%s'
                                      % (rel, node.lineno, name))
                    continue
                if name.startswith('_'):
                    if name not in PRIVATE_SEAM_ALLOWED.get((src, dst), ()):
                        errors.append('%s:%d 跨模块引用私有名 %s（来自 modules.%s）'
                                      '——公开化，或登记 PRIVATE_SEAM_ALLOWED 并附理由'
                                      % (rel, node.lineno, name, dst))
                elif asname and asname.startswith('_'):
                    errors.append('%s:%d 公有名 %s 起了私有别名 %s（来自 modules.%s）'
                                  '——直接用公有名' % (rel, node.lineno, name,
                                                      asname, dst))


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
    scan_execution_point_fields(errors)
    scan_budget(errors, warnings)
    scan_duplicate_helpers(warnings)
    scan_feature_imports(errors)
    scan_infra_dependencies(warnings)
    scan_private_seams(errors)
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