"""就诊准备闭环（协议 v4）：归因、纠错上下文、职责四分、子问题、材料、报告证据支持。

全部离线：不出网、不使用真实端点。真实模型只在显式 --live 入口下使用。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent, RAGTool
from stage0.memory import MemoryStore


@contextmanager
def _env():
    """A temp patient store that is CLOSED before the directory is removed.

    Windows keeps the SQLite handle locked, so the store must be closed inside
    the ``with`` block — a cleanup callback registered with ``addCleanup`` runs
    after TemporaryDirectory's own cleanup and fails with WinError 32.
    """
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
        try:
            yield store
        finally:
            store.close()


def _rag(chunks=None, warnings=None):
    """Offline exact retrieval double: never touches embeddings or the network."""
    class Fixed(RAGTool):
        def __call__(self, query, **kwargs):
            return {'query': query, 'mode': 'fixed', 'corpus_version': 'test-v1',
                    'results': list(chunks or []), 'warnings': list(warnings or [])}

        def _get_retriever(self):
            raise RuntimeError('offline exact retrieval; embeddings disabled')
    return Fixed()


def _planner_script(steps):
    """A scripted planner that binds each step to a gap the code ACTUALLY
    reports in the payload — never to a hard-coded id, so the test does not
    depend on how gaps happen to be named."""
    pending = list(steps)

    def provider(payload):
        inv = payload.get('investigation') or {}
        open_gaps = inv.get('open_gaps') or []
        if not pending:
            return {'decision': 'respond'}
        tool = pending.pop(0)
        if tool == 'respond':
            return {'decision': 'respond'}
        if tool == 'memory_read':
            return {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
                    'expected_observation': '获得完整当前事实及版本',
                    'arguments': {'query': 'snapshot'}}
        gap = next((g for g in open_gaps if g['kind'] == 'evidence_missing'), None) \
            or (open_gaps[0] if open_gaps else {'gap_id': 'authority'})
        return {'decision': 'tool', 'tool': tool, 'gap_id': gap['gap_id'],
                'expected_observation': f'{tool} 的结果', 'arguments': {}}

    return provider


PLANNER_SOURCES = ('llm', 'llm_post_correction', 'system_forced', 'fallback',
                   'deterministic', 'rejected')


class AttributionTest(unittest.TestCase):
    """代码替模型做的每个决定都必须与"模型选择"分开计数。"""

    def _run(self, ddi, steps):
        with _env() as store:
            store.apply_medication_change(action='add', name='华法林', ingredients=[],
                session_id='s', turn_id='t0', source='test', dose='3mg')
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=ddi, rag_tool=_rag(), max_cycles=6,
                llm_planner_enabled=True, proposal_provider=_planner_script(steps))
            with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'}):
                response = agent.handle(CareEvent('user_message', '看看我的用药风险'),
                                        session_id='s', turn_id='t1')
            return [e['planner'] for e in response.tool_trace
                    if e.get('phase') == 'plan' and e.get('planner')]

    def test_planner_sources_are_the_declared_five(self):
        traces = self._run(DDITool(lambda meds: [WARFARIN_WARNING]),
                           ['memory_read', 'ddi_check', 'respond'])
        sources = {t['source'] for t in traces}
        self.assertTrue(sources, 'trace 里没有任何 planner 归因')
        self.assertTrue(sources <= set(PLANNER_SOURCES),
                        f'出现了未声明的归因类别：{sources - set(PLANNER_SOURCES)}')

    def test_code_forced_warning_write_is_labelled_system_forced(self):
        """代码为满足安全不变量自己构造并执行的动作，不得记成模型选择。"""
        traces = self._run(DDITool(lambda meds: [WARFARIN_WARNING]),
                           ['memory_read', 'ddi_check', 'respond'])
        sources = [t['source'] for t in traces]
        self.assertIn('system_forced', sources,
                      f'强制写入未单独标记；实际 planner sources = {sources}')

    def test_hydrated_arguments_are_marked_on_the_trace(self):
        """代码替模型补了决定性参数时，这条 trace 必须单独可见。"""
        # ddi_check without medications: the schema requires it, and the guard
        # owns the authoritative value, so it is hydrated rather than rejected.
        traces = self._run(DDITool(lambda meds: []), ['memory_read', 'ddi_check', 'respond'])
        hydrated = [t for t in traces if t.get('hydrated_arguments')]
        self.assertTrue(hydrated, f'参数代填未被标记；traces = '
                                  f'{[(t["source"], t.get("argument_corrections")) for t in traces]}')
        self.assertTrue(all(t['argument_corrections'] for t in hydrated),
                        'hydrated_arguments 为真但 argument_corrections 为空')


WARFARIN_WARNING = {
    'drug_a': '华法林', 'drug_b': '阿司匹林', 'effect': '出血风险增加',
    'source_text': '华法林与阿司匹林合用可增加出血风险，需监测凝血功能。',
    'source_url': 'label://warfarin-aspirin', 'confidence': 'high',
}


if __name__ == '__main__':
    unittest.main()
