# -*- coding: utf-8 -*-
"""技能分发 — GET /api/agent/skills（清单）与 /api/agent/skills/<name>（原文）。

从 agent_api.py 拆出（2026-11-13 批次 FD, 文件预算）: 与 agent_sftp /
agent_skill_files 同模式挂在 agent_api 的蓝图上（app.py 显式 import 即
随 agentApiModule 一次注册）。认证、审计口径与 agent_api 完全一致
（联邦令牌同样按 full 放行, 见 agent_api.agentFromRequest）。

整包下载（pack.zip）与包内文件在 modules/agent_skill_files.py。
"""
import os
import re

from flask import jsonify

from modules.agent_api import requireAgentScope, blueprint


def _skillsDir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        '.agents', 'skills')


@blueprint.route('/api/agent/skills')
def agentSkillsList():
    """可分发的技能清单（供外部智能体自举与检测更新）。"""
    agent, err = requireAgentScope()
    if agent is None:
        return err
    root = _skillsDir()
    skills = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            skillFile = os.path.join(root, name, 'SKILL.md')
            if os.path.isfile(skillFile):
                try:
                    stat = os.stat(skillFile)
                except OSError:
                    continue
                folder = os.path.dirname(skillFile)
                extras = sorted(f for f in os.listdir(folder)
                                if f != 'SKILL.md' and os.path.isfile(os.path.join(folder, f)))
                skills.append({'name': name, 'size': stat.st_size,
                               'mtime': int(stat.st_mtime), 'files': extras,
                               'pack': '/api/agent/skills/%s/pack.zip' % name})
    return jsonify({'skills': skills})


@blueprint.route('/api/agent/skills/<name>')
def agentSkillGet(name):
    """返回技能原文（text/markdown），供智能体拉取安装到本地技能目录。"""
    agent, err = requireAgentScope()
    if agent is None:
        return err
    if not re.match(r'^[A-Za-z0-9_-]{1,40}$', name):
        return jsonify({'error': '名称不合法'}), 400
    skillFile = os.path.join(_skillsDir(), name, 'SKILL.md')
    if not os.path.isfile(skillFile):
        return jsonify({'error': '技能不存在', 'available':
                        [s for s in os.listdir(_skillsDir())
                         if os.path.isfile(os.path.join(_skillsDir(), s, 'SKILL.md'))]
                        if os.path.isdir(_skillsDir()) else []}), 404
    with open(skillFile, 'r', encoding='utf-8') as f:
        body = f.read()
    resp = jsonify({'name': name, 'content': body})
    resp.headers['X-Skill-Name'] = name
    return resp
