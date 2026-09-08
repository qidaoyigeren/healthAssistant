"""Harness P0: offline failure probes and resource/review recovery contracts."""
import os
import json
import time
from types import SimpleNamespace as NS
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent
from stage0.graph_runner import LangGraphAgentRunner, LegacyAgentRunner
from stage0.memory import MemoryStore
from stage0.test_stage8_agent import _repeat_read_provider, _composer_unavailable
from stage0.turn_budget import BudgetExceeded, budget_scope, initial_budget, provider_call, completion_call


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connect()

    def connect(self):
        from stage0.server import OutboxWorker
        self.store = MemoryStore(Path(self.temp.name) / 'test.db')
        self.agent = MedicationCoordinatorAgent(self.store, ddi_tool=DDITool(lambda meds: []))
        self.graph = LangGraphAgentRunner(self.agent, checkpoint_path=Path(self.temp.name) / 'cp.db', review_enabled=True)
        self.worker = OutboxWorker(self.store, lambda: self.graph)

    def restart(self):
        self.graph.close()
        self.store.close()
        self.connect()
        with self.store.connection:
            self.store.connection.execute("UPDATE resume_tasks SET lease_expires_at='2000-01-01',next_attempt_at=NULL WHERE status='pending'")

    def tearDown(self):
        self.graph.close()
        self.store.close()
        self.temp.cleanup()

    def parked(self, run_id='r'):
        from stage0.graph_runner import build_workflow_state
        self.store.workflow_run_start(run_id=run_id, graph_version=self.graph.graph_version)
        # This helper constructs a current-format checkpoint directly. Match
        # real graph startup by persisting its immutable provenance first.
        # Missing historical manifests are tested separately and must fail closed.
        self.graph._save_manifest(run_id)
        wf = build_workflow_state(event=CareEvent('register_profile', '合成报告'),
            session_id='s', turn_id=run_id, run_id=run_id, event_id=run_id, client_event_id=None,
            budget=initial_budget())
        wf.update(review_reason_codes=['severe_warning'], review_logic_key='event:' + run_id + ':severe_warning:r1',
                  result={'text': '需要进一步核实。建议咨询医生/药师。', 'warnings': [], 'conflicts': [], 'audit_trail': {}})
        config = {'configurable': {'thread_id': run_id}}
        with budget_scope(self.store, run_id):
            wf = self.graph._node_open_review(wf)
        self.graph._ensure_graph().update_state(config, wf, as_node='open_review')
        self.graph.run(event=CareEvent('register_profile', '合成报告'), session_id='s', turn_id=run_id)
        return self.store.review_case(wf['review_case']['id'])

    def decide(self, case, action='close_with_safe_guidance'):
        claimed = self.store.claim_review_case(case['id'], expected_revision=case['revision'], assignee='test-reviewer')
        return self.store.record_review_decision(case_id=case['id'], expected_revision=claimed['revision'],
            action=action, payload={}, idempotency_key='review:' + str(case['id']), actor_id='test-reviewer')

    def move_facts(self):
        from stage0.memory import SemanticFact
        self.store.write_semantic_fact(SemanticFact(namespace='age', key='age', value=71), source='synthetic')

    def assert_round_two_and_complete(self, old_id):
        with self.assertNoLogs('stage0.server', level='ERROR'):
            self.worker.drain_resume_tasks()
        old = self.store.review_case(old_id)
        self.assertEqual(old['status'], 'cancelled')
        self.assertEqual(self.store.review_decisions_for(old_id)[0]['outcome'], 'review_stale')
        cases = [c for c in self.store.review_cases() if c['run_id'] == 'r' and c['round'] == 2]
        self.assertEqual(len(cases), 1)
        new = cases[0]
        self.assertEqual(new['summary']['verification_status'], 'incomplete')
        self.assertNotIn('response_text', new['summary'])
        self.assertEqual(new['summary']['current_facts']['semantic'][0]['value'], 71)
        state = self.graph._ensure_graph().get_state({'configurable': {'thread_id': 'r'}})
        self.assertEqual(state.values['review_case']['id'], new['id'])
        self.assertEqual(state.next, ('await_review',))
        self.assertEqual(state.tasks[0].interrupts[0].value['case_id'], new['id'])
        task = self.store.connection.execute('SELECT * FROM resume_tasks WHERE case_id=?', (old_id,)).fetchone()
        self.assertEqual(task['status'], 'consumed')
        self.decide(new)
        with self.assertNoLogs('stage0.server', level='ERROR'):
            self.worker.drain_resume_tasks()
        self.assertEqual(self.store.workflow_run_get('r')['status'], 'succeeded')
        self.assertEqual(self.store.review_case(new['id'])['status'], 'resolved')
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM episodic_memory WHERE event_type='clinical_review'").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM resume_tasks WHERE status='pending'").fetchone()[0], 0)

    def test_stale_round_and_human_wait_preserve_budget(self):
        case = self.parked()
        before = self.store.workflow_run_get('r')['budget']
        with self.store.connection:
            self.store.connection.execute("UPDATE workflow_runs SET waiting_since='2026-01-01T00:00:00+00:00' WHERE run_id='r'")
        self.decide(case)
        self.move_facts()
        self.assert_round_two_and_complete(case['id'])
        after = self.store.workflow_run_get('r')['budget']
        self.assertEqual(before['token_budget'], after['token_budget'])
        self.assertEqual(before['calls_attempted'], after['calls_attempted'])
        self.assertGreaterEqual(after['consumed_seconds'], before['consumed_seconds'])
        self.assertLess(after['consumed_seconds'] - before['consumed_seconds'], 10)
        self.assertGreater(self.store.workflow_run_get('r')['human_wait_seconds'], 86400)

    def test_retryable_backoff_cap_and_effect_unknown(self):
        from stage0.memory import EffectUnknownError
        case = self.parked()
        self.decide(case)
        for attempt in range(3):
            with mock.patch.object(self.graph, 'resume', side_effect=ConnectionError('offline fault')), self.assertLogs('stage0.server', level='ERROR'):
                receipts = self.worker.drain_resume_tasks()
            self.assertEqual(receipts[0]['error_class'], 'retryable')
            self.assertEqual(receipts[0]['execution_state'], 'retryable' if attempt < 2 else 'failed')
            self.assertEqual(self.store.pending_resume_tasks(), [])
            with self.store.connection:
                self.store.connection.execute('UPDATE resume_tasks SET next_attempt_at=NULL')
        self.assertEqual(self.store.pending_resume_tasks(), [])
        other = self.parked('other')
        decision = self.decide(other)
        self.store.set_review_decision_outcome(decision['decision_id'], outcome='applied')
        with self.assertLogs('stage0.server', level='ERROR'):
            receipts = self.worker.drain_resume_tasks()
        self.assertEqual(receipts[0]['error_class'], 'effect_unknown')
        self.assertEqual(receipts[0]['execution_state'], 'failed')

    def test_incomplete_new_evidence_cannot_confirm_fact(self):
        case = self.parked()
        self.decide(case)
        self.move_facts()
        self.worker.drain_resume_tasks()
        new = self.store.review_cases()[0]
        self.decide(new, 'confirm_reported_fact')
        with self.assertLogs('stage0.server', level='ERROR'):
            receipts = self.worker.drain_resume_tasks()
        self.assertEqual(receipts[0]['error_class'], 'permanent')
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM episodic_memory WHERE event_type='clinical_review'").fetchone()[0], 0)

    def test_review_safety_failure_is_classified_and_effect_receipt_survives(self):
        case = self.parked()
        self.decide(case)
        with mock.patch.object(self.graph, '_validate_result', side_effect=RuntimeError('safety boundary: injected')), self.assertLogs('stage0.server', level='ERROR'):
            receipts = self.worker.drain_resume_tasks()
        self.assertEqual(receipts[0]['error_class'], 'safety')
        self.assertEqual(receipts[0]['execution_state'], 'failed')

    def test_crash_matrix_stale_transitions_and_ack(self):
        # Each boundary has its own database and real persisted graph recovery.
        for method in ('close_review_case', 'open_review_case', 'audit_review_stale', 'consume_resume_task'):
            for when in ('before','after'):
                with self.subTest(method=method, when=when):
                    if self.store.review_cases():
                        self.tearDown()
                        self.setUp()
                    case = self.parked()
                    self.decide(case)
                    self.move_facts()
                    original = getattr(self.store, method)
                    fired = []
                    def crash(*args, **kwargs):
                        if fired:
                            return original(*args, **kwargs)
                        fired.append(1)
                        if when == 'after':
                            original(*args, **kwargs)
                        raise SystemExit('injected boundary crash')
                    with mock.patch.object(self.store, method, side_effect=crash), self.assertRaises(SystemExit):
                        self.worker.drain_resume_tasks()
                    self.assertEqual(fired, [1])
                    self.restart()
                    self.assert_round_two_and_complete(case['id'])

    def test_crash_matrix_graph_checkpoint(self):
        for when in ('before', 'after'):
            with self.subTest(when=when):
                if self.store.review_cases():
                    self.tearDown()
                    self.setUp()
                case = self.parked()
                self.decide(case)
                self.move_facts()
                saver = self.graph._ensure_checkpointer()
                original = saver.put
                fired = []
                def crash(config, checkpoint, metadata, new_versions):
                    new_case = checkpoint.get('channel_values', {}).get('review_case') or {}
                    if new_case.get('round') == 2 and not fired:
                        fired.append(1)
                        if when == 'after':
                            original(config, checkpoint, metadata, new_versions)
                        raise SystemExit('injected checkpoint crash')
                    return original(config, checkpoint, metadata, new_versions)
                with mock.patch.object(saver, 'put', side_effect=crash), self.assertRaises(SystemExit):
                    self.worker.drain_resume_tasks()
                self.assertEqual(fired, [1])
                self.restart()
                self.assert_round_two_and_complete(case['id'])

    def test_effect_and_receipt_atomic_on_failure_and_replayed_after_commit(self):
        case = self.parked()
        decision = self.decide(case)
        original = self.store._review_receipt_tx
        with mock.patch.object(self.store, '_review_receipt_tx', side_effect=SystemExit('before receipt')), self.assertRaises(SystemExit):
            self.worker.drain_resume_tasks()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM episodic_memory WHERE event_type='clinical_review'").fetchone()[0], 0)
        self.assertEqual(self.store.review_decision(decision['decision_id'])['outcome'], 'recorded')
        self.restart()
        original_effect = self.store.apply_review_effect
        def after_commit(*args, **kwargs):
            original_effect(*args, **kwargs)
            raise SystemExit('after effect commit before checkpoint')
        with mock.patch.object(self.store, 'apply_review_effect', side_effect=after_commit), self.assertRaises(SystemExit):
            self.worker.drain_resume_tasks()
        self.restart()
        with self.assertNoLogs('stage0.server', level='ERROR'):
            self.worker.drain_resume_tasks()
        self.assertEqual(self.store.workflow_run_get('r')['status'], 'succeeded')
        self.assertEqual(self.store.review_case(case['id'])['status'], 'resolved')
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM episodic_memory WHERE event_type='clinical_review'").fetchone()[0], 1)
        self.assertEqual(self.store.pending_resume_tasks(), [])

    def test_permanent_resume_failure_does_not_starve_other_runs(self):
        first, second = self.parked('r'), self.parked('r2')
        self.decide(first)
        self.decide(second)
        original = self.graph.resume
        def fail_first(run_id, value):
            if run_id == 'r':
                raise AttributeError('injected programmer error')
            return original(run_id, value)
        with mock.patch.object(self.graph, 'resume', side_effect=fail_first), self.assertLogs('stage0.server', level='ERROR'):
            receipts = self.worker.drain_resume_tasks()
        self.assertEqual(receipts[0]['error_class'], 'permanent')
        self.assertEqual(receipts[1]['status'], 'resumed')
        self.assertEqual(self.store.workflow_run_get('r2')['status'], 'succeeded')
        self.assertEqual(self.store.pending_resume_tasks(), [])

    def test_run_internal_error_never_restarts_from_start(self):
        self.parked()
        with mock.patch.object(self.graph._ensure_graph(), 'invoke', side_effect=AttributeError('broken')), mock.patch.object(self.graph, '_invoke_fresh') as fresh:
            with self.assertRaises(AttributeError):
                self.graph.run(event=CareEvent('register_profile', '合成报告'), session_id='s', turn_id='r')
            fresh.assert_not_called()


def reply(content='{}', tokens=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=[]))],
              usage=None if tokens is None else NS(total_tokens=tokens))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.replies.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / 'test.db')
        self.store.workflow_run_start(run_id='r', graph_version='legacy')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_exact_token_boundary_actual_usage_and_transport_timeout(self):
        client = FakeClient([reply(tokens=10)])
        with mock.patch.dict(os.environ, {'AGENT_TURN_TOKEN_BUDGET': '10'}), budget_scope(self.store, 'r') as budget:
            completion_call('planner', client, messages=[], max_tokens=100)
            self.assertEqual(budget.data['tokens_actual'], 10)
            self.assertEqual(budget.exhausted(), 'tokens')
            with self.assertRaises(BudgetExceeded):
                completion_call('composer', client, messages=[])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]['max_tokens'], 10)
        self.assertGreater(client.calls[0]['timeout'], 0)
        self.assertLessEqual(client.calls[0]['timeout'], 60)

    def test_parse_retry_composer_verifier_and_tool_llm_accounted(self):
        from stage0.agent import AgentState, LLMPlanner, ResponseComposer, ResponseVerifier
        from stage0.memory import StructuredFactExtractor
        from stage0 import extract_ddi
        client = FakeClient([reply(''), reply(json.dumps(_repeat_read_provider({}))),
                             reply('建议咨询医生/药师。'), reply('{"verdict":"pass","findings":[]}'),
                             reply('{"semantic":[],"episodic":[],"working":[]}'), reply()])
        state = AgentState(session_id='s', turn_id='r', event=CareEvent('query_current_medications', '查询'))
        with budget_scope(self.store, 'r') as budget:
            LLMPlanner(client=client, model='fake').propose(state)
            ResponseComposer(client=client, model='fake').compose({})
            ResponseVerifier(client=client, model='fake').review('建议咨询医生/药师。',
                warnings=[], conflicts=[], memory_refs=[], semantic_errors=['semantic'])
            with mock.patch.object(extract_ddi, 'resolve_llm_config', return_value={'model': 'fake', 'provider': 'fake'}), mock.patch.object(extract_ddi, 'create_llm_client', return_value=client):
                StructuredFactExtractor(enabled=True)._llm_extract('合成报告')
            extract_ddi._production_call(client, 'fake', '合成药物', '合成说明书', delay=0)
            self.assertEqual(budget.data['calls_attempted'], 6)
            self.assertGreater(budget.data['tokens_estimated'], 0)
        kinds = [r[0] for r in self.store.connection.execute('SELECT kind FROM llm_attempts ORDER BY created_at,rowid')]
        self.assertEqual(kinds, ['planner','planner','composer','verifier','fact_extractor','ddi_extractor'])
        self.assertTrue(all('timeout' in c for c in client.calls))

    def test_provider_rejection_and_composer_adaptation_count(self):
        from stage0.agent import ResponseComposer
        client = FakeClient([RuntimeError('thinking unsupported'), reply('安全模板')])
        with budget_scope(self.store, 'r') as budget:
            ResponseComposer(client=client, model='fake').compose({})
            self.assertEqual(budget.data['calls_attempted'], 2)
        rows = list(self.store.connection.execute('SELECT status,usage_tokens FROM llm_attempts'))
        self.assertEqual([r['status'] for r in rows], ['failed_estimate','estimate'])
        self.assertTrue(all(r['usage_tokens'] is None for r in rows))

    def test_guard_rejection_is_charged(self):
        calls = []
        def provider(payload):
            calls.append(1)
            return {'decision': 'respond'}
        agent = MedicationCoordinatorAgent(self.store, llm_planner_enabled=True,
            proposal_provider=provider, response_provider=_composer_unavailable)
        with mock.patch.dict(os.environ, {'AGENT_TURN_CALL_BUDGET': '1'}):
            response = agent.handle(CareEvent('register_profile', '合成档案'), session_id='s', turn_id='r')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.workflow_run_get('r')['budget']['calls_attempted'], 1)
        self.assertTrue(any(t.get('exhausted') == 'calls' for t in response.tool_trace))

    def test_unsettled_attempt_survives_process_restart_without_new_budget(self):
        def crashed(payload, timeout):
            row = self.store.unsettled_llm_attempts('r')[0]
            self.assertIsNone(row['usage_tokens'])
            self.assertGreater(row['reserved_tokens'], 0)
            self.assertGreater(row['reserved_seconds'], 0)
            raise SystemExit('simulated process death')
        with self.assertRaises(SystemExit), budget_scope(self.store, 'r'):
            provider_call('planner', crashed, {})
        self.store.close()
        self.store = MemoryStore(Path(self.temp.name) / 'test.db')
        for _ in range(2):
            with budget_scope(self.store, 'r') as budget:
                self.assertEqual(budget.exhausted(), 'usage_unknown')
                with self.assertRaises(BudgetExceeded):
                    provider_call('planner', lambda p: self.fail('must not dispatch'), {})
                self.assertEqual(budget.data['calls_attempted'], 1)
        self.assertEqual(self.store.unsettled_llm_attempts('r')[0]['status'], 'reserved')

    def test_transport_unknown_and_late_sync_response_are_not_accepted(self):
        with budget_scope(self.store, 'r') as budget:
            with self.assertRaises(TimeoutError):
                provider_call('planner', mock.Mock(side_effect=TimeoutError()), {})
            self.assertEqual(budget.exhausted(), 'usage_unknown')
            self.assertEqual(self.store.unsettled_llm_attempts('r')[0]['status'], 'unknown')
        self.store.workflow_run_start(run_id='late', graph_version='legacy')
        def late(payload, timeout):
            time.sleep(.03)
            return 'late result'
        with budget_scope(self.store, 'late') as budget:
            with self.assertRaises(BudgetExceeded):
                provider_call('composer', late, {}, timeout=.01)
            self.assertGreaterEqual(budget.data['consumed_seconds'], .03)
        row = self.store.connection.execute("SELECT status FROM llm_attempts WHERE run_id='late'").fetchone()
        self.assertEqual(row['status'], 'late')

    def test_checkpoint_cannot_decrease_counters_or_change_limits(self):
        with budget_scope(self.store, 'r') as budget:
            provider_call('planner', lambda p: 'result', {})
            original = budget.sync()
        self.store.workflow_run_update('r', budget={**original, 'tokens_estimated': 0,
            'calls_attempted': 0, 'consumed_seconds': 0, 'token_budget': 999999})
        stored = self.store.workflow_run_get('r')['budget']
        self.assertEqual(stored['calls_attempted'], 1)
        self.assertEqual(stored['token_budget'], original['token_budget'])
        self.assertGreaterEqual(stored['tokens_estimated'], original['tokens_estimated'])

    def test_pre_harness_unknown_usage_does_not_disappear_on_second_restart(self):
        self.store.workflow_run_update('r', budget={'consumed_seconds': 1})
        for _ in range(2):
            with budget_scope(self.store, 'r') as budget:
                self.assertEqual(budget.exhausted(), 'usage_unknown')

    def test_legacy_checkpoint_without_ledger_fails_closed_across_restarts(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE workflow_runs SET state_schema_version='1',budget_json=NULL WHERE run_id='r'")
        for _ in range(2):
            with budget_scope(self.store, 'r', saved={'tokens_estimated': 0, 'consumed_seconds': 0}) as budget:
                self.assertEqual(budget.exhausted(), 'usage_unknown')
                with self.assertRaises(BudgetExceeded):
                    provider_call('planner', lambda p: self.fail('old usage not reconstructable'), {})

    def test_lost_lease_during_extraction_rejects_projection(self):
        from stage0.memory import LeaseRejected
        from stage0.turn_budget import lease_scope
        valid = [True]
        def guard():
            if not valid[0]:
                raise LeaseRejected('expired')
        def extract(text):
            valid[0] = False
            return {'semantic': [], 'episodic': [], 'working': []}, 'fake', None
        with lease_scope(guard), mock.patch.object(self.store.extractor, 'extract', side_effect=extract):
            with self.assertRaises(LeaseRejected):
                self.store.consolidate_interaction(session_id='s', turn_id='r', user_text='synthetic')
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM interactions WHERE process_status='committed'").fetchone()[0], 0)

    def test_trace_does_not_export_patient_text_or_model_rationale(self):
        secret = 'synthetic-patient-body'
        self.store.record_turn_trace('s','r',1,'plan', {'phase': 'plan',
            'decision': {'tool': 'memory_read', 'arguments': {'query': secret}, 'rationale': secret},
            'candidate': secret, 'observation': secret, 'delivered_text': secret})
        self.assertNotIn(secret, json.dumps(self.store.traces_for_turn('s','r')))

    def test_schema_reopen_preserves_legacy_resume_rows(self):
        # Recreate only the old empty table in this synthetic db, then migrate.
        self.store.connection.execute('DROP TABLE resume_tasks')
        self.store.connection.execute("CREATE TABLE resume_tasks(id INTEGER PRIMARY KEY,operation_id TEXT UNIQUE,run_id TEXT,case_id INTEGER,decision_id TEXT,status TEXT CHECK(status IN ('pending','consumed')),created_at TEXT,consumed_at TEXT)")
        self.store.connection.execute("INSERT INTO resume_tasks VALUES(1,'op','r',1,'d','pending','2026-01-01',NULL)")
        self.store.connection.commit()
        self.store.close()
        self.store = MemoryStore(Path(self.temp.name) / 'test.db')
        task = self.store.pending_resume_tasks()[0]
        self.assertEqual(task['operation_id'], 'op')
        self.assertEqual(task['execution_state'], 'ready')
        self.assertEqual(task['attempts'], 0)

    def test_actual_detector_script_imports_share_budget_and_http_gate(self):
        from stage0 import ddi_engine
        from stage0.turn_budget import CURRENT
        self.assertIs(ddi_engine.extract_ddi.CURRENT, CURRENT)
        client = FakeClient([reply()])
        with budget_scope(self.store, 'r') as budget:
            ddi_engine.extract_ddi._production_call(client, 'fake', '药物', '说明书')
            http = mock.MagicMock()
            http.__enter__.return_value.read.return_value = b'result'
            with mock.patch.object(ddi_engine.crosscheck_eval.urllib.request, 'urlopen', return_value=http) as opener:
                self.assertEqual(ddi_engine.crosscheck_eval._read_url('https://example.test'), 'result')
            self.assertEqual(budget.data['calls_attempted'], 2)
            self.assertLessEqual(opener.call_args.kwargs['timeout'], 30)
            self.assertGreater(opener.call_args.kwargs['timeout'], 0)
            budget.data['calls_attempted'] = budget.data['call_budget']
            with mock.patch.object(ddi_engine.crosscheck_eval.urllib.request, 'urlopen') as opener, self.assertRaises(BudgetExceeded):
                ddi_engine.crosscheck_eval._read_url('https://example.test')
            opener.assert_not_called()


class BudgetParityTests(unittest.TestCase):
    def test_graph_process_restart_keeps_attempts_after_plan_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = MemoryStore(path / 'test.db')
            provider = mock.Mock(side_effect=_repeat_read_provider)
            def make():
                agent = MedicationCoordinatorAgent(store, max_cycles=4, llm_planner_enabled=True,
                    proposal_provider=provider, response_provider=_composer_unavailable)
                return agent, LangGraphAgentRunner(agent, checkpoint_path=path / 'cp.db')
            agent, graph = make()
            kwargs = dict(event=CareEvent('user_message','查询用药证据'),session_id='s',turn_id='r',run_id='r')
            try:
                with mock.patch.dict(os.environ, {'AGENT_TURN_CALL_BUDGET':'1'}), mock.patch.object(agent, '_act', side_effect=SystemExit('crash after plan')), self.assertRaises(SystemExit):
                    graph.run(**kwargs)
                before = store.workflow_run_get('r')['budget']
                graph.close()
                store.close()
                store = MemoryStore(path / 'test.db')
                agent, graph = make()
                with mock.patch.dict(os.environ, {'AGENT_TURN_CALL_BUDGET':'100','AGENT_TURN_TOKEN_BUDGET':'999999'}):
                    response = graph.run(**kwargs)
                after = store.workflow_run_get('r')['budget']
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(after['call_budget'], 1)
                self.assertEqual(after['token_budget'], before['token_budget'])
                self.assertEqual(after['tokens_estimated'], before['tokens_estimated'])
                self.assertTrue(any(t.get('exhausted') == 'calls' for t in response.tool_trace))
            finally:
                graph.close()
                store.close()
    def test_zero_limits_never_dispatch_or_claim_recording(self):
        for runner_type in (LegacyAgentRunner, LangGraphAgentRunner):
            for name in ('AGENT_TURN_TOKEN_BUDGET','AGENT_TURN_CALL_BUDGET','AGENT_TURN_BUDGET_SECONDS','cycles'):
                with self.subTest(runner=runner_type.__name__, limit=name), tempfile.TemporaryDirectory() as directory:
                    with MemoryStore(Path(directory) / 'test.db') as store:
                        provider = mock.Mock(side_effect=AssertionError('unexpected external call'))
                        agent = MedicationCoordinatorAgent(store, max_cycles=0 if name == 'cycles' else 4,
                            llm_planner_enabled=True, proposal_provider=provider, response_provider=provider)
                        runner = runner_type(agent)
                        try:
                            with mock.patch.dict(os.environ, {name: '0'}):
                                response = runner.run(event=CareEvent('register_profile', '档案'),
                                    session_id='s', turn_id='r', run_id='r')
                            provider.assert_not_called()
                            self.assertIn('尚未保存', response.text)
                            self.assertEqual(store.workflow_run_get('r')['budget']['calls_attempted'], 0)
                            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 0)
                        finally:
                            if hasattr(runner, 'close'):
                                runner.close()
    def test_small_token_budget_same_termination_and_persistence(self):
        for runner_type in (LegacyAgentRunner, LangGraphAgentRunner):
            with self.subTest(runner=runner_type.__name__), tempfile.TemporaryDirectory() as directory:
                with MemoryStore(Path(directory) / 'test.db') as store:
                    calls = []
                    def provider(payload):
                        calls.append(1)
                        return _repeat_read_provider(payload)
                    agent = MedicationCoordinatorAgent(store, max_cycles=4,
                        ddi_tool=DDITool(lambda meds: []), llm_planner_enabled=True,
                        proposal_provider=provider, response_provider=_composer_unavailable)
                    runner = runner_type(agent)
                    try:
                        with mock.patch.dict(os.environ, {'AGENT_TURN_TOKEN_BUDGET': '200'}):
                            response = runner.run(event=CareEvent('user_message', '查询用药证据'),
                                session_id='s', turn_id='r', run_id='r')
                        self.assertEqual(len(calls), 1)
                        self.assertTrue(any(t.get('exhausted') == 'tokens' for t in response.tool_trace))
                        self.assertGreater(store.workflow_run_get('r')['budget']['tokens_estimated'], 0)
                        self.assertIn('未完成', response.text)
                    finally:
                        if hasattr(runner, 'close'):
                            runner.close()
