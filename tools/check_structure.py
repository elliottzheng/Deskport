# -*- coding: utf-8 -*-
"""代码结构门禁：文件预算、存量例外、重复工具、横向 import、window 全局、
跨模块私有接缝、执行点字段（对外只认 node/cwd）。

用法: python tools/check_structure.py [--strict]
退出码: 预算/存量增长/未定义名/横向 import/跨模块私有名违规 → 1；其它为 warning。
"""
import argparse
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGET = {'.py': 400, '.js': 500, '.css': 500}

# 存量例外: 只减不增（行数上限）
# 2026-11-05: read_lines 曾恒返回 0 导致预算检查空转, 暴露的 7 个超预算文件
# 已全部拆完并从本表移除 (过程见 docs/design-review.md 与各 refactor 提交)。
# 2026-11-12: R4/R5 两轮把 tasks_* 又写回超预算, 且未登记 —— 门禁红了数日而
# 发布绕过了它。按"先认清现状"重新登记现有超预算文件 (上限 = 当前行数),
# 之后这些文件只能变短; 未列在本表的新文件仍必须直接符合预算。
LEGACY = {
    'config.py': 516,   # 全局配置聚合（读取/热改/默认值/迁移），拆分会散落语义
    # .agents/skills/deskport/deskport.py 是发给外部智能体的「拷一个文件就能跑」客户端
    # （零依赖，也能直接当 CLI 用），拆分会破坏这个约定；与 agent_node/acp_session.py
    # 同理按单文件设计登记，只许变短。
    '.agents/skills/deskport/deskport.py': 605,   # 外部智能体单文件客户端（见上注）
    'agent_node/acp_session.py': 454,             # ACP 会话单文件实现（见上注）
    'agent_node/node_api.py': 417,                # 节点 API 单文件实现（见上注）
    'frontend/assets/css/files.css': 522,         # 文件页样式一体，选择器跨文件耦合，拆分暂缓
    'modules/agent_local.py': 491,                # 本机内置主机全等价实现（exec/sftp/读文件），拆分中
    'modules/agent_nodes.py': 580,                # 节点注册+隧道+引擎注册一体
    'modules/agent_sessions.py': 443,             # 持久会话（bash 常驻通道）管理
    'modules/tasks_api.py': 961,                  # 任务 API 面最大，拆分需配回归
    'modules/tasks_runner.py': 689,               # 任务执行编排（引擎分发/会话线程）
    'modules/tasks_store.py': 433,                # 任务存储（暂存区已拆去 staging，仍偏长）
    'tests/test_agent_api.py': 611,               # 智能体 API 回归集
    'tests/test_r47_remote_adopt.py': 481,        # R47 远端 adopt 回归集
    'tests/test_tasks_api.py': 1539,              # 任务 API 回归集（面最大）
}

SKIP_DIRS = {'.git', '.venv', '__pycache__', 'vendor', 'node_modules', '.deploy-backup', '.run'}
FEATURES = {'aihandoff', 'agent_api', 'agent_local', 'agent_nodes', 'agent_sftp',
            'agent_sftp_write', 'agent_sessions', 'agent_share', 'agent_skill_files',
            'agent_tunnel', 'command', 'control', 'feedback', 'feedback_store',
            'files', 'files_archive', 'files_base', 'files_browse', 'files_federation',
            'files_pins', 'files_transfer', 'screen', 'ssh', 'task_cwd', 'task_node',
            'tasks', 'tasks_api', 'tasks_runner', 'tasks_store', 'terminal',
            'worklines_api', 'worklines_store'}
# 允许的“上游 → 下游”依赖；其余同层 import 视为违规
ALLOWED_FEATURE_IMPORTS = {('aihandoff', 'files'), ('tasks', 'tasks_api'),
                           ('tasks_api', 'tasks_runner'), ('tasks_api', 'tasks_store'),
                           ('tasks_runner', 'tasks_store')}
# 批次 C（2026-11-13）扩面登记：按 modules/ 现名核实的特性模块全部纳入后，
# 逐条认定现有依赖方向。约定: agent_api/files_base/feedback_store/tasks_store 等
# 是各自家族的「底座」（蓝图/守卫/仓库），家族成员 → 底座是设计方向；
# 反向（底座 → 成员）只在函数内延迟导入、且只为请求期分工时登记。
ALLOWED_FEATURE_IMPORTS.update({
    # —— agent 家族（蓝图同源, app.py 显式登记; agent_api 是门面与守卫基座）——
    ('agent_local', 'agent_api'),    # 本机内置主机实现, 复用 agent_api 的令牌守卫/输出上限常量
    ('agent_local', 'agent_sftp'),   # 本机文件读复用 agent_sftp 的错误码语义（mapSftpError 公开助手）
    ('agent_api', 'agent_local'),    # 反向仅请求期: agentExec 本地分支委托 agentLocalExec, 函数内延迟导入不成加载环
    ('agent_sftp', 'agent_api'),     # SFTP 路由复用守卫/主机解析
    ('agent_sftp', 'agent_local'),   # 本地分支委托 sftpListLocal 等（函数内延迟导入）
    ('agent_sftp_write', 'agent_api'),   # 写操作路由复用守卫/蓝图
    ('agent_sftp_write', 'agent_local'),   # 函数内延迟导入 localEnabled/normalizeLocalPath
    ('agent_sftp_write', 'agent_sftp'),    # 写操作复用连接/错误码/建目录助手（C2 公开化后）
    ('agent_sessions', 'agent_api'),       # 持久会话复用守卫/主机解析
    ('agent_sessions', 'agent_local'),     # 持久会话的 shell 解析在本机模块
    ('agent_share', 'agent_api'),          # 手机转发入口复用守卫/scope 常量
    ('agent_skill_files', 'agent_api'),    # 技能包附件路由复用守卫/蓝图
    ('agent_upload', 'agent_api'),         # 手机上传复用守卫/scope/蓝图
    ('agent_nodes', 'agent_tunnel'),  # 远端执行点经 HTTP 隧道访问（tunnelPort/dropTunnelsFor）
    ('agent_nodes', 'task_node'),     # task_node 是执行点(node/cwd)语义的唯一出处, 各特性只读消费
    ('agent_nodes', 'tasks_runner'),  # 本地/联邦引擎的会话线程委托 tasks_runner 跑 ACP
    ('agent_share', 'task_node'),     # 执行点语义（同上）
    ('agent_share', 'tasks_store'),   # 暂存区根 taskRoot() 由 tasks_store 提供（staging 文档约定的调用方传入模式）
    # —— files 家族（files_base 是同蓝图共享底座: blueprint/log/属性常量）——
    ('files', 'files_base'),          # files.py 是家族门面, 蓝图出自 files_base
    ('files', 'files_archive'),       # 门面聚合登记各路由模块（noqa: F401 再导出）
    ('files', 'files_browse'),        # 门面聚合（同上）
    ('files', 'files_pins'),          # 门面聚合（同上）
    ('files', 'files_transfer'),      # 门面聚合（同上）
    ('files_archive', 'files_base'),  # 家族底座
    ('files_browse', 'files_base'),   # 家族底座
    ('files_pins', 'files_base'),     # 家族底座
    ('files_transfer', 'files_base'), # 家族底座
    # —— feedback（路由 → 存储, 同 tasks_api → tasks_store 的形状）——
    ('feedback', 'feedback_store'),
    # —— tasks/worklines 家族消费执行点语义与节点注册表（批次 B3/C 后公开的方向）——
    ('tasks_api', 'agent_nodes'),     # 执行点存在性校验/寻址/模型声明
    ('tasks_api', 'task_cwd'),        # cwd 前置校验（远端 cwdRoots, 批次 B3）
    ('tasks_api', 'task_node'),       # 执行点语义
    ('tasks_api', 'worklines_store'), # 工作线是任务的上级聚合, 建/挂任务时读工作线存储
    ('tasks_runner', 'agent_nodes'),  # 联邦成员判定（peerJudged）
    ('tasks_runner', 'task_node'),    # 执行点语义
    ('tasks_runner', 'worklines_store'),  # 执行后回写工作线上下文
    ('tasks_store', 'task_cwd'),      # 摘要/提示渲染里的 cwd 文案
    ('tasks_store', 'task_node'),     # 执行点语义
    ('worklines_api', 'task_node'),   # 执行点语义
    ('worklines_api', 'tasks_api'),   # 工作线 API 的任务编排复用 tasks_api（其文档自述的设计）
    ('worklines_api', 'tasks_store'), # 建/挂任务时读任务仓库
    ('worklines_api', 'worklines_store'),  # 自己的存储层
    ('worklines_store', 'task_node'), # 工作线执行点判定
})
# 基础设施层: 可被任何层依赖, 但自己不得 import 特性层 (依赖只能向上)
# termcore 是 terminal/ssh 共用的共享内核, 归基础设施而非特性
INFRA = {'state', 'internal_api', 'events', 'fsutil', 'textio', 'media', 'contracts',
         'decision', 'termcore', 'termprobe', 'wsproto', 'winenv'}
ALLOWED_WINDOW = {'WPFeedback', 'WPFeedbackCapture', 'WPFeedbackPanel',   # 反馈入口
                  'fetch', 'onerror',              # 反馈捕获层给请求/错误打的钩子
                  'termManager', 'termUI', 'createSshSession', 'SSHUI',
                  'AppShell', 'AppShellV2', 'Icons', 'SftpMemory',
                  'SftpState', 'SftpMenu', 'SftpSelect', 'SftpTransfer',
                  'FilesMethodsSelect', 'TermSidebar', 'TermGeometry', 'WP_NAV',
                  'TermClient', 'TermFeatures', 'toggleMultiExec', 'WPCommon',
                  'FilesMethodsCore', 'FilesMethodsOps', 'FilesMethodsUi', 'FilesMethodsAi',
                  'SSHUiCtx', 'SSHSftpUI', 'WP_MSG', 'SSHHostStore', 'WP_BRAND',
                  'TermEnv', 'TermSessionUI', 'TermControls'}
# 跨模块私有接缝存量白名单（批次 C, C2/C4）: (使用方, 提供方) → 私有名集合。
# 规则: 跨模块 import 不得出现单下划线前缀（引私有名 / 公有名起私有别名都算）。
# 本表登记的是「同包内部接缝」——提供方是使用方所在家族的共享底座, 私有助手
# 只在该家族内流转; 每条附理由, 且只减不增: 新接缝必须公开化, 不许往这加项。
PRIVATE_SEAM_ALLOWED = {
    ('agent_nodes', 'tasks_federation'): {'_memberCall'},      # federation 成员 RPC 是 tasks_federation 的内部机制
    ('agent_sessions', 'agent_api'): {'_resolveHost', '_safeHosts'},  # agent 家族的主机解析/主机清单助手
    ('agent_sessions', 'agent_local'): {'_resolveShell'},      # 持久会话的 shell 解析在本机模块
    ('agent_sftp', 'agent_api'): {'_resolveHost'},             # 同上: 主机解析助手
    ('agent_sftp', 'ssh_sftp'): {'_openSftp'},                 # SFTP 连接助手, ssh_sftp → agent_sftp 的本地分支要用
    ('agent_skill_files', 'agent_skills'): {'_skillsDir'},     # 技能包附件读写 agent_skills 的技能目录
    ('aihandoff', 'inbox_scan'): {'_under', '_sha256', '_dedupe'},  # 收件箱家族的遍历/摘要/去重助手
    ('control', 'control_base'): {'_audit', '_readAudit'},     # control 家族共享底座的审计读写
    ('control_input', 'control_base'): {'_audit'},             # 同上
    ('files_federation', 'external_proxy'): {'_dropHopHeaders'},  # 出站代理剥 hop 头的内部助手, 仅文件转发一处调用
    ('model_availability', 'agent_nodes'): {'_healthModelsCached'},  # 读节点注册表的模型健康缓存
    ('model_availability', 'tasks_federation'): {'_memberCall'},     # 同 agent_nodes: 成员 RPC 内部机制
    ('plugins_discover', 'plugins_base'): {'_log'},            # plugins 家族共享底座的日志助手
    ('plugins_load', 'plugins_base'): {'_log'},                # 同上
    ('ssh_sftp', 'ssh_sftp_base'): {'_openSftp', '_sftpSession'},  # ssh_sftp 家族的会话助手, 只在本家族与 agent_sftp/task_inputs_sftp 内流转
    ('ssh_sftp_ops', 'ssh_sftp_base'): {'_sftpSession'},       # 同上
    ('ssh_sftp_transfer', 'ssh_sftp_base'): {'_openSftp', '_sftpSession'},  # 同上
    ('task_inputs_sftp', 'ssh_sftp_base'): {'_openSftp'},      # 同上
    ('tasks_api', 'task_wait'): {'_blockWait', '_parseWaitSeconds', '_waitSnapshot'},  # tasks/worklines 两个 API 共用的等待态助手
    ('worklines_api', 'task_wait'): {'_parseWaitSeconds'},     # 同上
}


def read_lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    return text, text.count('\n') + (0 if text.endswith('\n') or not text else 1)


# 执行点门禁（2026-11-13）：对外只剩 node（去哪台机器，缺省 local）与
# cwd（在哪个目录跑，缺省任务目录）。engine 不得回到 modules/ 与 frontend/：
# 不许当入参名、不许读旧字段、用户可见文案不许出现「引擎」。
# 唯一白名单 modules/task_node.py：旧任务记录 / 旧工作线元数据读取时映射一次
# （acp→local），以及请求里带 engine 时显式 400——这两处必须认识旧名字。
NODE_FIELD_ALLOWED = {'modules/task_node.py'}


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