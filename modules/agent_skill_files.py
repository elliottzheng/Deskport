# -*- coding: utf-8 -*-
"""技能包里的附件：一个技能目录除 SKILL.md 之外还可以带工具文件。

技能的定位是「外部智能体的一次自举」——手册和它要用的客户端应该在同一个包里，
所以这里按目录分发，而不是另开一套 SDK 通道（app.py 显式登记，共用 agent_api 蓝图）:

    GET /api/agent/skills/<技能>/<文件>          {name, file, content, sha256}
    GET /api/agent/skills/<技能>/<文件>?raw=1    纯文本，给 `curl -o` 直接落盘
    GET /api/agent/skills/<技能>/pack.zip        整包 zip（含现成的 .deskport.json）

只发 `.agents/skills/<技能>/` 下、扩展名在白名单内的普通文件；名称与文件名都走
白名单正则，`..`、子目录、隐藏文件一律拒绝（400），不存在 404 并列出可选项。

整包是给「一条链接 + 一个令牌」准备的：解开就是一个技能目录，`.deskport.json`
按本次请求的地址生成（url/node，令牌单独给，绝不进包）。

组装逻辑 `buildSkillZip` 是共用的：需令牌的 pack.zip 传原文件字节，公开的
`/api/agent/skill.zip`（modules/agent_tools.py）传按本次请求替换过
`{{BASE}}`/`{{VERSION}}` 的 SKILL.md——两处都是单一来源实时组装，不复制文件。
"""
import hashlib
import io
import json
import os
import re
import zipfile

from flask import Response, jsonify, request

from modules import agent_handbook, meta
from modules.agent_api import requireAgentScope, blueprint
from modules.agent_skills import _skillsDir

_SKILL_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
# 附件名：<名>.<ext>；另放行 <名>.example.<ext> 形式的样例文件（driver.example.json，
# 驱动首次运行照它抄一份 driver.json）。a.b.py 这类双扩展仍拒绝（有测试锁着）。
_FILE_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}\.(?:example\.)?(?:md|py|json|txt)$')
MAX_BYTES = 2 * 1024 * 1024
_EXCLUDED = {'SKILL.md'}          # 手册走 /api/agent/skills/<技能>，这里只发附件
# deskport.py 自带版本常量，装配时盖成本次构建号（2026-11-14 用户提议的修正：
# 版本跟着脚本走，不依赖用户手动维护 SKILL.md 里的版本行——不遵守也不完蛋，
# 因为根本没有"遵守"这个动作）。hub 凭它判定客户端新旧，见 app.skillVersionHeaders。
VERSION_PLACEHOLDER = '__SKILL_VERSION__'
VERSION_STAMPED = ('deskport.py',)


def stampedTool(name, blob):
    """工具字节的最终形态：deskport.py 的版本占位符盖成本次构建号，其余原样。"""
    if name in VERSION_STAMPED and VERSION_PLACEHOLDER.encode() in blob:
        return blob.replace(VERSION_PLACEHOLDER.encode(), meta.BUILD.encode())
    return blob


def _skillFileEntries(skill):
    """技能目录下的附件清单（名称排序，SKILL.md 不计）。"""
    root = os.path.join(_skillsDir(), skill)
    if not os.path.isdir(root):
        return []
    entries = []
    for name in sorted(os.listdir(root)):
        if name in _EXCLUDED or not _FILE_RE.match(name):
            continue
        path = os.path.join(root, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path) or stat.st_size > MAX_BYTES:
            continue
        entries.append({'name': name, 'size': stat.st_size, 'mtime': int(stat.st_mtime)})
    return entries


@blueprint.route('/api/agent/skills/<skill>/<name>')
def agentSkillFileGet(skill, name):
    """技能附件原文；`?raw=1` 给文本，方便 `curl -o deskport.py`。"""
    agent, err = requireAgentScope()
    if agent is None:
        return err
    if not _SKILL_RE.match(skill) or not _FILE_RE.match(name):
        return jsonify({'error': '名称不合法'}), 400
    entries = _skillFileEntries(skill)
    entry = next((e for e in entries if e['name'] == name), None)
    if entry is None:
        return jsonify({'error': '附件不存在', 'skill': skill,
                        'available': [e['name'] for e in entries]}), 404
    path = os.path.join(_skillsDir(), skill, name)
    with open(path, 'rb') as f:      # sha256 按最终交付字节算，与下载后校验一致
        blob = stampedTool(name, f.read())
    if request.args.get('raw') in ('1', 'true', 'yes'):
        resp = Response(blob, mimetype='text/plain')
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    else:
        resp = jsonify({'name': skill, 'file': name,
                        'content': blob.decode('utf-8', 'replace'),
                        'sha256': hashlib.sha256(blob).hexdigest()})
    resp.headers['X-Skill-File'] = name
    resp.headers['X-Skill-Sha256'] = hashlib.sha256(blob).hexdigest()
    return resp


def _packConfig():
    """整包里附带的 .deskport.json：地址取本次请求，默认执行点 local；**不含令牌**。

    url 走 agent_handbook.baseUrl()——与手册同一口径：trustProxyProto 打开时
    按 X-Forwarded-Proto 印出 https（公网入口下印 http 会指到一个 501 的地址）。
    node/host 两个键同值（批次 J）：node 是新名，host 是旧名仍接受，新老客户端都不用改。
    """
    return {
        '_说明': 'url = 下载这个包用的地址；node = 默认执行点（local 是本机，也可填 SSH '
                '主机名，见 GET /api/agent/hosts；host 是旧名，与 node 同值）。令牌放同目录 '
                '.agent-token 文件或环境变量 DESKPORT_TOKEN，不要写进这个文件。',
        'url': agent_handbook.baseUrl(),
        'node': 'local',
        'host': 'local',
    }


def renderedSkillManual(skill):
    """SKILL.md 原文按本次请求替换 {{BASE}}/{{VERSION}}（与 handbook 一致）。

    「如含即换」：没有占位符就原样返回；文件不存在返回 None。公开整包用它，
    解开即可照做，地址就是本次下载用的地址。
    """
    path = os.path.join(_skillsDir(), skill, 'SKILL.md')
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        text = f.read()
    return agent_handbook.render(text, agent_handbook.baseUrl(), meta.BUILD)


def buildSkillZip(skill, manual=None):
    """整包字节与条目数：SKILL.md（manual 给了用渲染后的文本，否则原文件字节）
    + 白名单附件 + 现成的 .deskport.json；技能目录/SKILL.md 不存在返回 None。

    pack.zip（需令牌）与 /api/agent/skill.zip（公开、占位符已替换）共用这一处；
    附件只认 _skillFileEntries 的白名单，__pycache__、子目录、隐藏文件天然进不来。
    deskport.py 出版前盖版本号（stampedTool）——装配即版本，客户端零手动。
    """
    root = os.path.join(_skillsDir(), skill)
    manualPath = os.path.join(root, 'SKILL.md')
    if not os.path.isfile(manualPath):
        return None
    entries = _skillFileEntries(skill)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        if manual is None:
            z.write(manualPath, '%s/SKILL.md' % skill)
        else:
            z.writestr('%s/SKILL.md' % skill, manual)
        for entry in entries:
            arcname = '%s/%s' % (skill, entry['name'])
            if entry['name'] in VERSION_STAMPED:
                with open(os.path.join(root, entry['name']), 'rb') as f:
                    z.writestr(arcname, stampedTool(entry['name'], f.read()))
            else:
                z.write(os.path.join(root, entry['name']), arcname)
        z.writestr('%s/.deskport.json' % skill,
                   json.dumps(_packConfig(), ensure_ascii=False, indent=2) + '\n')
    return buf.getvalue(), len(entries) + 2   # 手册 + 附件 + 配置


@blueprint.route('/api/agent/skills/<skill>/pack.zip')
def agentSkillPack(skill):
    """整包下载：解开就是一个技能目录——手册 + 全部附件 + 现成的 .deskport.json。

    给「一条链接 + 一个令牌」用：
        curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/pack.zip" -o p.zip
        unzip -oq p.zip -d ~/.kimi-code/skills/ && export DESKPORT_TOKEN=$T
    """
    agent, err = requireAgentScope()
    if agent is None:
        return err
    if not _SKILL_RE.match(skill):
        return jsonify({'error': '名称不合法'}), 400
    packed = buildSkillZip(skill)
    if packed is None:
        names = [s for s in (os.listdir(_skillsDir()) if os.path.isdir(_skillsDir()) else [])
                 if os.path.isfile(os.path.join(_skillsDir(), s, 'SKILL.md'))]
        return jsonify({'error': '技能不存在', 'available': names}), 404
    blob, count = packed
    resp = Response(blob, mimetype='application/zip')
    resp.headers['Content-Disposition'] = 'attachment; filename="%s-skill.zip"' % skill
    resp.headers['X-Skill-Files'] = str(count)   # 手册 + 附件 + 配置
    return resp