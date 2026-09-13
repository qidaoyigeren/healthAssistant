"""A4 invariants: the multi-agent review is default-OFF, trigger-gated,
read-only, scope-bounded, divergence-preserving and honestly labelled."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.agent_evals.run_eval import DATA
from stage0.harness.multi_agent import (multi_review_enabled, run_multi_agent_review,
                                        trigger_condition, _validate_refs)
from stage0.investigation import InvestigationState
from stage0.memory import MemoryStore

TASKS = json.loads(DATA.read_text(encoding='utf-8'))
SUPPORTED_CHUNK = {'chunk_id': 'label-sup', 'drug_name': '合成药甲', 'section': '药物相互作用',
                   'text': '合成药甲与合成药乙合用增加出血风险。', 'source_url': 'https://synthetic.invalid/sup',
                   'corpus_version': 'synthetic-v1'}
CONTRA_CHUNK = {'chunk_id': 'label-con', 'drug_name': '合成药甲', 'section': '药物相互作用',
                'text': '合成药甲与合成药乙未发现相互作用。', 'source_url': 'https://synthetic.invalid/con',
                'corpus_version': 'synthetic-v1'}


class FixedRAG:
    def __init__(self, chunks):
        self.chunks = chunks

    def __call__(self, query, **kwargs):
        return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1', 'results': self.chunks}


def seed(store):
    for item in TASKS[0]['initial_state']['medications']:
        store.apply_medication_change(action='add', name=item['name'], ingredients=[], session_id='s',
            turn_id=item['name'], source='synthetic', dose=item['dose'], occurred_at=item['date'])


class MultiAgentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-a4-')
        self.store = MemoryStore(Path(self.temp.name) / 'memory.db', llm_enabled=False)
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def conflicting_inv(self):
        """A finished investigation holding support AND opposition."""
        from stage0.agent import CareEvent
        agent = MedicationCoordinatorAgent(self.store, ddi_tool=DDITool(lambda meds: []),
                                           rag_tool=FixedRAG([SUPPORTED_CHUNK, CONTRA_CHUNK]))
        with patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            result = agent.handle(CareEvent('user_message', '请核查当前用药证据'), session_id='s')
        return agent, result.answer_bundle['investigation']

    def test_default_on_can_be_disabled_and_no_trigger_keeps_single_agent_path(self):
        # Default ON (user-directed, 2026-09-09); an explicit env value turns
        # it off, and a missing explainable trigger keeps the single-agent
        # path regardless.
        self.assertTrue(multi_review_enabled())
        with patch.dict('os.environ', {'AGENT_MULTI_REVIEW_ENABLED': '0'}):
            self.assertFalse(multi_review_enabled())
        agent, inv = self.conflicting_inv()
        with patch.dict('os.environ', {'AGENT_MULTI_REVIEW_ENABLED': '0'}):
            self.assertIsNone(run_multi_agent_review(inv, evidence_store=agent.evidence_store))
        self.assertIsNone(trigger_condition({'claims': [{'a': 1}], 'gaps': []}))

    def test_enabled_conflict_trigger_runs_two_labelled_roles(self):
        agent, inv = self.conflicting_inv()
        with patch.dict('os.environ', {'AGENT_MULTI_REVIEW_ENABLED': '1'}):
            review = run_multi_agent_review(inv, evidence_store=agent.evidence_store)
        self.assertIsNotNone(review)
        self.assertEqual('open_evidence_conflict', review['trigger'])
        kinds = {w['researcher']['worker_kind'] for w in review['workers'] if w.get('researcher')}
        kinds |= {w['checker']['worker_kind'] for w in review['workers'] if w.get('checker')}
        self.assertEqual({'evidence_researcher', 'evidence_checker'}, kinds)
        for worker in review['workers']:
            if worker.get('checker'):
                self.assertIn('deterministic', worker['checker']['model'])
        # Divergences are recorded, not voted away.
        self.assertTrue(any(d['kind'] == 'support_and_opposition_coexist' for d in review['divergences']),
                        review['divergences'])
        # Exact accounting, not "at least one call": this default path runs the
        # deterministic workers, so the counts are fixed — 2 checker verdicts
        # over 4 cycles and NO model call at all (the sibling test pins the
        # exact ``model_calls`` the same way for the model path).
        self.assertEqual(2, review['usage']['calls'])
        self.assertEqual(4, review['usage']['cycles'])
        self.assertNotIn('model_calls', review['usage'])
        self.assertFalse(review['usage']['usage_unknown'])

    def test_worker_cannot_expand_parent_scope(self):
        self.assertEqual(['ev-ok'], _validate_refs({'ev-ok': True}, ['ev-ok', 'ev-forged']))
        self.assertEqual([], _validate_refs({}, ['anything']))

    def test_deadline_or_cap_reports_not_dispatched(self):
        agent, inv = self.conflicting_inv()
        with patch.dict('os.environ', {'AGENT_MULTI_REVIEW_ENABLED': '1'}):
            review = run_multi_agent_review(inv, evidence_store=agent.evidence_store, deadline_seconds=0.0)
        self.assertIsNotNone(review)
        self.assertTrue(all(w['status'] == 'not_dispatched' for w in review['workers']))

    def test_model_workers_choose_read_and_stop_with_shared_budget(self):
        from stage0.harness.model_review import run_model_review
        from stage0.agent import AgentState, CareEvent
        from stage0.harness.runtime import RunContext
        from stage0.turn_budget import budget_scope
        agent, inv = self.conflicting_inv()
        state = AgentState('s', 'worker-test', CareEvent('user_message', '核查证据'),
                           ctx=RunContext('worker-test', 'worker-test'))
        self.store.workflow_run_start(run_id='worker-test', graph_version='test')
        def decide(payload):
            if not payload['observations']:
                return {'decision': 'tool', 'tool': 'read_evidence',
                        'arguments': {'evidence_id': payload['evidence_refs'][0], 'limit': 2000}}
            return {'decision': 'respond', 'verdict': 'insufficient',
                    'evidence_refs': [payload['evidence_refs'][0]]}
        with budget_scope(self.store, 'worker-test'):
            result = run_model_review(inv, agent=agent, state=state, proposal_provider=decide)
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(4, result['usage']['model_calls'])
        self.assertEqual(2, result['usage']['calls'])
        self.assertEqual('scripted', result['workers'][0]['checker']['execution_mode'])
        run = self.store.workflow_run_get('worker-test')
        self.assertEqual(4, run['budget']['calls_attempted'])

    def test_model_worker_cannot_write_or_claim_unread_evidence(self):
        from stage0.harness.model_review import run_model_review
        from stage0.agent import AgentState, CareEvent
        from stage0.harness.runtime import RunContext
        from stage0.turn_budget import budget_scope
        agent, inv = self.conflicting_inv()
        self.store.workflow_run_start(run_id='worker-denied', graph_version='test')
        state = AgentState('s', 'worker-denied', CareEvent('user_message', '核查证据'),
                           ctx=RunContext('worker-denied', 'worker-denied'))
        before = self.store.current_medications()
        def forbidden(payload):
            if payload['role'] == 'evidence_researcher':
                return {'decision': 'tool', 'tool': 'memory_write', 'arguments': {}}
            return {'decision': 'respond', 'verdict': 'supported', 'evidence_refs': ['forged']}
        with budget_scope(self.store, 'worker-denied'):
            result = run_model_review(inv, agent=agent, state=state, proposal_provider=forbidden)
        self.assertEqual('incomplete', result['status'])
        self.assertEqual(0, result['usage']['calls'])
        self.assertEqual(before, self.store.current_medications())


if __name__ == '__main__':
    unittest.main()
