# -*- coding: utf-8 -*-
"""前端静态检查：资源存在性、HTML/JS id 引用、JS 调用目标、API 端点一致性。

与 tools/check_structure.py（Python 侧门禁）配套，覆盖纯正则/语法检查抓不到的
前后端漂移问题。用法:

    python tools/check_frontend.py            # warning 不失败
    python tools/check_frontend.py --strict   # warning 也失败
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, 'frontend')
JS_DIR = os.path.join(FRONTEND, 'assets', 'js')
CSS_DIR = os.path.join(FRONTEND, 'assets', 'css')
VENDOR_DIR = os.path.join(FRONTEND, 'assets', 'vendor')
SKIP_VENDOR = True

JS_KEYWORDS = {
    'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'instanceof', 'delete',
    'function', 'new', 'await', 'yield', 'void', 'in', 'of', 'do', 'else', 'try',
    'finally', 'throw', 'case', 'default', 'with', 'class', 'const', 'let', 'var',
    'super', 'this', 'import', 'export', 'extends', 'async',
}

JS_GLOBALS = set('''window document console location navigator localStorage sessionStorage
fetch setTimeout setInterval clearTimeout clearInterval requestAnimationFrame
cancelAnimationFrame alert confirm prompt JSON Math Date Object Array String Number
Boolean RegExp Error TypeError RangeError SyntaxError Map Set WeakMap WeakSet Promise
Symbol BigInt parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent
structuredClone atob btoa TextEncoder TextDecoder URL URLSearchParams Blob File
FileReader FormData Headers Request Response AbortController Event CustomEvent
KeyboardEvent MouseEvent WheelEvent DragEvent MessageChannel MutationObserver
ResizeObserver IntersectionObserver PerformanceObserver Intl ArrayBuffer Uint8Array
Uint16Array Int8Array Float32Array DataView process require module exports globalThis
WebSocket EventSource XMLHttpRequest Image Audio Notification crypto performance history
screen self top parent frames opener close open print focus blur scrollTo scrollBy
getComputedStyle matchMedia devicePixelRatio innerWidth innerHeight pageXOffset
pageYOffset addEventListener removeEventListener dispatchEvent postMessage
setImmediate queueMicrotask Proxy Reflect'''.split())

# 项目内跨文件共享的全局（脚本无模块包裹，全部挂 window/顶层作用域）
PROJECT_GLOBALS = {
    'Vue', 'createApp', 'ref', 'reactive', 'computed', 'watch', 'nextTick', 'onMounted',
    'Terminal', 'FitAddon', 'SearchAddon', 'WebLinksAddon', 'SerializeAddon',
    'AttachAddon', 'Unicode11Addon',
    'TermManager', 'TermSessionUI', 'TermControls', 'TermFeatures', 'TermClient',
    'TermEnv', 'AppShell', 'AppShellV2', 'Icons', 'WPCommon', 'WP_MSG', 'WP_BRAND',
    'SftpMemory', 'SSHUI', 'SSHSftpUI', 'SSHHostStore', 'SSHUiCtx',
    'FilesMethodsCore', 'FilesMethodsOps', 'FilesMethodsUi', 'FilesMethodsAi',
    'toggleMultiExec', 'createSshSession', 'termManager', 'termUI',
}

KNOWN_CALLS = JS_GLOBALS | PROJECT_GLOBALS

_STRIP_CACHE = {}


def _read(path):
    if path not in _STRIP_CACHE:
        with open(path, encoding='utf-8', errors='replace') as f:
            _STRIP_CACHE[path] = f.read()
    return _STRIP_CACHE[path]


def _strip_noise(text):
    """去掉注释与字符串内容，保留引号本身以维持结构。"""
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'(?<!\\)//[^\n]*', ' ', text)
    # 模板字符串: 整体替换为空串（保留反引号对），避免内部 ${} 干扰
    text = re.sub(r'`(?:\\.|[^`\\])*`', '""', text, flags=re.S)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\\n])*'", '""', text)
    return text


def js_files():
    out = []
    for base, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in sorted(files):
            if name.endswith('.js'):
                out.append(os.path.join(base, name))
    if not SKIP_VENDOR:
        for name in sorted(os.listdir(VENDOR_DIR)) if os.path.isdir(VENDOR_DIR) else []:
            if name.endswith('.js'):
                out.append(os.path.join(VENDOR_DIR, name))
    return out


def html_files():
    return [os.path.join(FRONTEND, n) for n in sorted(os.listdir(FRONTEND))
            if n.endswith('.html')]


def html_inline_scripts():
    """HTML 内联 <script> 代码（与外部 js 共享顶层作用域）。"""
    out = []
    for path in html_files():
        text = _read(path)
        for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', text, re.S | re.I):
            body = m.group(1)
            if body.strip():
                out.append((path, body))
    return out


# ---------------------------------------------------------------- 检查项

def check_js_syntax(errors, warnings):
    for path in js_files():
        rel = os.path.relpath(path, ROOT).replace('\\', '/')
        proc = subprocess.run(['node', '--check', path], capture_output=True, text=True)
        if proc.returncode != 0:
            errors.append('%s 语法错误: %s' % (rel, (proc.stderr or '').strip().splitlines()[:2]))


def check_css_braces(errors, warnings):
    for base, dirs, files in os.walk(CSS_DIR):
        for name in sorted(files):
            if not name.endswith('.css'):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace('\\', '/')
            text = _read(path)
            text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
            text = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', text)
            if text.count('{') != text.count('}'):
                errors.append('%s 花括号不配平: { =%d } =%d'
                              % (rel, text.count('{'), text.count('}')))


def check_assets_exist(errors, warnings):
    for path in html_files():
        rel = os.path.relpath(path, ROOT).replace('\\', '/')
        text = _read(path)
        for m in re.finditer(r'(?:src|href)="(/assets/[^"?]+)', text):
            target = os.path.join(FRONTEND, m.group(1).lstrip('/').replace('/', os.sep))
            if not os.path.isfile(target):
                errors.append('%s 引用不存在的资源: %s' % (rel, m.group(1)))


def _html_static_ids():
    ids = set()
    for path in html_files():
        for m in re.finditer(r'\bid="([^"]+)"', _read(path)):
            ids.add(m.group(1))
    return ids


def _js_dynamic_ids():
    ids = set()
    pats = [
        r'\.id\s*=\s*[\'"]([^\'"]+)[\'"]',
        r'\bid=["\']?([A-Za-z_][\w-]*)',
        r'setAttribute\(\s*[\'"]id[\'"]\s*,\s*[\'"]([^\'"]+)[\'"]',
    ]
    for path in js_files():
        text = _read(path)
        for pat in pats:
            for m in re.finditer(pat, text):
                ids.add(m.group(1))
    return ids


def check_id_refs(errors, warnings):
    known = _html_static_ids() | _js_dynamic_ids()
    ref_pats = [
        r'getElementById\(\s*[\'"]([^\'"]+)[\'"]',
        r'querySelector(?:All)?\(\s*[\'"]#([\w-]+)',
    ]
    for path in js_files():
        rel = os.path.relpath(path, ROOT).replace('\\', '/')
        text = _read(path)
        for pat in ref_pats:
            for m in re.finditer(pat, text):
                ref = m.group(1)
                if ref not in known:
                    warnings.append('%s 引用未定义的 id #%s' % (rel, ref))


def _declared_names():
    """所有外部 JS + HTML 内联脚本里声明的顶层/局部名（共享全局作用域）。"""
    names = set()
    srcs = [(_read(p), p) for p in js_files()]
    srcs += [(body, path) for path, body in html_inline_scripts()]
    for text, _ in srcs:
        clean = _strip_noise(text)
        names.update(re.findall(r'\bfunction\s+([A-Za-z_$][\w$]*)', clean))
        names.update(re.findall(r'\bclass\s+([A-Za-z_$][\w$]*)', clean))
        names.update(re.findall(r'\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)', clean))
        # 解构: const {a, b} = ... / const [a, b] = ...
        for m in re.finditer(r'\b(?:var|let|const)\s*[\{\[]([^\}\]]{1,400})[\}\]]', clean):
            for part in m.group(1).split(','):
                name = part.split(':')[-1].split('=')[0].strip()
                if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                    names.add(name)
        # 函数/箭头参数
        for m in re.finditer(r'\bfunction\s*[\w$]*\s*\(([^)]{0,400})\)', clean):
            for part in m.group(1).split(','):
                name = part.split('=')[0].strip().lstrip('...')
                if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                    names.add(name)
        for m in re.finditer(r'\(([^()]{0,400})\)\s*=>', clean):
            for part in m.group(1).split(','):
                name = part.split('=')[0].strip().lstrip('...')
                if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                    names.add(name)
        for m in re.finditer(r'\bcatch\s*\(\s*([A-Za-z_$][\w$]*)', clean):
            names.add(m.group(1))
        # 对象/类方法简写: foo(args) { ... }
        for m in re.finditer(r'(?:^|[\s{,;])([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{', clean, re.M):
            names.add(m.group(1))
    return names


def check_call_targets(errors, warnings):
    known = _declared_names() | KNOWN_CALLS
    for path in js_files():
        rel = os.path.relpath(path, ROOT).replace('\\', '/')
        clean = _strip_noise(_read(path))
        unknown = {}
        for m in re.finditer(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(', clean):
            name = m.group(1)
            if name in JS_KEYWORDS or name in known:
                continue
            unknown.setdefault(name, 0)
            unknown[name] += 1
        for m in re.finditer(r'\bnew\s+([A-Za-z_$][\w$]*)', clean):
            name = m.group(1)
            if name in known:
                continue
            unknown.setdefault(name, 0)
            unknown[name] += 1
        for name, count in sorted(unknown.items()):
            warnings.append('%s 调用未声明的 %s() (%d 处)' % (rel, name, count))


def _python_routes():
    routes = set()
    srcs = ['app.py', 'auth.py', 'config.py', 'sessiond.py', 'watchdog.py']
    mod_dir = os.path.join(ROOT, 'modules')
    srcs += [os.path.join('modules', n) for n in sorted(os.listdir(mod_dir))
             if n.endswith('.py')] if os.path.isdir(mod_dir) else []
    for rel in srcs:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        for m in re.finditer(r'\.route\(\s*[\'"]([^\'"]+)[\'"]', text):
            routes.add(m.group(1))
        for m in re.finditer(r'add_url_rule\(\s*[\'"]([^\'"]+)[\'"]', text):
            routes.add(m.group(1))
    return routes


def _norm_route(r):
    r = r.split('?')[0]
    return '/' + '/'.join('*' if seg.startswith('<') else seg
                          for seg in r.strip('/').split('/') if seg)


def check_api_endpoints(errors, warnings):
    routes = {_norm_route(r): r for r in _python_routes()}
    literals = {}
    for path in js_files():
        rel = os.path.relpath(path, ROOT).replace('\\', '/')
        text = _read(path)
        for m in re.finditer(r'[\'"`](/api/[A-Za-z0-9_\-/{}.$]*)', text):
            literals.setdefault(m.group(1), set()).add(rel)
        for m in re.finditer(r'([\'"])(/api/[^\'"]*)\1\s*\+', text):
            literals.setdefault(m.group(2), set()).add(rel)
    for lit, files in sorted(literals.items()):
        base = lit.split('?')[0].rstrip('/')
        if not base or base == '/api':
            continue
        norm = _norm_route(base)
        hit = any(r == norm or r.startswith(norm) or norm.startswith(r)
                  for r in routes)
        if not hit:
            warnings.append('JS 请求未登记的后端端点 %s (%s)' % (base, ', '.join(sorted(files))))


CHECKS = [
    ('JS 语法', check_js_syntax),
    ('CSS 括号', check_css_braces),
    ('静态资源', check_assets_exist),
    ('id 引用', check_id_refs),
    ('调用目标', check_call_targets),
    ('API 端点', check_api_endpoints),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--strict', action='store_true', help='warning 也视为失败')
    args = parser.parse_args()
    errors, warnings = [], []
    for label, fn in CHECKS:
        before = len(errors) + len(warnings)
        fn(errors, warnings)
        found = len(errors) + len(warnings) - before
        print('[check] %-8s %s' % (label, 'OK' if found == 0 else '%d 项' % found))
    print()
    for w in warnings:
        print('[warn] ' + w)
    for e in errors:
        print('[fail] ' + e)
    print('frontend: %d errors, %d warnings' % (len(errors), len(warnings)))
    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
