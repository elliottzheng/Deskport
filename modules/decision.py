# -*- coding: utf-8 -*-
"""AI 落点判断 — 候选目录索引 + SemIf 决策 + 判定门。

策略：SemIf + 裸路径 + "选最具体目录"规则。
不默认加目录索引（最近修改列表会强化父目录吸引力）。
"""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Protocol

import config
from modules.fsutil import SKIP_DIRS

API_URL = 'https://api.siliconflow.cn/v1/systemone'
MODEL = 'SemIf'
LEAF_RULE = ('\n判断规则：如果任务明显属于某个候选目录下面的子目录，请选那个子目录；'
             '只有当子目录不存在时，才选父目录。落点要尽可能具体。')
MAX_CANDIDATES = 12
INDEX_TTL = 120

_indexLock = threading.Lock()
_indexCache = {'at': 0.0, 'dirs': []}
_planCache = {}
_planLock = threading.Lock()


def _listDirs(path):
    try:
        with os.scandir(path) as it:
            return sorted(e.name for e in it
                          if e.is_dir(follow_symlinks=False)
                          and not e.name.startswith('.')
                          and e.name.lower() not in SKIP_DIRS)
    except OSError:
        return []


def _walk(maxDepth=2):
    """工作目录下 0..maxDepth 层的目录 (含根)。"""
    dirs = []
    for root in config.getWorkDirs():
        dirs.append(root)
        level = [root]
        for _ in range(maxDepth):
            nxt = []
            for parent in level:
                for name in _listDirs(parent):
                    p = os.path.join(parent, name)
                    dirs.append(p)
                    nxt.append(p)
            level = nxt
    return dirs


def getIndex():
    now = time.time()
    with _indexLock:
        if now - _indexCache['at'] < INDEX_TTL and _indexCache['dirs']:
            return list(_indexCache['dirs'])
    dirs = _walk()
    with _indexLock:
        _indexCache['at'] = now
        _indexCache['dirs'] = dirs
    return list(dirs)


def _bigrams(text):
    text = ''.join(ch for ch in (text or '').lower() if ch.isalnum())
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else set()


def _score(taskGrams, path):
    parts = [p for p in path.replace('/', '\\').split('\\') if p]
    grams = set()
    for part in parts[-2:]:
        grams |= _bigrams(part)
    return len(taskGrams & grams)


def _ensurePath():
    fallback = config.getFallbackDir()
    if fallback and not os.path.isdir(fallback):
        try:
            os.makedirs(fallback, exist_ok=True)
        except OSError:
            pass


def buildCandidates(task):
    """按任务词面挑不超过 12 个候选: 词面靠前的 + 兜底目录 + 各工作目录根。"""
    _ensurePath()
    taskGrams = _bigrams(task)
    dirs = getIndex()
    scored = sorted((( _score(taskGrams, p), p) for p in dirs),
                    key=lambda x: (-x[0], len(x[1]), x[1]))
    picks = [p for _, p in scored[:MAX_CANDIDATES - 3]]
    for mandatory in ([config.getFallbackDir()] + config.getWorkDirs()):
        if mandatory and os.path.isdir(mandatory) and mandatory not in picks:
            picks.append(mandatory)
    return picks[:MAX_CANDIDATES]


def _criteria(paths):
    notes = config.getDirNotes()
    out = {}
    for i, path in enumerate(paths):
        note = notes.get(path)
        out['c%d' % (i + 1)] = path + ('  [备注: %s]' % note if note else '')
    return out


def _callModel(task, fileNames, paths):
    key = config.getDecisionApiKey()
    if not key:
        return None
    state = '任务：%s\n涉及文件：%s' % (task, '、'.join(fileNames) if fileNames else '（无）')
    payload = {'model': MODEL, 'state': state, 'questions': {'dst': {
        'type': 'choice',
        'instructions': '这个任务的落点应该是哪个目录？' + LEAF_RULE,
        'criteria': _criteria(paths)}}}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    lastError = None
    for attempt in range(2):
        req = urllib.request.Request(API_URL, data=body, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
            'User-Agent': 'web-platform/2.0'})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.load(resp)
            return data['answers']['dst']
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            lastError = e
            if attempt == 0:
                time.sleep(0.4)
    return {'error': str(lastError)}


def _lexicalBest(task, paths):
    taskGrams = _bigrams(task)
    ranked = sorted((( _score(taskGrams, p), p) for p in paths),
                    key=lambda x: (-x[0], len(x[1]), x[1]))
    return ranked


class DecisionError(Exception):
    """模型不可用/返回不合法；由 FallbackProvider 决定是否降级。"""


class DecisionProvider(Protocol):
    def plan(self, task, fileNames, paths): ...


def _modelResult(ans, paths, latency):
    probs = ans.get('probabilities') or {}
    ordered = sorted(probs.items(), key=lambda kv: -kv[1])
    if not ordered:
        return {'mode': 'fallback', 'suggestion': config.getFallbackDir(),
                'confidence': 0, 'margin': 0, 'alternatives': [],
                'source': 'model', 'latencyMs': latency}
    topKey, topP = ordered[0]
    secondP = ordered[1][1] if len(ordered) > 1 else 0.0
    margin = topP - secondP
    index = int(topKey[1:]) - 1
    top = paths[index] if 0 <= index < len(paths) else config.getFallbackDir()
    conf = float(ans.get('confidence') or topP)
    if conf >= 0.6 and margin >= 0.4:
        mode = 'auto'
    elif conf >= 0.35:
        mode = 'choose'
    else:
        mode = 'fallback'
    alternatives = []
    for k, p in ordered[:3]:
        i = int(k[1:]) - 1
        alternatives.append({'path': paths[i] if 0 <= i < len(paths) else '',
                             'p': round(p, 4)})
    suggestion = top if mode != 'fallback' else (config.getFallbackDir() or top)
    return {'mode': mode, 'suggestion': suggestion, 'confidence': round(conf, 4),
            'margin': round(margin, 4), 'alternatives': alternatives,
            'source': 'model', 'latencyMs': latency}


def _lexicalResult(task, paths, latency, error=None):
    ranked = _lexicalBest(task, paths)
    bestScore, best = ranked[0]
    return {
        'mode': 'auto' if bestScore >= 3 else 'fallback',
        'suggestion': best if bestScore >= 3 else config.getFallbackDir(),
        'confidence': None,
        'margin': None,
        'alternatives': [{'path': p, 'p': None} for _, p in ranked[:3]],
        'source': 'lexical',
        'error': error,
        'latencyMs': latency,
    }


class SemIfProvider:
    def plan(self, task, fileNames, paths):
        t0 = time.time()
        ans = _callModel(task, fileNames, paths)
        latency = int((time.time() - t0) * 1000)
        if not ans or 'error' in ans:
            raise DecisionError((ans or {}).get('error') or 'model unavailable')
        return _modelResult(ans, paths, latency)


class LexicalProvider:
    def plan(self, task, fileNames, paths):
        t0 = time.time()
        result = _lexicalResult(task, paths, int((time.time() - t0) * 1000))
        return result


class FallbackProvider:
    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def plan(self, task, fileNames, paths):
        try:
            return self.primary.plan(task, fileNames, paths)
        except DecisionError as e:
            result = self.fallback.plan(task, fileNames, paths)
            result['error'] = str(e)
            return result


_provider = FallbackProvider(SemIfProvider(), LexicalProvider())


def getProvider():
    return _provider


def setProvider(provider):
    global _provider
    _provider = provider


def plan(task, fileNames):
    """返回 {mode, suggestion, alternatives, source, latencyMs}。
    mode: auto(直接执行) / choose(top3 点选) / fallback(兜底)。"""
    task = (task or '').strip()
    fileNames = [os.path.basename(p) for p in (fileNames or []) if p]
    paths = buildCandidates(task)
    cacheKey = hashlib.sha256(('\x00'.join(
        [task, ','.join(fileNames), ','.join(paths)])).encode('utf-8')).hexdigest()
    with _planLock:
        cached = _planCache.get(cacheKey)
        if cached:
            return dict(cached, cached=True)

    result = _provider.plan(task, fileNames, paths)

    with _planLock:
        if len(_planCache) > 200:
            _planCache.clear()
        _planCache[cacheKey] = result
    return dict(result)
