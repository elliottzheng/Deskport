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
按本次请求的地址生成（url/host），令牌单独给（绝不进包）。
"""
import hashlib
import io
import json
import os
import re
import zipfile

from flask import Response, jsonify, request

from modules.agent_api import _agentRequired, blueprint
from modules.agent_skills import _skillsDir

_SKILL_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
_FILE_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}\.(md|py|json|txt)$')
MAX_BYTES = 2 * 1024 * 1024
_EXCLUDED = {'SKILL.md'}          # 手册走 /api/agent/skills/<技能>，这里只发附件


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
    agent, err = _agentRequired()
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
    with open(path, 'rb') as f:      # sha256 按字节算，与下载后校验一致
        blob = f.read()
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
    """整包里附带的 .deskport.json：地址取本次请求，默认执行点 local；**不含令牌**。"""
    return {
        '_说明': 'url = 下载这个包用的地址；host = 默认执行点（local 是本机，也可填 SSH '
                '主机名，见 GET /api/agent/hosts）。令牌放同目录 .agent-token 文件或'
                '环境变量 DESKPORT_TOKEN，不要写进这个文件。',
        'url': request.host_url.rstrip('/'),
        'host': 'local',
    }


@blueprint.route('/api/agent/skills/<skill>/pack.zip')
def agentSkillPack(skill):
    """整包下载：解开就是一个技能目录——手册 + 全部附件 + 现成的 .deskport.json。

    给「一条链接 + 一个令牌」用：
        curl -s -H "X-Agent-Token: $T" "$D/api/agent/skills/deskport/pack.zip" -o p.zip
        unzip -oq p.zip -d ~/.kimi-code/skills/ && export DESKPORT_TOKEN=$T
    """
    agent, err = _agentRequired()
    if agent is None:
        return err
    if not _SKILL_RE.match(skill):
        return jsonify({'error': '名称不合法'}), 400
    root = os.path.join(_skillsDir(), skill)
    manual = os.path.join(root, 'SKILL.md')
    if not os.path.isfile(manual):
        names = [s for s in (os.listdir(_skillsDir()) if os.path.isdir(_skillsDir()) else [])
                 if os.path.isfile(os.path.join(_skillsDir(), s, 'SKILL.md'))]
        return jsonify({'error': '技能不存在', 'available': names}), 404
    entries = _skillFileEntries(skill)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(manual, '%s/SKILL.md' % skill)
        for entry in entries:
            z.write(os.path.join(root, entry['name']), '%s/%s' % (skill, entry['name']))
        z.writestr('%s/.deskport.json' % skill,
                   json.dumps(_packConfig(), ensure_ascii=False, indent=2) + '\n')
    resp = Response(buf.getvalue(), mimetype='application/zip')
    resp.headers['Content-Disposition'] = 'attachment; filename="%s-skill.zip"' % skill
    resp.headers['X-Skill-Files'] = str(len(entries) + 2)   # 手册 + 附件 + 配置
    return resp