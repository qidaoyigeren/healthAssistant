"""Closeout regressions over real SQLite, receipts and the task worker."""
import unittest
from unittest.mock import patch

from stage0 import test_agent_open_tasks as fixtures
from stage0.test_agent_open_tasks import make_agent
from stage0.care_tasks import CareTasks, ProductError
from stage0.product import ProductStore
from stage0.server import OutboxWorker


class CloseoutTests(fixtures.OpenTaskTests):
    # Inherit the original restart, cancellation and continuation contract tests.
    def test_changed_goal_cannot_replay_create_key(self):
        tasks = self.tasks()
        tasks.create('create', 'evidence_review', goal='核查当前用药证据')
        with self.assertRaises(ProductError):
            tasks.create('create', 'evidence_review', goal='核查另外一份材料')

    def test_changed_supplement_cannot_replay_input_key(self):
        tasks = self.tasks()
        task = tasks.create('create', 'evidence_review', goal='核查当前用药证据')
        tasks.record_input(task['id'], 'input', 1, semantic={'renal_function': '正常'})
        with self.assertRaises(ProductError):
            tasks.record_input(task['id'], 'input', 1, semantic={'renal_function': '异常'})

    def test_invalid_later_input_rolls_back_all_effects(self):
        tasks = self.tasks()
        task = tasks.create('create', 'evidence_review', goal='核查当前用药证据')
        before = self.store.current_medications()
        with self.assertRaises(ProductError):
            tasks.record_input(task['id'], 'input', 1,
                medications=[{'name': '回滚合成药', 'dose': '2mg'}], semantic={'not_allowed': 3})
        self.assertEqual(before, self.store.current_medications())

    def test_review_io_never_holds_transaction(self):
        tasks = self.tasks()
        agent = make_agent(self.store)
        # Assert at the runner boundary, before any planner or tool I/O.
        original_run = agent.run_open_review
        def run(*args, **kwargs):
            self.assertFalse(self.store.connection.in_transaction, 'review inside write transaction')
            return original_run(*args, **kwargs)
        agent.run_open_review = run
        tasks._agent = agent
        task = tasks.create('create', 'evidence_review', goal='核查当前用药证据')
        tasks.resume(task['id'], 'resume', 1)

    def test_queued_review_survives_service_restart(self):
        tasks = self.tasks()
        task = tasks.create('create', 'evidence_review', goal='核查当前用药证据')
        accepted = tasks.resume(task['id'], 'resume', 1, enqueue=True)
        self.assertEqual('running', accepted['status'])
        self.store.close()
        from stage0.memory import MemoryStore
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        from stage0.graph_runner import LegacyAgentRunner
        OutboxWorker(self.store, runner_factory=lambda: LegacyAgentRunner(make_agent(self.store))).drain_once()
        actual = ProductStore(self.store).get(task['id'])
        self.assertEqual('waiting_input', actual['status'])
        self.assertEqual(1, actual['budget']['spent'])

    def test_task_budget_is_reserved_before_dispatch(self):
        tasks = self.tasks()
        task = tasks.create('create', 'evidence_review', goal='核查当前用药证据')
        with tasks.p.transaction():
            task['resource_budget']['call_limit'] = 0
            tasks.p.save('care_task', task)
        actual = tasks.resume(task['id'], 'resume', 1)
        self.assertEqual('failed', actual['status'])
        self.assertFalse(actual['resource_budget']['child_run_ids'])

    def test_planner_breaker_continues_verified_deterministic_work(self):
        from stage0.agent import CareEvent, MedicationCoordinatorAgent, DDITool
        from stage0.test_agent_open_tasks import FixedRAG, TASKS
        attempts = []
        def early_response(payload):
            attempts.append(1)
            if not any(o['tool'] == 'memory_write' and o['ok'] for o in payload['observations']):
                return {'decision': 'tool', 'tool': 'memory_write', 'arguments': {'operation': 'consolidate_event'}}
            return {'decision': 'respond'}
        with patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            agent = MedicationCoordinatorAgent(self.store, ddi_tool=DDITool(lambda _: []),
                rag_tool=FixedRAG(TASKS[0]['materials']), llm_planner_enabled=True,
                proposal_provider=early_response)
            response = agent.handle(CareEvent('user_message', '请核查当前用药证据'), session_id='test')
        self.assertEqual(3, len(attempts))
        self.assertEqual('checks_completed', response.answer_bundle['investigation']['termination_reason'])
        self.assertEqual('degraded', response.answer_bundle['execution_status'])

    def test_provider_fallback_is_degraded_even_when_task_completes(self):
        from stage0.agent import CareEvent, MedicationCoordinatorAgent, DDITool
        from stage0.test_agent_open_tasks import FixedRAG, TASKS
        def unavailable(payload):
            raise RuntimeError('synthetic provider 429 rate_limit')
        with patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            agent = MedicationCoordinatorAgent(self.store, ddi_tool=DDITool(lambda _: []),
                rag_tool=FixedRAG(TASKS[0]['materials']), llm_planner_enabled=True,
                proposal_provider=unavailable)
            response = agent.handle(CareEvent('user_message', '请核查当前用药证据'), session_id='test')
        bundle = response.answer_bundle
        self.assertEqual('completed', bundle['goal_status'])
        self.assertEqual('degraded', bundle['execution_status'])
        self.assertIn('provider_error', bundle['coverage']['planner_fallback_reasons'])
        self.assertTrue(bundle['coverage']['degraded_reason'].startswith('planner_fallback:'))

    def test_recorded_number_does_not_establish_clinical_applicability(self):
        from stage0.agent import CareEvent, MedicationCoordinatorAgent, DDITool
        from stage0.memory import SemanticFact
        self.store.write_semantic_fact(SemanticFact('renal_function', 'renal_status', {'value': 55, 'unit': 'ml/min'}), source='synthetic')
        agent = MedicationCoordinatorAgent(self.store, ddi_tool=DDITool(lambda _: []),
            rag_tool=fixtures.FixedRAG([fixtures.CHUNKS[-1]]))
        with patch.dict('os.environ', {'AGENT_INVESTIGATION_ENABLED': '1'}):
            result = agent.handle(CareEvent('user_message', '核查当前用药证据'), session_id='test')
        self.assertFalse(any(c['status'] == 'supported' for c in result.answer_bundle['investigation']['claims']))

    def test_open_task_review_uses_real_review_queue_and_requests_input(self):
        from stage0.agent import MedicationCoordinatorAgent, DDITool
        from stage0.test_multi_agent_review import SUPPORTED_CHUNK, CONTRA_CHUNK
        tasks = self.tasks()
        tasks._agent = MedicationCoordinatorAgent(self.store, ddi_tool=DDITool(lambda _: []),
            rag_tool=fixtures.FixedRAG([SUPPORTED_CHUNK, CONTRA_CHUNK]))
        with patch.dict('os.environ', {'STAGE0_REVIEW_ENABLED': '1'}):
            task = tasks.create('review-create', 'evidence_review', goal='核查当前用药证据')
            task = tasks.resume(task['id'], 'review-start', task['revision'])
        self.assertEqual('waiting_review', task['status'])
        case = self.store.review_case(task['review_refs'][0])
        claimed = self.store.claim_review_case(case['id'], expected_revision=case['revision'], assignee='synthetic-reviewer')
        self.store.record_review_decision(case_id=case['id'], expected_revision=claimed['revision'],
            action='request_more_info', payload={}, idempotency_key='review-input', actor_id='synthetic-reviewer')
        from stage0.graph_runner import LegacyAgentRunner
        worker = OutboxWorker(self.store, runner_factory=lambda: LegacyAgentRunner(tasks.agent()))
        result = worker.drain_resume_tasks()
        self.assertEqual('resumed', result[0]['status'], result)
        self.assertEqual('waiting_input', tasks.p.get(task['id'])['status'])
        self.assertFalse(worker.drain_resume_tasks())

    def test_phase_reservation_prevents_spending_delivery_tokens(self):
        from stage0.turn_budget import budget_scope, BudgetExceeded
        self.store.workflow_run_start(run_id='reserved', graph_version='test',
            budget={'token_budget': 100, 'accounting_version': 2})
        invoked = []
        with budget_scope(self.store, 'reserved') as budget:
            budget.data['wrap_up_tokens_reserved'] = 20
            with self.assertRaises(BudgetExceeded):
                budget.call('planner', lambda *args: invoked.append(True), {'input': 'x' * 200})
        self.assertFalse(invoked)
        self.assertEqual(0, self.store.workflow_run_get('reserved')['budget']['calls_attempted'])


if __name__ == '__main__':
    unittest.main()
