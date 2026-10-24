# -*- coding: utf-8 -*-
"""决策提供者：Fallback 与可替换 Provider。"""
from modules import decision


class Failing:
    def plan(self, task, fileNames, paths):
        raise decision.DecisionError('offline')


class Lexical:
    def plan(self, task, fileNames, paths):
        return {'mode': 'fallback', 'suggestion': paths[0] if paths else '',
                'source': 'lexical', 'alternatives': []}


def test_fallback_provider_switches_to_lexical():
    p = decision.FallbackProvider(Failing(), Lexical())
    out = p.plan('task', [], ['D:/x'])
    assert out['source'] == 'lexical'
    assert out['suggestion'] == 'D:/x'
    assert out['error'] == 'offline'


def test_plan_uses_replaceable_provider():
    old = decision.getProvider()

    class Fake:
        def plan(self, task, fileNames, paths):
            return {'mode': 'auto', 'suggestion': 'E:/fake',
                    'source': 'fake', 'alternatives': [], 'latencyMs': 1}

    try:
        decision.setProvider(Fake())
        out = decision.plan('一个从未出现过的任务名-xyz', [])
        assert out['source'] == 'fake'
        assert out['suggestion'] == 'E:/fake'
    finally:
        decision.setProvider(old)
