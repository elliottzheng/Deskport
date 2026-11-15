# -*- coding: utf-8 -*-
r"""并行跑全量 pytest：按历史耗时分组，子进程并行，全量压在 2 分钟内。

用法：
    python tools/run_tests.py                 # 全量（组数缺省 min(CPU, 6)）
    python tools/run_tests.py --files tests/test_tasks_api.py    # 只跑指定文件
    python tools/run_tests.py --changed       # 只跑相对 main 有改动的模块对应的测试
    python tools/run_tests.py --jobs 4        # 指定组数

设计：
  - 分组用 .pytest-tmp/durations.json 里的历史耗时（首次没有记录时按文件大小），
    最长优先装进当前最闲的组（LPT），各组总耗时尽量均衡；
  - 每组一个独立子进程：独立 --basetemp 与独立临时 APPDATA，互不踩
    （pyproject 的 addopts 把所有运行都指到同一个 .pytest-tmp，串行跑无所谓，
    并行必须按组分流）；
  - basetemp 缺省在系统临时目录（%TEMP%\wp-test-shards\shard-<i>）而不是仓库
    的 .pytest-tmp：本机 E: 是 USB 机械盘，fsync+replace 约 60ms/次（C: NVMe
    约 6ms，慢 10 倍），而快照/收件箱这类存储是「每次事件一次 fsync 重写」，
    basetemp 落在 E: 时全量最好 265s、达不到 2 分钟目标；落 C: 后全量约 66s。
    每次运行先清空再建、结束即删（C: 空间有限，不留垃圾）。想回到规格字面的
    仓库内目录可传 --basetemp-root .pytest-tmp；
  - 跑完核对「分组合计条数 == --collect-only 条数」，防某组静默少跑；
  - 任一组失败打印该组失败原文片段（最多 40 行），退出码非 0；
  - 本次各文件耗时回写 durations.json，下次分组更准。

ignore 规则与 .drive/verify.py 保持一致：test_protocol.py / test_ssh_ws.py
需要真服务与 WebSocket，不在全量里跑。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / '.pytest-tmp'
DURATIONS = TMP / 'durations.json'
IGNORED = ('test_protocol.py', 'test_ssh_ws.py')   # 需真服务/WS，见 .drive/verify.py
SHARD_TIMEOUT = 600

# pytest -q 最后一行计数: "234 passed, 1 failed, 2 skipped in 12.3s"
COUNT_RE = re.compile(r'(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)')
# --durations=0 的行: "0.52s call     tests/test_x.py::test_y"
DUR_RE = re.compile(r'^(\d+(?:\.\d+)?)s\s+(call|setup|teardown)\s+(\S+)')
FAIL_MARK_RE = re.compile(r'^=+ (FAILURES|ERRORS|errors|failures)')


def list_test_files():
    files = sorted(p.name for p in (ROOT / 'tests').glob('test_*.py')
                   if p.name not in IGNORED)
    return files


def load_durations():
    try:
        data = json.loads(DURATIONS.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_durations(data):
    TMP.mkdir(exist_ok=True)
    tmp = DURATIONS.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding='utf-8')
    os.replace(tmp, DURATIONS)


def estimate(name, durations):
    """分组权重：有历史按历史（秒），没有按文件大小（字节，同单位内可比）。"""
    if name in durations:
        return float(durations[name])
    try:
        return (ROOT / 'tests' / name).stat().st_size / 1e6
    except OSError:
        return 0.0


def make_shards(files, jobs, durations):
    """LPT：耗时长的文件先装进当前最闲的组，均衡各组总耗时。"""
    shards = [[] for _ in range(min(jobs, len(files)) or 1)]
    loads = [0.0] * len(shards)
    for name in sorted(files, key=lambda f: -estimate(f, durations)):
        i = loads.index(min(loads))
        shards[i].append(name)
        loads[i] += estimate(name, durations)
    return [s for s in shards if s]


def parse_counts(output):
    """从输出里找 pytest 的计数行，返回 {passed: n, failed: n, ...}（缺项 0）。"""
    counts = {}
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line or ' in ' not in line:
            continue
        found = {k: int(v) for v, k in COUNT_RE.findall(line)}
        if found:
            return found
    return counts


def parse_durations(output):
    per_file = {}
    for line in output.splitlines():
        m = DUR_RE.match(line.strip())
        if m:
            node = m.group(3)
            fname = node.split('::')[0]
            per_file[fname] = per_file.get(fname, 0.0) + float(m.group(1))
    return per_file


def failure_excerpt(output, limit=40):
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if FAIL_MARK_RE.match(line.strip()):
            body = [ln for ln in lines[i:] if ln.strip()]
            return '\n'.join(body[:limit])
    body = [ln for ln in lines if ln.strip()]
    return '\n'.join(body[-limit:])


def run_shard(idx, files, basetemp_root):
    """一组一个子进程：独立 basetemp + 独立临时 APPDATA。

    APPDATA 也放在 basetemp_root 下面（而非各自 mkdtemp）：运行前后的
    整根清理能顺带清掉它，被中断的上一轮遗留也在下一轮开头清掉。
    """
    appdata = Path(basetemp_root) / ('appdata-%d' % idx)
    appdata.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['APPDATA'] = str(appdata)
    basetemp = Path(basetemp_root) / ('shard-%d' % idx)
    cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
           '--durations=0', '--basetemp=%s' % basetemp]
    cmd += ['tests/%s' % f for f in files]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                           text=True, encoding='utf-8', errors='replace',
                           timeout=SHARD_TIMEOUT)
        out = (p.stdout or '') + (p.stderr or '')
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or '') if isinstance(e.stdout, str) else ''
        out += '\nSHARD TIMEOUT after %ds' % SHARD_TIMEOUT
        code = -1
    return {'idx': idx, 'files': files, 'code': code, 'out': out,
            'sec': time.perf_counter() - t0}


def collect_total(files):
    """--collect-only 拿条数基线，用于核对分组合计有没有少跑。"""
    cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
           '--collect-only']
    cmd += ['tests/%s' % f for f in files]
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=300)
    out = (p.stdout or '') + (p.stderr or '')
    m = re.search(r'(\d+) tests? collected', out)
    if m:
        return int(m.group(1)), None
    if 'no tests ran' in out:
        return 0, None
    return None, out[-2000:]


def changed_files():
    """相对 main 改动（含未跟踪新文件）的 tests/test_*.py + 对应模块的测试。"""
    def git(args):
        p = subprocess.run(['git'] + args, cwd=str(ROOT), capture_output=True,
                           text=True, encoding='utf-8', errors='replace')
        return [ln.strip() for ln in (p.stdout or '').splitlines() if ln.strip()]

    paths = set(git(['diff', '--name-only', 'main']))
    paths.update(git(['ls-files', '--others', '--exclude-standard']))
    picked = set()
    for rel in paths:
        rel = rel.replace('\\', '/')
        if rel.startswith('tests/') and Path(rel).name.startswith('test_'):
            picked.add(Path(rel).name)
        elif rel.startswith('modules/') and rel.endswith('.py'):
            stem = Path(rel).stem
            picked.update(p.name for p in (ROOT / 'tests').glob('test_%s*' % stem))
    return sorted(picked & set(list_test_files()))


def resolve_files(args):
    """--files 的参数规整成 tests/ 下的文件名；--changed 为空时回落全量。"""
    if args.changed:
        files = changed_files()
        print('--changed: %d 个测试文件（%s）'
              % (len(files), ', '.join(files) if files else '无，回落全量'))
        return files or list_test_files()
    if args.files:
        picked = []
        for a in args.files:
            cand = [Path(a), ROOT / 'tests' / a, ROOT / a]
            hit = next((c for c in cand if c.is_file()), None)
            if hit is None:
                print('--files: 找不到 %s' % a)
                continue
            name = hit.name
            if name in IGNORED:
                print('--files: %s 在 ignore 名单里，跳过' % name)
                continue
            picked.append(name)
        if not picked:
            sys.exit(2)
        return sorted(set(picked))
    return list_test_files()


def clear_run_dirs(root):
    """清掉 root 下上次运行的 shard-*/appdata-*（不动 durations.json 等其它
    内容——root 也可能被指到 .pytest-tmp，那里放着 durations.json）。"""
    root = Path(root)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name.startswith(('shard-', 'appdata-')):
            shutil.rmtree(child, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description='并行跑全量 pytest')
    ap.add_argument('--jobs', '-j', type=int, default=0,
                    help='并行组数，缺省 min(CPU 核数, 6)')
    ap.add_argument('--files', nargs='+', default=[],
                    help='只跑指定文件（tests/ 下相对路径或文件名）')
    ap.add_argument('--changed', action='store_true',
                    help='只跑相对 main 有改动的模块对应的测试（找不到则全量）')
    ap.add_argument('--basetemp-root', default=os.path.join(tempfile.gettempdir(),
                                                            'wp-test-shards'),
                    help='各组 basetemp 的父目录，缺省系统临时目录下的 '
                         'wp-test-shards（每组一个 shard-<i> 子目录，运行前 '
                         '清空、结束后删除）；E: 是 USB 机械盘 fsync 慢约 10 '
                         '倍，别指回仓库内的 .pytest-tmp')
    args = ap.parse_args()

    jobs = args.jobs or min(os.cpu_count() or 1, 6)
    files = resolve_files(args)
    durations = load_durations()
    shards = make_shards(files, jobs, durations)
    # 上次残留（含上次异常退出没删的）先清空；本次的跑完即删，C: 不留垃圾
    root = Path(args.basetemp_root)
    made_root = not root.exists()
    clear_run_dirs(root)
    root.mkdir(parents=True, exist_ok=True)

    expected, err = collect_total(files)
    if expected is None:
        print('--collect-only 失败：\n%s' % err)
        sys.exit(1)

    print('%d 个文件分 %d 组并行（collect-only 基线 %d 条）'
          % (len(files), len(shards), expected))
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        results = list(pool.map(
            lambda s: run_shard(s[0], s[1], args.basetemp_root),
            enumerate(shards, 1)))
    total_sec = time.perf_counter() - t0
    # C: 空间有限，不留垃圾：自己建的根目录整棵删，用户指定的已有目录只清子目录
    if made_root:
        shutil.rmtree(root, ignore_errors=True)
    else:
        clear_run_dirs(root)

    totals = {}
    bad = []
    for r in results:
        counts = parse_counts(r['out'])
        got = sum(counts.values())
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        ok = r['code'] == 0
        if not ok:
            bad.append(r['idx'])
        print('  shard %d/%d: %2d 文件 %5d 条 %6.1fs %s'
              % (r['idx'], len(shards), len(r['files']), got, r['sec'],
                 'ok' if ok else 'FAILED'))

    measured = {}
    for r in results:
        measured.update(parse_durations(r['out']))
    if measured:
        merged = dict(durations)
        merged.update({Path(k).name: round(v, 3) for k, v in measured.items()})
        save_durations(merged)

    passed = totals.get('passed', 0)
    failed = totals.get('failed', 0) + totals.get('error', 0)
    skipped = totals.get('skipped', 0)
    print('%d passed, %d failed, %d skipped in %.1fs'
          % (passed, failed, skipped, total_sec))
    got_total = sum(totals.values())
    if got_total != expected:
        print('条数不一致：collect-only %d != 分组合计 %d（可能有组收集失败）'
              % (expected, got_total))
        bad.append(-1)
    else:
        print('条数核对：collect-only %d == 分组合计 %d，一致' % (expected, got_total))

    for r in results:
        if r['code'] != 0:
            print('---- shard %d 失败片段 ----\n%s' % (r['idx'],
                                                    failure_excerpt(r['out'])))
    if bad:
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
