"""A3 invariants: explicit routing with a recorded basis, wrap-up budget
reserve (a partition, never extra), result-fingerprint re-plan avoidance and
the failure taxonomy (429 vs timeout vs permanent vs effect_unknown)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stage0.agent import AgentState, CareEvent, DDITool, MedicationCoordinatorAgent, ToolAction
from stage0.agent_evals.run_eval import DATA
from stage0.harness.runtime import RunContext
from stage0.investigation import InvestigationState
from stage0.memory import MemoryStore
from stage0.router import route_request
from stage0.turn_budget import BudgetExceeded


class EffectUnknownError(RuntimeError):
    pass


TASKS = json.loads(DATA.read_text(encoding='utf-8'))
CHUNKS = TASKS[0]['materials']


class FixedRAG:
    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else CHUNKS

    def __call__(self, query, **kwargs):
        return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1', 'results': self.chunks}


def make_agent(store, **kwargs):
    return MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda meds: []), rag_tool=FixedRAG(), **kwargs)


class RouterTests(unittest.TestCase):
    def test_explicit_api_types_route_deterministically(self):
        self.assertEqual('exact_query', route_request(CareEvent('query_current_medications', 'x'), investigation_enabled=True)['route'])
        change = route_request(CareEvent('medication_change', 'x'), investigation_enabled=False)
        self.assertEqual('contract_flow', change['route'])
        self.assertIn('explicit_api_event_type', change['basis'])

    def test_compound_nl_request_is_not_dropped_by_keyword(self):
        text = '现在吃什么药？另外帮我核查一下相互作用的证据。'
        route = route_request(CareEvent('user_message', text), investigation_enabled=True)
        self.assertEqual('open_planning', route['route'], route)
        self.assertEqual('compound_request_keeps_all_goals', route['basis'])

    def test_simple_med_ask_exact_and_off_flag_legacy(self):
        simple = route_request(CareEvent('user_message', '现在吃什么药'), investigation_enabled=True)
        self.assertEqual('exact_query', simple['route'])
        self.assertEqual('legacy', route_request(CareEvent('user_message', '现在吃什么药'), investigation_enabled=False)['route'])
        refused = route_request(CareEvent('user_message', '请给我下诊断'), investigation_enabled=True)
        self.assertEqual('legacy', refused['route'])

    def test_route_recorded_in_answer_bundle(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                result = make_agent(store).handle(CareEvent('user_message', '请核查当前用药证据'), session_id='s')
                self.assertEqual('open_planning', result.answer_bundle['route'])
                self.assertTrue(result.answer_bundle['route_basis'])
            finally:
                store.close()


class WrapUpReserveTests(unittest.TestCase):
    def test_reserve_is_partition_not_increase(self):
        self.assertEqual(1, MedicationCoordinatorAgent.wrap_up_reserve(3))
        self.assertEqual(2, MedicationCoordinatorAgent.wrap_up_reserve(12))
        self.assertLessEqual(MedicationCoordinatorAgent.wrap_up_reserve(12), 12 * 0.2 + 1)

    def test_reserve_stops_new_tool_work_but_delivers_report(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                for item in TASKS[0]['initial_state']['medications']:
                    store.apply_medication_change(action='add', name=item['name'], ingredients=[], session_id='s',
                        turn_id=item['name'], source='synthetic', dose=item['dose'], occurred_at=item['date'])
                agent = make_agent(store, max_cycles=3)
                result = agent.handle(CareEvent('user_message', '请核查当前用药证据'), session_id='s')
                bundle = result.answer_bundle
                self.assertIsNotNone(bundle['investigation'])
                self.assertEqual(3, bundle['phase_budget']['limit_cycles'])
                self.assertEqual(1, bundle['phase_budget']['wrap_up_reserved'])
                # The delivered text is still a bounded report with the
                # disclaimer — not an empty answer (quality floor, not speed).
                self.assertIn('有界证据核查报告', result.text)
                self.assertIn('不', result.text)
                self.assertNotEqual('completed', bundle['goal_status'])
            finally:
                store.close()


class FailureTaxonomyTests(unittest.TestCase):
    def test_classify_and_detail(self):
        from stage0.server import classify_error, error_detail
        class RateLimitError(Exception):
            pass
        class APIStatusError(Exception):
            pass
        self.assertEqual('retryable', classify_error(RateLimitError('429 too many requests')))
        self.assertEqual('rate_limit', error_detail(RateLimitError('429'))['class'])
        self.assertEqual('retryable', classify_error(APIStatusError('code: 429')))
        self.assertEqual('retryable', classify_error(TimeoutError('x')))
        self.assertEqual('timeout', error_detail(TimeoutError('x'))['class'])
        self.assertEqual('effect_unknown', classify_error(EffectUnknownError('x')))
        self.assertEqual('permanent', classify_error(ValueError('bad param')))
        self.assertFalse(error_detail(ValueError('x'))['retryable'] == 'true')


class FingerprintGuardTests(unittest.TestCase):
    def test_repeated_identical_proposal_stops_bounded(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                for item in TASKS[0]['initial_state']['medications']:
                    store.apply_medication_change(action='add', name=item['name'], ingredients=[], session_id='s',
                        turn_id=item['name'], source='synthetic', dose=item['dose'], occurred_at=item['date'])
                agent = make_agent(store)
                fixed = ToolAction('rag_search', 'stuck', {'query': '同一查询', 'top_k': 5}, 'r', 'claim:x', 'e')
                def decide(state):
                    agent._prepare_investigation(state)
                    return fixed
                agent._decide = decide
                out = agent.run_open_review('请核查当前用药证据', run_id='fp-1', scope_id='local-demo', max_cycles=8)
                self.assertEqual('no_progress', out['investigation']['termination_reason'], out)
                self.assertIn('repeated_proposal', out['degraded_reason'])
                self.assertLess(out['cycles'], 8)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
