# -*- coding: utf-8 -*-
"""技能包里的附件：一个技能目录除 SKILL.md 之外还可以带工具文件。

技能的定位是「外部智能体的一次自举」——手册和它要用的客户端应该在同一个包里，
所以这里按目录分发，而不是另开一套 SDK 通道（app.py 显式登记，共用 agent_api 蓝图）:

    GET /api/agent/skills/<技能>/<文件>          {name, file, content, sha256}
    GET /api/agent/skills/<技能>/<文件>?raw=1    纯文本，给 `curl -o` 直接落盘

只发 `.agents/skills/<技能>/` 下、扩展名在白名单内的普通文件；名称与文件名都走
白名单正则，`..`、子目录、隐藏文件一律拒绝（400），不存在 404 并列出可选项。
"""
import hashlib
import os
import re

from flask import Response, jsonify, request

from modules.agent_api import _agentRequired, _skillsDir, blueprint

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