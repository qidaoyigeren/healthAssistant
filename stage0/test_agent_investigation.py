"""A0/A1 invariants over isolated synthetic stores and real graph/API paths."""
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stage0.agent import AgentState, CareEvent, DDITool, MedicationCoordinatorAgent, PlannerPolicyGuard, ToolAction
from stage0.agent_evals.run_eval import DATA, run_task, score
from stage0.evidence_quality import assess_claim
from stage0.graph_runner import LangGraphAgentRunner, build_workflow_state
from stage0.harness.runtime import RunContext
from stage0.investigation import InvestigationState
from stage0.memory import MemoryStore, SemanticFact

TASKS = json.loads(DATA.read_text(encoding='utf-8'))


class FixedRAG:
    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else TASKS[0]['materials']

    def __call__(self, query, **kwargs):
        return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1', 'results': self.chunks}


def make_agent(store, **kwargs):
    return MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda meds: []), rag_tool=FixedRAG(), **kwargs)


def seed(store):
    for item in TASKS[0]['initial_state']['medications']:
        store.apply_medication_change(action='add', name=item['name'], ingredients=[], session_id='synthetic',
            turn_id=item['name'], source='synthetic-fixture', dose=item['dose'], occurred_at=item['date'])


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-a1-')
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'})
        self.env.start()
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def test_same_dev_outcomes_include_wait_conflict_repeat_and_failure(self):
        for task in TASKS:
            with self.subTest(family=task['family_id']):
                result = run_task(task, 'gap', 'replay')
                self.assertTrue(result['score']['passed'], result)

    def test_negative_scoring_and_no_missing_data_pass(self):
        self.assertFalse(score(TASKS[0], {})['passed'])
        task = dict(TASKS[0], expected_gaps=['absent_fact'])
        self.assertFalse(score(task, {'investigation': {'termination_reason': 'checks_completed'}})['passed'])

    def test_off_flag_keeps_legacy_and_old_checkpoint_does_not_opt_in(self):
        with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '0'}):
            result = make_agent(self.store).handle(CareEvent('user_message', '请核查用药证据'), session_id='s')
        self.assertIsNone(result.answer_bundle['investigation'])
        wf = build_workflow_state(event=CareEvent('user_message', '核查证据'), session_id='s', turn_id='t',
            client_event_id=None, event_id=None, run_id='t', budget=None)
        wf.pop('investigation_policy')
        runner = LangGraphAgentRunner(make_agent(self.store))
        state = runner._agent_state(wf)
        runner.agent._prepare_investigation(state)
        self.assertEqual('legacy', state.investigation_policy)
        self.assertIsNone(state.investigation)

    def test_proposal_cannot_skip_checks_ask_known_fact_or_read_forged_ref(self):
        inv = InvestigationState('核查', 'local-demo')
        inv.gap('authority', 'patient_fact_missing', '读取')
        state = AgentState('s', 't', CareEvent('user_message', '核查'), ctx=RunContext('t', 't'), investigation=inv)
        guard = PlannerPolicyGuard()
        for proposal in [
            {'decision': 'respond'},
            {'decision': 'tool', 'tool': 'rag_search', 'arguments': {'query': 'x'}, 'gap_id': 'forged', 'expected_observation': 'x'},
            {'decision': 'tool', 'tool': 'ask_clarification', 'arguments': {'question': '你吃什么药'}, 'gap_id': 'authority', 'expected_observation': 'x'},
        ]:
            self.assertFalse(guard.validate(state, proposal).valid)
        inv.version = 'future'
        with self.assertRaisesRegex(ValueError, 'migration'):
            InvestigationState.restore(inv.to_dict(), 'local-demo')
        inv.version = 'investigation@1'
        with self.assertRaisesRegex(ValueError, 'scope'):
            InvestigationState.restore(inv.to_dict(), 'other')
        raw = inv.to_dict(); raw['checks'].pop('applicability')
        with self.assertRaisesRegex(ValueError, 'coverage'):
            InvestigationState.restore(raw, 'local-demo')

    def test_hard_source_date_entity_condition_and_negation_rules(self):
        text = '合成药甲与合成药乙合用增加出血风险。'
        def check(**kw):
            return assess_claim(quote=text, text=text, entities=['合成药甲', '合成药乙'], evidence_id='e', **kw)
        self.assertEqual('insufficient', check(source_status='invalid')['status'])
        self.assertEqual('insufficient', check(required_date='2026-09-01', evidence_date='2026-08-01')['status'])
        self.assertEqual('insufficient', check(conditions_known=False)['status'])
        self.assertEqual('insufficient', check(content_complete=False)['status'])

    def test_tamper_after_check_removes_support_but_preserves_historic_refs(self):
        seed(self.store)
        agent = make_agent(self.store)
        result = agent.handle(CareEvent('user_message', '请核查当前用药证据'), session_id='s', turn_id='t')
        inv = InvestigationState.restore(result.answer_bundle['investigation'], 'local-demo')
        ref = inv.read_refs[0]
        with self.store.connection:
            self.store.connection.execute('UPDATE evidence_records SET content=content||? WHERE evidence_id=?', ('篡改', ref))
        inv.validate_sources(agent.evidence_store)
        self.assertEqual('insufficient', inv.claims[0]['status'])
        self.assertEqual('unrecoverable_failure', inv.termination_reason)
        self.assertIn(ref, inv.evidence_refs)
        self.assertTrue(any(g['kind'] == 'source_invalid' for g in inv.gaps))

    def test_model_script_uses_gap_links_but_cannot_fabricate_completion(self):
        seed(self.store)
        seen = []
        def provider(payload):
            inv = payload['investigation']
            seen.append(inv)
            if not any(o['tool'] == 'memory_write' and o['ok'] for o in payload['observations']):
                return {'decision': 'tool', 'tool': 'memory_write', 'arguments': {'operation': 'consolidate_event'}}
            return dict(inv['candidates'][0], decision='tool')
        result = make_agent(self.store, llm_planner_enabled=True, proposal_provider=provider,
                            response_provider=lambda _: 'must not author investigation state').handle(
            CareEvent('user_message', '请核查当前用药证据'), session_id='s', turn_id='script')
        self.assertEqual('completed', result.answer_bundle['goal_status'])
        self.assertEqual('scripted', result.answer_bundle['investigation']['mode'])
        self.assertEqual('bounded_report', result.answer_bundle['answer_status'])
        self.assertTrue(any(v['candidates'] for v in seen))

    def test_crash_after_commit_resume_graph_with_flag_off_keeps_budget_and_effects(self):
        seed(self.store)
        checkpoint = self.root / 'checkpoints.db'
        runner = LangGraphAgentRunner(make_agent(self.store), checkpoint_path=checkpoint)
        original = runner._node_execute
        def crash(wf):
            tool = wf['pending_action']['tool']
            value = original(wf)
            if tool == 'memory_write':
                raise RuntimeError('synthetic crash after domain commit before checkpoint')
            return value
        runner._node_execute = crash
        kwargs = dict(event=CareEvent('user_message', '请核查当前用药证据'), session_id='s', turn_id='recover', run_id='recover')
        with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            runner.run(**kwargs)
        before = self.store.workflow_run_get('recover')['budget']['cycles_consumed']
        receipts = self.store.connection.execute('SELECT COUNT(*) FROM operation_receipts').fetchone()[0]
        runner.close()
        self.store.close()
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '0'}):
            runner = LangGraphAgentRunner(make_agent(self.store), checkpoint_path=checkpoint)
            try:
                response = runner.run(**kwargs)
                checkpoint_state = runner._ensure_graph().get_state({'configurable': {'thread_id': 'recover'}}).values
                self.assertEqual('investigation@1', checkpoint_state['investigation']['version'])
                self.assertGreaterEqual(self.store.workflow_run_get('recover')['budget']['cycles_consumed'], before)
                self.assertEqual(receipts, self.store.connection.execute('SELECT COUNT(*) FROM operation_receipts').fetchone()[0])
                self.assertEqual('completed', response.answer_bundle['goal_status'])
            finally:
                runner.close()

    def test_fact_version_change_invalidates_assessments_and_requires_full_read(self):
        seed(self.store)
        agent = make_agent(self.store)
        response = agent.handle(CareEvent('user_message', '请核查用药证据'), session_id='s', turn_id='t')
        inv = InvestigationState.restore(response.answer_bundle['investigation'], 'local-demo')
        self.store.apply_medication_change(action='add', name='合成药丙', ingredients=[], session_id='s',
            turn_id='change', source='synthetic-fixture', occurred_at='2026-09-02')
        inv.sync_authority(self.store)
        self.assertFalse(inv.authority_read)
        self.assertFalse(inv.assessments)
        self.assertTrue(inv.invalidations)
        self.assertEqual('memory_read', inv.next_action().tool)


if __name__ == '__main__':
    unittest.main()
