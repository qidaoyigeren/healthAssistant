"""A2 invariants: the evidence_review open contract over persisted care
tasks — cross-restart continuation, selective invalidation, budget
accumulation, duplicate/submit crash safety and cancellation."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.agent_evals.run_eval import DATA
from stage0.care_tasks import CareTasks, ProductError
from stage0.memory import MemoryStore, SemanticFact
from stage0.product import ProductStore
from stage0.server import OutboxWorker

TASKS = json.loads(DATA.read_text(encoding='utf-8'))
CHUNKS = TASKS[0]['materials'] + [{
    'chunk_id': 'synthetic-label-renal', 'drug_name': '合成药甲', 'section': '药物相互作用',
    'text': '合成药甲与合成药乙合用可能增加出血风险；肾功能不全者需调整剂量。',
    'source_url': 'https://synthetic.invalid/label/renal', 'corpus_version': 'synthetic-v1'}]


class FixedRAG:
    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else CHUNKS

    def __call__(self, query, **kwargs):
        return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1', 'results': self.chunks}


def make_agent(store):
    return MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda meds: []), rag_tool=FixedRAG())


def seed(store):
    for item in TASKS[0]['initial_state']['medications']:
        store.apply_medication_change(action='add', name=item['name'], ingredients=[], session_id='synthetic',
            turn_id=item['name'], source='synthetic-fixture', dose=item['dose'], occurred_at=item['date'])


class OpenTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-a2-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def tasks(self):
        """A fresh service instance over the same persisted database — the
        restart path must keep working without in-memory state."""
        product = ProductStore(self.store)
        return CareTasks(product, agent_factory=lambda: make_agent(self.store))

    def test_wait_persists_restart_supplement_then_completes(self):
        tasks = self.tasks()
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件')
        # First run: the renal chunk states a population condition with no
        # recorded renal fact → the task must WAIT with concrete questions.
        task = tasks.resume(task['id'], 't-resume-1', task['revision'])
        self.assertEqual('waiting_input', task['status'], task)
        fields = {q['field'] for q in task['missing_inputs']}
        self.assertIn('renal_function', fields)
        self.assertTrue(task['partial_report_refs'])
        # "Restart": everything below uses a brand-new instance over SQLite.
        tasks = self.tasks()
        record = tasks.record_input(task['id'], 't-input-1', task['revision'],
                                    semantic={'renal_function': {'value': 55, 'unit': 'ml/min'}},
                                    additional_questions=['合成药甲晚上服用是否需要调整？'])
        self.assertEqual(0, record['applied_medications'])
        self.assertEqual(1, record['applied_semantic_facts'])
        task = tasks.resume(task['id'], 't-resume-2', record_revision := task['revision'] + 1)
        self.assertEqual('completed', task['status'], task)
        self.assertEqual(1, len(task['result_refs']))
        artifact = ProductStore(self.store).get(task['result_refs'][0], 'investigation_report')
        self.assertFalse(artifact['partial'])
        self.assertIn('待确认问题', artifact['markdown'])
        self.assertIn('合成药甲晚上服用', artifact['markdown'])
        inv = artifact['investigation']
        self.assertEqual('checks_completed', inv['termination_reason'])
        # The model/user-proposed question is recorded but never auto-completed.
        self.assertTrue(any(s['kind'] == 'user_question' and s['status'] == 'recorded' for s in task['subgoals']))
        # Domain effects are retained and the receipt replays identically.
        again = tasks.resume(task['id'], 't-resume-2', record_revision)
        self.assertEqual(again['receipt_id'], task['receipt_id'])

    def tasks_get(self, tasks):
        return ProductStore(self.store).objects('care_task')[0]

    def test_semantic_correction_reuses_evidence_medication_change_rechecks(self):
        tasks = self.tasks()
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件')
        task = tasks.resume(task['id'], 't-resume-1', task['revision'])
        self.assertEqual('waiting_input', task['status'])
        before = task['investigation']
        queries_before = len(before['queries'])
        refs_before = len(before['evidence_refs'])
        # A semantic-only correction: applicability re-verified, evidence and
        # searches reused (selective invalidation).
        self.store.write_semantic_fact(
            SemanticFact(namespace='renal_function', key='egfr', value={'value': 55, 'unit': 'ml/min'}),
            source='caregiver-input')
        task = tasks.resume(task['id'], 't-resume-2', task['revision'])
        self.assertEqual('completed', task['status'], task)
        inv = task['investigation']
        self.assertTrue(any(i['reason'] == 'semantic_changed_applicability_recheck' for i in inv['invalidations']))
        self.assertGreaterEqual(len(inv['evidence_refs']), refs_before)
        self.assertEqual(queries_before, len(inv['queries']))  # no re-search
        # A medication change afterwards would be a NEW goal — completed tasks
        # are closed; the version check that would invalidate a live run is
        # covered by the investigation unit tests.
        with self.assertRaisesRegex(ProductError, '已结束'):
            tasks.resume(task['id'], 't-resume-3', task['revision'])

    def test_duplicate_submit_replays_receipt_and_budget_accumulates(self):
        tasks = self.tasks()
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件', budget=3)
        accepted_revision = task['revision']
        task = tasks.resume(task['id'], 't-resume-1', task['revision'])
        self.assertEqual('waiting_input', task['status'])
        self.assertEqual(1, task['budget']['spent'])
        replay = tasks.resume(task['id'], 't-resume-1', accepted_revision)
        self.assertEqual(replay['receipt_id'], task['receipt_id'])
        self.assertEqual(1, replay['budget']['spent'])  # no double spend
        runs_before = len(replay['runs'])
        self.assertEqual(runs_before, len(ProductStore(self.store).get(task['id'], 'care_task')['runs']))
        # Continue after restart: budget and child-run accounting accumulate
        # across runs instead of resetting.
        tasks = self.tasks()
        tasks.record_input(task['id'], 't-input-2', task['revision'],
                           semantic={'renal_function': {'value': 55, 'unit': 'ml/min'}})
        task = tasks.resume(task['id'], 't-resume-2', task['revision'] + 1)
        self.assertEqual('completed', task['status'], task)
        self.assertEqual(2, task['budget']['spent'])
        self.assertEqual(2, len(task['runs']))
        resources = task['resource_budget']
        self.assertEqual(2, len(resources['child_run_ids']))
        self.assertIn('usage_unknown', resources)
        self.assertFalse(resources.get('usage_unknown'))

    def test_budget_exhaustion_fails_honestly_keeping_partial_report(self):
        tasks = self.tasks()
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件', budget=1)
        task = tasks.resume(task['id'], 't-resume-1', task['revision'])
        self.assertEqual('waiting_input', task['status'])
        final = tasks.resume(task['id'], 't-resume-2', task['revision'])
        self.assertEqual('failed', final['status'])
        self.assertIn('上限', final['waiting_reason'])
        self.assertTrue(final['partial_report_refs'])
        artifact = ProductStore(self.store).get(final['partial_report_refs'][0], 'investigation_report')
        self.assertTrue(artifact['partial'])
        self.assertNotEqual('checks_completed', artifact['investigation']['termination_reason'])

    def test_cancel_while_waiting_is_terminal_and_effects_retained(self):
        tasks = self.tasks()
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件')
        task = tasks.resume(task['id'], 't-resume-1', task['revision'])
        self.assertEqual('waiting_input', task['status'])
        task = tasks.resume(task['id'], 't-cancel', task['revision'], action='cancel')
        self.assertEqual('cancelled', task['status'])
        self.assertIn('保留', task['waiting_reason'])
        with self.assertRaisesRegex(ProductError, '已结束'):
            tasks.resume(task['id'], 't-resume-2', task['revision'])
        with self.assertRaisesRegex(ProductError, '不需要补充输入'):
            tasks.record_input(task['id'], 't-input', task['revision'], medications=[{'name': '合成药丙'}])

    def test_worker_auto_resume_continues_named_task_only(self):
        tasks = self.tasks()
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件')
        task = tasks.resume(task['id'], 't-resume-1', task['revision'])
        worker = OutboxWorker(self.store, runner_factory=lambda: NS(agent=make_agent(self.store)))
        # A stale expected revision must not be honoured (no guessing).
        worker._auto_resume_care_task({'task_id': task['id'], 'expected_revision': task['revision'] + 5}, 'k1')
        current = ProductStore(self.store).get(task['id'], 'care_task')
        self.assertEqual('waiting_input', current['status'])
        self.assertEqual(1, current['budget']['spent'])
        # The turn's facts are supplemented through the controlled input path;
        # the worker then continues exactly the named task at that revision.
        tasks.record_input(task['id'], 't-input-1', current['revision'],
                           semantic={'renal_function': {'value': 55, 'unit': 'ml/min'}})
        worker._auto_resume_care_task({'task_id': task['id'], 'expected_revision': current['revision'] + 1}, 'k2')
        self.assertEqual('completed', ProductStore(self.store).get(task['id'], 'care_task')['status'])


if __name__ == '__main__':
    unittest.main()
