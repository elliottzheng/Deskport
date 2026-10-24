# -*- coding: utf-8 -*-
"""纯函数回归: 路径白名单、命名、状态原子写、事件流、代理契约、落点评分。"""
import json
import os

import config
import modules.internal_api as internal_api
import modules.events as events
from modules import aihandoff, decision, state


def test_under_path_whitelist():
    roots = [r'D:\work']
    assert aihandoff._under(r'D:\work\a.txt', roots)
    assert aihandoff._under(r'D:\work', roots)
    assert not aihandoff._under(r'D:\work2\a.txt', roots)
    assert not aihandoff._under(r'D:\wor\a.txt', roots)


def test_safe_name():
    assert aihandoff._safeName('a<>:"/\\|?*b') == 'ab'
    assert len(aihandoff._safeName('x' * 80)) == 40
    assert aihandoff._safeName('   ') == '未命名任务'


def test_unique_file(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('x', encoding='utf-8')
    new_path, ok = aihandoff._uniqueFile(str(target))
    assert ok is True
    assert os.path.basename(new_path) == 'a (1).txt'


def test_internal_api_urls(monkeypatch):
    monkeypatch.setattr(config, 'getSessiondUrl',
                        lambda kind='local': 'http://127.0.0.1:6791')
    monkeypatch.setattr(config, 'getSessiondToken', lambda kind='local': 'tok')
    assert internal_api.wsUrl('local') == 'ws://127.0.0.1:6791/ws/terminal'
    assert internal_api.wsUrl('local', 'session=x') == \
        'ws://127.0.0.1:6791/ws/terminal?session=x'
    assert internal_api.wsUrl('ssh') == 'ws://127.0.0.1:6791/ws/ssh'
    assert internal_api.headers('local') == {internal_api.TOKEN_HEADER: 'tok'}
    assert internal_api.PROTO >= 1
    assert 'replay' in internal_api.CAPS


def test_state_atomic_json(tmp_path):
    path = str(tmp_path / 'x.json')
    state.atomicWriteJson(path, {'a': 1})
    with open(path, encoding='utf-8') as f:
        assert json.load(f) == {'a': 1}
    assert not [p for p in os.listdir(tmp_path) if p.startswith('.tmp-')]


def test_state_append_jsonl(tmp_path):
    path = str(tmp_path / 'e.jsonl')
    state.appendJsonl(path, {'n': 1})
    state.appendJsonl(path, {'n': 2})
    with open(path, encoding='utf-8') as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert lines == [{'n': 1}, {'n': 2}]


def test_events_roundtrip(tmp_path, monkeypatch):
    target = str(tmp_path / 'audit.jsonl')
    monkeypatch.setattr(events, 'eventPath', lambda: target)
    events.log('unit.test', 'target', 'ok', 'detail', '127.0.0.1')
    events.log('unit.test2', 'x', 'fail')
    rows = events.recent(10)
    assert len(rows) == 2
    assert rows[0]['action'] == 'unit.test'
    assert rows[1]['result'] == 'fail'


def test_decision_score():
    grams = decision._bigrams('数学提高2')
    assert '数学' in grams
    assert decision._score(grams, r'D:\work\数学提高2') > 0
    assert decision._score(grams, r'D:\work\remote') == 0

def test_canonical_name():
    assert aihandoff.canonicalName('数学提高1参考答案(1).pdf') == '数学提高1参考答案.pdf'
    assert aihandoff.canonicalName('a (2).docx') == 'a.docx'
    assert aihandoff.canonicalName('报告（3）.pdf') == '报告.pdf'
    assert aihandoff.canonicalName('报告 - 副本.pdf') == '报告.pdf'
    assert aihandoff.canonicalName('报告 副本(2).pdf') == '报告.pdf'
    assert aihandoff.canonicalName('normal.pdf') == 'normal.pdf'


def test_dedupe_merges_copy_suffix(tmp_path):
    same1 = tmp_path / 'a.txt'
    same1.write_text('same-content', encoding='utf-8')
    same2 = tmp_path / 'a(1).txt'
    same2.write_text('same-content', encoding='utf-8')
    diff = tmp_path / 'a(2).txt'
    diff.write_text('different-content', encoding='utf-8')
    items = []
    for p, mt in ((same1, 100), (same2, 200), (diff, 300)):
        items.append({'name': p.name, 'path': str(p),
                      'size': p.stat().st_size, 'mtime': mt, 'source': '微信'})
    merged = aihandoff._dedupe(items)
    by_name = {m['name']: m for m in merged}
    assert 'a.txt' in by_name
    assert len(by_name['a.txt']['dupes']) == 1
    assert os.path.basename(by_name['a.txt']['dupes'][0]['path']) == 'a(1).txt'
    assert 'a(2).txt' in by_name
    assert by_name['a(2).txt']['dupes'] == []


def test_dedupe_prefers_shallowest_name(tmp_path):
    a = tmp_path / 'x(1)(1).txt'
    a.write_text('same', encoding='utf-8')
    b = tmp_path / 'x(1).txt'
    b.write_text('same', encoding='utf-8')
    items = [{'name': p.name, 'path': str(p), 'size': p.stat().st_size,
              'mtime': 100 + i, 'source': '微信'} for i, p in enumerate((a, b))]
    merged = aihandoff._dedupe(items)
    assert len(merged) == 1
    assert merged[0]['name'] == 'x(1).txt'
    assert len(merged[0]['dupes']) == 1


def test_services_view(monkeypatch):
    monkeypatch.setattr(internal_api, 'base',
                        lambda kind='local': 'http://127.0.0.1:1' if kind == 'local' else '')
    monkeypatch.setattr(internal_api, 'health',
                        lambda kind, timeout=2: {'pid': 1, 'proto': internal_api.PROTO,
                                                 'sessions': 2})
    rows = internal_api.services()
    assert rows[0]['ok'] is True and rows[0]['sessions'] == 2
    assert rows[0]['url'].startswith('http://')
    assert rows[1]['configured'] is False and rows[1]['ok'] is False
