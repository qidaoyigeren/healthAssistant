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


def _run(ddi, steps, medication='华法林'):
    """Drive one turn through the real loop with a scripted planner."""
    with _env() as store:
        store.apply_medication_change(action='add', name=medication, ingredients=[],
            session_id='s', turn_id='t0', source='test', dose='3mg')
        agent = MedicationCoordinatorAgent(
            store, ddi_tool=ddi, rag_tool=_rag(), max_cycles=6,
            llm_planner_enabled=True, proposal_provider=_planner_script(steps))
        with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'}):
            response = agent.handle(CareEvent('user_message', '看看我的用药风险'),
                                    session_id='s', turn_id='t1')
        return [e['planner'] for e in response.tool_trace
                if e.get('phase') == 'plan' and e.get('planner')]


class AttributionTest(unittest.TestCase):
    """代码替模型做的每个决定都必须与"模型选择"分开计数。"""

    def test_planner_sources_are_the_declared_five(self):
        traces = _run(DDITool(lambda meds: [WARFARIN_WARNING]),
                      ['memory_read', 'ddi_check', 'respond'])
        sources = {t['source'] for t in traces}
        self.assertTrue(sources, 'trace 里没有任何 planner 归因')
        self.assertTrue(sources <= set(PLANNER_SOURCES),
                        f'出现了未声明的归因类别：{sources - set(PLANNER_SOURCES)}')

    def test_code_forced_warning_write_is_labelled_system_forced(self):
        """代码为满足安全不变量自己构造并执行的动作，不得记成模型选择。"""
        traces = _run(DDITool(lambda meds: [WARFARIN_WARNING]),
                      ['memory_read', 'ddi_check', 'respond'])
        sources = [t['source'] for t in traces]
        self.assertIn('system_forced', sources,
                      f'强制写入未单独标记；实际 planner sources = {sources}')

    def test_hydrated_arguments_are_marked_on_the_trace(self):
        """代码替模型补了决定性参数时，这条 trace 必须单独可见。"""
        # ddi_check without medications: the schema requires it, and the guard
        # owns the authoritative value, so it is hydrated rather than rejected.
        traces = _run(DDITool(lambda meds: []), ['memory_read', 'ddi_check', 'respond'])
        hydrated = [t for t in traces if t.get('hydrated_arguments')]
        self.assertTrue(hydrated, f'参数代填未被标记；traces = '
                                  f'{[(t["source"], t.get("argument_corrections")) for t in traces]}')
        self.assertTrue(all(t['argument_corrections'] for t in hydrated),
                        'hydrated_arguments 为真但 argument_corrections 为空')


class CorrectionContextTest(unittest.TestCase):
    """纠错提示只给约束：一旦它携带代码算好的动作，模型照抄即通过，
    却会被计成独立自主规划。"""

    def _planner(self):
        from stage0.agent import HybridPlanner
        return HybridPlanner(
            tool_schemas={'rag_search': {'type': 'object',
                                         'properties': {'query': {'type': 'string'}},
                                         'required': ['query']}},
            proposal_provider=lambda payload: {'decision': 'respond'})

    def test_correction_task_never_leaks_the_code_computed_next_action(self):
        from stage0.agent import AgentState
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对相互作用', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': 'x', 'entities': ['氨氯地平'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': []}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                     'description': 'd', 'claim_id': 'claim:a'}]
        scripted = inv.next_action()   # the code-computed action, with its fixed wording
        self.assertIsNotNone(scripted, '前置条件不成立：脚本动作没有被计算出来')
        planner = self._planner()
        state = AgentState(session_id='s', turn_id='t',
                           event=CareEvent('user_message', 'x'), investigation=inv)
        planner.last_rejection = {
            'proposal': {'decision': 'tool', 'tool': 'rag_search',
                         'arguments': {'query': '模型自己写的查询'}},
            'errors': [{'code': 'invalid_gap_link', 'category': 'safety', 'message': 'gap 无效'}],
            'reason': 'safety_rejection'}
        correction = planner.correction_for(state)
        self.assertIsNotNone(correction)
        self.assertNotIn('next_expected_action_hint', correction)
        blob = json.dumps(correction, ensure_ascii=False)
        self.assertNotIn(scripted.arguments['query'], blob,
                         '纠错上下文把代码算好的搜索词交给了模型')

    def test_correction_task_still_carries_actionable_constraints(self):
        """去掉泄露不等于去掉反馈：约束必须还在，否则模型只能瞎猜。"""
        from stage0.agent import AgentState
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对相互作用', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': 'x', 'entities': ['氨氯地平'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': []}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                     'description': 'd', 'claim_id': 'claim:a'}]
        planner = self._planner()
        state = AgentState(session_id='s', turn_id='t',
                           event=CareEvent('user_message', 'x'), investigation=inv)
        planner.last_rejection = {'proposal': {'decision': 'tool', 'tool': 'rag_search',
                                               'arguments': {'query': 'q'}},
                                  'errors': [{'code': 'invalid_gap_link', 'category': 'safety',
                                              'message': 'gap 无效'}],
                                  'reason': 'safety_rejection'}
        correction = planner.correction_for(state)
        self.assertIn('claim:a', correction['open_gap_ids'])
        self.assertFalse(correction['termination_ready'])
        self.assertIn('rag_search', correction['allowed_tools_now'])
        self.assertTrue(correction['instruction'])

    def test_post_correction_acceptance_is_labelled(self):
        """被拒后修正通过的提案，必须与"一次就对的独立规划"分开计数。"""
        traces = _run(DDITool(lambda meds: []), ['memory_read', 'respond', 'ddi_check'])
        sources = [t['source'] for t in traces]
        self.assertIn('llm_post_correction', sources, f'sources={sources}')


WARFARIN_WARNING = {
    'drug_a': '华法林', 'drug_b': '阿司匹林', 'effect': '出血风险增加',
    'source_text': '华法林与阿司匹林合用可增加出血风险，需监测凝血功能。',
    'source_url': 'label://warfarin-aspirin', 'confidence': 'high',
}


if __name__ == '__main__':
    unittest.main()
