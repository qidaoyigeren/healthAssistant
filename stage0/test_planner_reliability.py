"""Protocol v2 regressions (2026-09-10 planner-reliability round).

Covers, all offline and deterministic:
- actionable rejection feedback (correction_task) reaching the next payload;
- the identical-repeat fast breaker (saves a doomed model round-trip);
- correctable required-argument omissions (ddi_check.medications,
  memory_read.query under an open authority gap) and their env kill-switch;
- code-specific investigation rejection messages;
- the state-conditional tool catalog and planner_view protocol fields;
- the bounded 429 retry (success after one retry; exhausted → provider_error).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from stage0.agent import (AgentState, CareEvent, DDITool, HybridPlanner,
                          LLMPlanner, MedicationCoordinatorAgent,
                          PlannerPolicyGuard, PlannerProposalError)
from stage0.turn_budget import BudgetExceeded, budget_scope
from stage0.agent_evals.run_eval import DATA
from stage0.investigation import InvestigationState, CONTRACT, allowed_tools
from stage0.memory import MemoryStore
from stage0.test_agent_open_tasks import FixedRAG

TASKS = json.loads(DATA.read_text(encoding='utf-8'))


class RateLimitError(Exception):
    """Mirror of the provider exception name the retry check matches on."""


def seed(store):
    for item in TASKS[0]['initial_state']['medications']:
        store.apply_medication_change(action='add', name=item['name'], ingredients=[], session_id='synthetic',
            turn_id=item['name'], source='synthetic-fixture', dose=item['dose'], occurred_at=item['date'])


def scripted_agent(store, proposals, payloads=None):
    """LLM-enabled agent replaying a fixed proposal list; every payload the
    prompt layer builds is recorded so feedback wiring can be asserted.
    The live AgentState is captured through a decision spy."""
    payloads = payloads if payloads is not None else []
    calls = {'n': 0}
    states = []

    def provider(payload):
        payloads.append(payload)
        index = calls['n']
        calls['n'] += 1
        if index >= len(proposals):
            raise RuntimeError('scripted provider exhausted')
        return proposals[index]

    agent = MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda meds: []), rag_tool=FixedRAG(),
        llm_planner_enabled=True, proposal_provider=provider)
    original_decide = agent._decide

    def decide_spy(state):
        states.append(state)
        return original_decide(state)

    agent._decide = decide_spy
    agent._last_state = lambda: states[-1] if states else None
    return agent, provider, payloads


class PlannerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-planner-reliability-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        seed(self.store)
        # The interactive handle() path routes to the investigation contract
        # only when the capability flag is on, like the eval harness does.
        env = patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_rejection_feedback_reaches_next_payload(self):
        payloads = []
        agent, _, _ = scripted_agent(self.store, [
            {'decision': 'respond', 'rationale': '还没核查就想回答'},
            {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
             'expected_observation': '获得完整当前事实及版本'},
        ], payloads)
        event = TASKS[0]['events'][0]
        agent.handle(CareEvent('user_message', event['text']), session_id='synthetic', turn_id='t-0')
        self.assertGreaterEqual(len(payloads), 2)
        self.assertIsNone(payloads[0].get('correction_task'))
        correction = payloads[1].get('correction_task')
        self.assertIsInstance(correction, dict)
        self.assertEqual(correction['previous_proposal_was_rejected']['decision'], 'respond')
        codes = {e['code'] for e in correction['rejection_reasons']}
        self.assertIn('investigation_not_terminal', codes)
        messages = ' '.join(e['message'] for e in correction['rejection_reasons'])
        # The feedback names the concrete missing step, not a generic complaint.
        self.assertIn('termination_reason', messages)
        self.assertIn('memory_read', correction['allowed_tools_now'])
        self.assertNotIn('respond', correction['allowed_tools_now'])

    def test_identical_repeat_breaks_immediately(self):
        payloads = []
        stuck = {'decision': 'respond', 'rationale': 'same'}
        agent, _, _ = scripted_agent(self.store, [stuck, stuck, stuck, stuck], payloads)
        event = TASKS[0]['events'][0]
        with patch.dict(os.environ, {'PLANNER_SAFETY_REJECTION_LIMIT': '4'}):
            agent.handle(CareEvent('user_message', event['text']), session_id='synthetic', turn_id='t-0')
        # Old behaviour would burn rejection_limit=4 model calls; the repeat
        # breaker stops after the identical resubmission (2 calls total).
        self.assertEqual(2, len(payloads))
        self.assertEqual('planner_circuit_break:safety_rejections', agent._last_state().degraded_reason)
        notes = [e.get('note', '') for e in agent._last_state().trace]
        self.assertTrue(any('同一提案重复被拒' in note for note in notes))

    def test_run_open_review_rejection_trace_and_stop(self):
        payloads = []
        stuck = {'decision': 'respond', 'rationale': 'same'}
        agent, _, _ = scripted_agent(self.store, [stuck, stuck, stuck], payloads)
        event = TASKS[0]['events'][0]
        result = agent.run_open_review(event['text'], run_id='t-or-1', scope_id='local-demo')
        # Both rejections are traced (previously invisible in this loop) and
        # the identical repeat stops the run instead of burning the budget.
        self.assertEqual(2, len(payloads))
        self.assertEqual('planner_circuit_break:repeated_rejection', result['degraded_reason'])
        replans = [e for e in agent._last_state().trace if e.get('phase') == 'plan' and e.get('decision', {}).get('tool') == 'replan']
        self.assertEqual(2, len(replans))

    def test_correctable_ddi_medications_omission(self):
        guard = PlannerPolicyGuard(medication_grounding=self.store.current_medications,
                                   snapshot_provider=self.store.snapshot)
        state = AgentState('s', 't', CareEvent('user_message', '检查相互作用'))
        proposal = {'decision': 'tool', 'tool': 'ddi_check', 'purpose': 'check', 'arguments': {}}
        validation = guard.validate(state, proposal)
        self.assertTrue(validation.valid, validation.errors)
        self.assertIn('ddi_check.medications(authoritative)', guard.last_corrections)
        action = guard.materialize(state, proposal)
        expected = sorted(m['display_name'] for m in self.store.current_medications())
        self.assertEqual(expected, sorted(action.arguments['medications']))
        # Kill-switch restores the strict rejection.
        with patch.dict(os.environ, {'PLANNER_ARG_AUTOCORRECT': '0'}):
            guard2 = PlannerPolicyGuard(medication_grounding=self.store.current_medications,
                                        snapshot_provider=self.store.snapshot)
            self.assertFalse(guard2.validate(state, proposal).valid)

    def test_correctable_memory_read_query_under_authority_gap(self):
        inv = InvestigationState('核查当前用药', 'local-demo')
        inv.sync_authority(self.store)
        state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv)
        guard = PlannerPolicyGuard(medication_grounding=self.store.current_medications,
                                   snapshot_provider=self.store.snapshot)
        proposal = {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
                    'expected_observation': '获得完整当前事实及版本'}
        validation = guard.validate(state, proposal)
        self.assertTrue(validation.valid, validation.errors)
        action = guard.materialize(state, proposal)
        self.assertEqual({'query': 'snapshot'}, action.arguments)
        # A WRONG query value is still a hard rejection (authority contract).
        bad = dict(proposal, arguments={'query': 'conflicts'})
        self.assertFalse(guard.validate(state, bad).valid)

    def test_investigation_rejection_messages_are_specific(self):
        inv = InvestigationState('核查当前用药', 'local-demo')
        inv.sync_authority(self.store)
        guard = PlannerPolicyGuard(medication_grounding=self.store.current_medications,
                                   snapshot_provider=self.store.snapshot)
        state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv)
        proposal = {'decision': 'respond'}
        validation = guard.validate(state, proposal)
        self.assertFalse(validation.valid)
        message = validation.errors[0]['message']
        self.assertIn('authority', message)
        self.assertIn('termination_reason', message)
        # The old generic message is gone.
        self.assertNotEqual('proposal must address a current gap and observable outcome', message)
        # When evidence was found but not read, the feedback names the exact
        # missing step — the recorded live failure was respond-before-read.
        inv.evidence_refs.append('ev-x')
        again = guard.validate(state, {'decision': 'respond'})
        self.assertIn('read_evidence', again.errors[0]['message'])

    def test_state_conditional_catalog_and_view(self):
        inv = InvestigationState('核查当前用药', 'local-demo')
        inv.sync_authority(self.store)
        state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv)
        payloads = []
        agent, _, _ = scripted_agent(self.store, [{'decision': 'respond'}], payloads)
        payload = agent.planner.llm_planner.prompt_payload(state)
        catalog_names = {entry['name'] for entry in payload['tool_catalog']}
        self.assertEqual({'memory_write', 'memory_read'}, catalog_names)
        self.assertEqual('propose-next-action@3', payload['protocol']['version'])
        # v3: the payload advertises the same named functions the wire uses.
        function_names = {entry['function']['name'] for entry in payload['tool_functions']}
        self.assertEqual({'memory_write', 'memory_read'}, function_names)
        self.assertIn('evidence_unread', payload['investigation'])
        self.assertEqual({'memory_write', 'memory_read'}, set(payload['investigation']['allowed_tools']))
        # After authority read with open claim gaps, the catalog widens to
        # retrieval; read_evidence only appears once refs exist unread.
        inv.authority_read = True
        inv.checks['authority'] = 'checked'
        for g in inv.gaps:
            if g['gap_id'] == 'authority':
                g['status'] = 'resolved'
        inv.sync_authority(self.store)  # claims derive from the read authority
        payload2 = agent.planner.llm_planner.prompt_payload(state)
        names2 = {entry['name'] for entry in payload2['tool_catalog']}
        self.assertIn('rag_search', names2)
        self.assertNotIn('read_evidence', names2)
        inv.evidence_refs.append('ev-x')
        payload3 = agent.planner.llm_planner.prompt_payload(state)
        self.assertIn('read_evidence', {entry['name'] for entry in payload3['tool_catalog']})
        self.assertEqual(['ev-x'], payload3['investigation']['evidence_unread'])

    def test_rate_limit_retry_then_success(self):
        attempts = {'n': 0}

        def create(**kwargs):
            attempts['n'] += 1
            if attempts['n'] == 1:
                raise RateLimitError("Error code: 429 - {'error': {'code': '1305'}}")
            proposal = json.dumps({'decision': 'tool', 'tool': 'memory_read', 'arguments': {'query': 'snapshot'}})
            return NS(choices=[NS(message=NS(tool_calls=[NS(function=NS(name='propose_next_action', arguments=proposal))]))])

        client = NS(chat=NS(completions=NS(create=create)))
        planner = LLMPlanner(client=client, model='test-model')
        state = AgentState('s', 't', CareEvent('user_message', '你好'))
        with patch.dict(os.environ, {'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '0.01', 'PLANNER_PROVIDER_RETRIES': '1'}):
            proposal = planner.propose(state)
        self.assertEqual('memory_read', proposal['tool'])
        self.assertEqual(2, attempts['n'])

    def test_rate_limit_retry_exhausted(self):
        attempts = {'n': 0}

        def create(**kwargs):
            attempts['n'] += 1
            raise RateLimitError("Error code: 429 - busy")

        client = NS(chat=NS(completions=NS(create=create)))
        planner = LLMPlanner(client=client, model='test-model')
        state = AgentState('s', 't', CareEvent('user_message', '你好'))
        with patch.dict(os.environ, {'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '0.01', 'PLANNER_PROVIDER_RETRIES': '1'}):
            with self.assertRaises(Exception) as caught:
                planner.propose(state)
        self.assertIn('provider_error', getattr(caught.exception, 'code', ''))
        self.assertEqual(2, attempts['n'])  # 1 attempt + 1 bounded retry

    def test_timeout_is_never_retried(self):
        attempts = {'n': 0}

        def create(**kwargs):
            attempts['n'] += 1
            raise TimeoutError('synthetic timeout')

        client = NS(chat=NS(completions=NS(create=create)))
        planner = LLMPlanner(client=client, model='test-model')
        state = AgentState('s', 't', CareEvent('user_message', '你好'))
        with self.assertRaises(Exception):
            planner.propose(state)
        self.assertEqual(1, attempts['n'])  # unknown usage stays un-retried

    def test_graph_feedback_survives_checkpoint_and_new_planner(self):
        from stage0.graph_runner import LangGraphAgentRunner, build_workflow_state
        from stage0.turn_budget import BudgetExceeded, budget_scope
        stuck = {'decision': 'respond'}
        agent, _, first = scripted_agent(self.store, [stuck])
        runner = LangGraphAgentRunner(agent)
        wf = build_workflow_state(event=CareEvent('user_message', '核查当前用药证据'),
            session_id='s', turn_id='graph-feedback', run_id='graph-feedback',
            client_event_id=None, event_id=None, budget=None)
        self.store.workflow_run_start(run_id='graph-feedback', graph_version='1')
        with budget_scope(self.store, 'graph-feedback', 12), patch.dict(os.environ, {'PLANNER_SAFETY_REJECTION_LIMIT': '4'}):
            wf = runner._node_plan(wf)
            wf = json.loads(json.dumps(wf))
            self.assertTrue(wf['pending_correction'])
            fresh, _, second = scripted_agent(self.store, [stuck])
            restored = LangGraphAgentRunner(fresh)
            wf = restored._node_plan(wf)
            self.assertEqual(stuck, second[0]['correction_task']['previous_proposal_was_rejected'])
            self.assertTrue(wf['breaker']['broken'])
        runner.close()
        restored.close()

    def test_open_review_reports_provider_fallback_and_waiting_progress(self):
        from stage0.care_tasks import CareTasks
        from stage0.product import ProductStore
        from stage0.server import OutboxWorker
        from stage0.graph_runner import LegacyAgentRunner
        agent, _, _ = scripted_agent(self.store, [])
        tasks = CareTasks(ProductStore(self.store), agent_factory=lambda: agent)
        task = tasks.create('progress-create', 'evidence_review', goal='核查当前用药证据')
        accepted = tasks.resume(task['id'], 'progress-run', 1, enqueue=True)
        OutboxWorker(self.store, runner_factory=lambda: LegacyAgentRunner(agent)).drain_once()
        current = tasks.p.get(task['id'])
        self.assertEqual('waiting_input', current['status'])
        run = self.store.workflow_run_get(accepted['active_run_id'])
        self.assertEqual('degraded', run['status'])
        self.assertIn('provider_error', run['result']['planner_fallback_reasons'])
        progress = agent.progress_store.events_since(accepted['active_run_id'])
        self.assertEqual('waiting_input', progress['events'][-1]['kind'])
        self.assertFalse(any(e['kind'] == 'completed' for e in progress['events']))

    def test_dynamic_schema_matches_catalog_and_retains_reported_conditions(self):
        from stage0.memory import SemanticFact
        self.store.write_semantic_fact(SemanticFact('chronic_condition', 'reported', {'value': '合成既往病史'}), source='synthetic')
        inv = InvestigationState('核查当前用药', 'local-demo')
        inv.sync_authority(self.store)
        state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv)
        planner = LLMPlanner(proposal_provider=lambda _: {})
        payload = planner.prompt_payload(state)
        params = planner.function_schema(state)['function']['parameters']['properties']
        self.assertEqual({'memory_write', 'memory_read'}, set(params['tool']['enum']))
        self.assertEqual(['tool'], params['decision']['enum'])
        self.assertIn('query', params['arguments']['properties'])
        self.assertIn('arguments', planner.function_schema(state)['function']['parameters']['required'])
        self.assertTrue(any(f.get('namespace') == 'chronic_condition' for f in payload['investigation']['facts']['semantic']))

    def test_code_terminal_does_not_repeat_last_model_proposal(self):
        agent, _, _ = scripted_agent(self.store, [])
        inv = InvestigationState('核查当前用药', 'local-demo')
        inv.sync_authority(self.store)
        inv.termination_reason = 'checks_completed'
        state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv,
                           investigation_policy=CONTRACT)
        agent.planner.last_decision_trace = {'source': 'llm', 'proposal': {'decision': 'tool'}}
        self.assertIsNone(agent._decide(state))
        self.assertIsNone(agent.planner.last_decision_trace)

    def test_metrics_do_not_count_response_or_repeated_reads_as_progress(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('planner_metrics', Path(__file__).resolve().parents[1] / 'scripts/analyze-planner-metrics.py')
        metrics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(metrics)
        proposal = {'source': 'llm', 'model': 'synthetic', 'proposal': {'decision': 'respond'},
                    'validation': {'status': 'accepted', 'errors': []}}
        trace = [{'phase': 'plan', 'planner': proposal},
                 {'phase': 'investigation_progress', 'new_evidence_refs': [], 'new_read_refs': [], 'gap_changes': []}]
        artifact = {'tasks': [{'task_id': 'synthetic', 'score': {'passed': True, 'failures': []},
            'observed': {'goal_status': 'completed', 'degradation_reasons': [None], 'responses': [{'tool_trace': trace}],
                         'usage_ledger': [{'status': 'estimate', 'usage_tokens': None}]}}]}
        path = self.root / 'metric-case.json'
        path.write_text(json.dumps(artifact), encoding='utf-8')
        result = metrics.analyze_run(path)
        self.assertFalse(result['autonomous_success'])
        self.assertTrue(result['fallback_free'])
        self.assertEqual(1, result['actual_responses'])  # response without token usage
        self.assertEqual(0, result['advanced_ratio'])
        missing = [{'code': 'missing_required_arguments', 'message': 'arguments.query is required'}]
        self.assertFalse(metrics.correctable_rejection(missing, {'tool': 'rag_search', 'gap_id': 'claim:x'}))
        self.assertTrue(metrics.correctable_rejection(missing, {'tool': 'memory_read', 'gap_id': 'authority'}))

    def test_progress_is_available_before_worker_starts(self):
        from fastapi.testclient import TestClient
        from stage0.server import create_app
        from stage0.care_tasks import CareTasks
        from stage0.product import ProductStore
        app = create_app(db_path=self.root / 'queued.db', worker_thread=False)
        try:
            tasks = CareTasks(ProductStore(app.state.store))
            created = tasks.create('queue', 'evidence_review', goal='核查当前用药证据')
            accepted = tasks.resume(created['id'], 'resume', 1, enqueue=True)
            response = TestClient(app).get('/v1/runs/' + accepted['active_run_id'] + '/progress')
            self.assertEqual(200, response.status_code)
            self.assertEqual('queued', response.json()['run_status'])
        finally:
            app.state.store.close()


class ProviderOutageRetryTests(unittest.TestCase):
    """A transient provider outage must be visible to the caregiver and
    retryable — an honest bounded report, clearly labelled, with a way back.
    It must NOT be dressed up as a completed model-checked review."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-outage-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        seed(self.store)
        env = patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _tasks(self, agent_factory):
        from stage0.care_tasks import CareTasks
        from stage0.product import ProductStore
        return CareTasks(ProductStore(self.store), agent_factory=agent_factory)

    def _outage_agent(self):
        def provider(payload):
            raise RateLimitError("Error code: 429 - {'error': {'code': '1305'}}")
        return MedicationCoordinatorAgent(
            self.store, ddi_tool=DDITool(lambda meds: []), rag_tool=FixedRAG(),
            llm_planner_enabled=True, proposal_provider=provider)

    def _healthy_agent(self):
        return MedicationCoordinatorAgent(
            self.store, ddi_tool=DDITool(lambda meds: []), rag_tool=FixedRAG())

    def test_provider_outage_is_labelled_and_retryable(self):
        tasks = self._tasks(self._outage_agent)
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药证据')
        final = tasks.resume(task['id'], 't-resume-1', task['revision'])
        # The provider never answered, so the run degraded — whether the rule
        # fallback then reached a question or a bounded report.
        self.assertIn('provider_error', final.get('degraded_reason') or '')
        self.assertTrue(final.get('retry_available'))
        label = final.get('degraded_label') or ''
        self.assertIn('模型服务', label)
        self.assertIn('未经模型核查', label)
        self.assertNotIn('provider_error', label)  # no internal token leaks
        self.assertTrue(final['partial_report_refs'])

    def test_retry_entry_opens_only_for_a_marked_task(self):
        """A terminal task is normally closed for good; the ONLY door is the
        transient-outage marker, and using it consumes the marker."""
        from stage0.care_tasks import ProductError
        tasks = self._tasks(self._healthy_agent)
        task = tasks.create('t-create', 'evidence_review', goal='核查当前用药证据', budget=1)
        task = tasks.resume(task['id'], 'k1', task['revision'])
        final = tasks.resume(task['id'], 'k2', task['revision'])
        # A budget exhaustion is not transient: no retry offered, still closed.
        self.assertEqual('failed', final['status'])
        self.assertFalse(final.get('retry_available'))
        with self.assertRaises(ProductError):
            tasks.resume(task['id'], 'k3', final['revision'])
        stored = tasks.p.get(task['id'], 'care_task')
        stored['degraded_reason'] = 'planner_fallback:provider_error'
        stored['degraded_label'] = '本次未经模型核查：模型服务暂时不可用。以下为依据已保存记录整理的部分结果，可稍后重新核查。'
        stored['retry_available'] = True
        with tasks.p.transaction():
            tasks.p.save('care_task', stored)
        retried = tasks.resume(task['id'], 'k4', stored['revision'])
        self.assertIsNotNone(retried)
        self.assertFalse(tasks.p.get(task['id'], 'care_task').get('retry_available'))


class ProviderRefusalTests(unittest.TestCase):
    """A definitive provider refusal (429) runs no model, so it must not
    permanently eat the turn's model-call budget — otherwise a saturated
    provider starves the run it is supposed to serve.  Transport uncertainty
    (timeout/connection) keeps its charge: the remote outcome is unknown."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-refusal-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        self.store.workflow_run_start(run_id='r', graph_version='legacy')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _planner(self, replies):
        """Replies are exceptions (raised) or the literal 'ok' (a valid call)."""
        seen = {'n': 0}

        def create(**kwargs):
            item = replies[min(seen['n'], len(replies) - 1)]
            seen['n'] += 1
            if isinstance(item, Exception):
                raise item
            return NS(choices=[NS(message=NS(tool_calls=[NS(function=NS(
                name='memory_read', arguments=json.dumps({'query': 'snapshot'})))]))])

        return LLMPlanner(client=NS(chat=NS(completions=NS(create=create))),
                          model='test-model'), seen

    @staticmethod
    def _refusal():
        return RateLimitError("Error code: 429 - {'error': {'code': '1305'}}")

    def test_rate_limit_does_not_consume_call_budget(self):
        planner, seen = self._planner([self._refusal(), self._refusal(), 'ok'])
        state = AgentState('s', 't', CareEvent('user_message', '核查'))
        env = {'AGENT_TURN_CALL_BUDGET': '2', 'PLANNER_PROVIDER_RETRIES': '2',
               'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '0.01',
               'PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS': '0.01'}
        with patch.dict(os.environ, env):
            with budget_scope(self.store, 'r') as budget:
                proposal = planner.propose(state)
                data = budget.sync()
        self.assertEqual('tool', proposal['decision'])
        self.assertEqual(3, seen['n'])
        # Every transport attempt is still recorded (the ledger keeps the
        # rate-limit history); two of them are simply not charged.
        self.assertEqual(3, data['calls_attempted'])
        self.assertEqual(2, data['refused_calls'])
        self.assertEqual(1, data['calls_attempted'] - data['refused_calls'])

    def test_kill_switch_restores_the_old_starving_behaviour(self):
        """Causal control: same provider, same budget — only the refund off."""
        planner, _ = self._planner([self._refusal(), self._refusal(), 'ok'])
        state = AgentState('s', 't', CareEvent('user_message', '核查'))
        env = {'AGENT_TURN_CALL_BUDGET': '2', 'PLANNER_PROVIDER_RETRIES': '2',
               'PLANNER_PROVIDER_REFUND_REFUSALS': '0',
               'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '0.01',
               'PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS': '0.01'}
        with patch.dict(os.environ, env):
            with budget_scope(self.store, 'r') as budget:
                with self.assertRaises(BudgetExceeded):
                    planner.propose(state)
                data = budget.sync()
        self.assertEqual(0, data.get('refused_calls', 0))
        self.assertEqual(2, data['calls_attempted'])

    def test_refunded_refusal_is_not_token_charged_either(self):
        """Same fiction, one budget dimension over: a call that never ran
        processed no tokens, so it must not charge the token budget."""
        measured = {}
        for flag in ('1', '0'):
            with tempfile.TemporaryDirectory(prefix='synthetic-refund-tokens-') as directory:
                store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
                store.workflow_run_start(run_id='r', graph_version='legacy')
                try:
                    planner, _ = self._planner([self._refusal(), 'ok'])
                    env = {'AGENT_TURN_CALL_BUDGET': '8', 'PLANNER_PROVIDER_RETRIES': '1',
                           'PLANNER_PROVIDER_REFUND_REFUSALS': flag,
                           'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '0.01',
                           'PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS': '0.01'}
                    with patch.dict(os.environ, env), budget_scope(store, 'r') as budget:
                        planner.propose(AgentState('s', 't', CareEvent('user_message', '核查')))
                        measured[flag] = budget.sync()
                finally:
                    store.close()
        refunded, charged = measured['1'], measured['0']
        self.assertGreater(refunded['tokens_estimated'], 0)   # still observed
        self.assertLess(refunded['tokens_charged'], charged['tokens_charged'])
        self.assertEqual(refunded['calls_attempted'], charged['calls_attempted'])

    def test_timeout_still_consumes_call_budget(self):
        planner, _ = self._planner([TimeoutError('slow provider')])
        state = AgentState('s', 't', CareEvent('user_message', '核查'))
        with patch.dict(os.environ, {'AGENT_TURN_CALL_BUDGET': '8'}):
            with budget_scope(self.store, 'r') as budget:
                with self.assertRaises(PlannerProposalError):
                    planner.propose(state)
                data = budget.sync()
        self.assertEqual(1, data['calls_attempted'])
        self.assertEqual(0, data.get('refused_calls', 0))

    def test_refund_cannot_exceed_the_call_budget_so_retries_stay_bounded(self):
        """An always-refusing provider must terminate, not loop forever: the
        refund frees at most one call budget, so total attempts are capped."""
        planner, seen = self._planner([self._refusal()])
        state = AgentState('s', 't', CareEvent('user_message', '核查'))
        env = {'AGENT_TURN_CALL_BUDGET': '2', 'PLANNER_PROVIDER_RETRIES': '2',
               'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '0.01',
               'PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS': '0.01'}
        with patch.dict(os.environ, env):
            with budget_scope(self.store, 'r') as budget:
                with self.assertRaises(PlannerProposalError):
                    planner.propose(state)
                data = budget.sync()
        self.assertLessEqual(seen['n'], 2 * 2)
        self.assertLessEqual(data['refused_calls'], data['call_budget'])


class BackoffEnvelopeTests(unittest.TestCase):
    """The retry window must be able to reach the provider's real recovery
    time (~61s measured), while staying inside the turn's wall clock."""

    def test_backoff_grows_then_caps_at_the_configured_maximum(self):
        sleeps = []
        env = {'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '2',
               'PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS': '20'}
        with patch.dict(os.environ, env), \
                patch('stage0.agent.time.sleep', side_effect=lambda s: sleeps.append(s)):
            for attempt in range(1, 6):
                LLMPlanner._rate_limit_backoff(attempt)
        self.assertEqual([2, 4, 8, 16, 20], sleeps)

    def test_backoff_is_bounded_by_remaining_wall_clock(self):
        import time as real_time
        from stage0.turn_budget import BudgetExceeded, budget_scope
        with tempfile.TemporaryDirectory(prefix='synthetic-backoff-') as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            store.workflow_run_start(run_id='r', graph_version='legacy')
            sleeps = []
            env = {'AGENT_TURN_BUDGET_SECONDS': '10', 'AGENT_TURN_CALL_BUDGET': '8',
                   'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '2',
                   'PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS': '60'}
            try:
                with patch.dict(os.environ, env), budget_scope(store, 'r'):
                    with patch('stage0.agent.time.sleep', side_effect=lambda s: sleeps.append(s)):
                        for attempt in range(1, 6):
                            LLMPlanner._rate_limit_backoff(attempt)
            finally:
                store.close()
        self.assertTrue(sleeps)
        # The exponential would reach 32s by attempt 5; the turn has 10s, so
        # every sleep must be clamped to a fifth of what is left.
        self.assertLessEqual(max(sleeps), 10 * 0.2 + 0.05)
        self.assertTrue(all(s > 0 for s in sleeps))


class NamedFunctionProtocolTests(unittest.TestCase):
    """Protocol v3: tools are exposed as named functions.

    Root cause this locks in: under the unified ``propose_next_action`` schema
    the model filled every *named* property (``tool``/``gap_id``/
    ``expected_observation``) and omitted the opaque, unrequired ``arguments``
    object in 8/8 recorded live proposals — 6 rejected for
    ``arguments.query is required``, 2 hydrated by code.  Per-tool required
    arguments now live in each function's own ``parameters``.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-named-tools-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        seed(self.store)
        env = patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _planner_and_state(self, advance=False, client=None):
        inv = InvestigationState('核查当前用药', 'local-demo')
        inv.sync_authority(self.store)
        state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv)
        agent, _, _ = scripted_agent(self.store, [])
        planner = agent.planner.llm_planner
        planner.proposal_provider = None  # exercise the client/wire path
        if client is not None:
            planner.client = client
        if advance:
            # The stage the recorded live cohort failed at: authority read,
            # claim gaps open, retrieval permitted.
            inv.authority_read = True
            inv.checks['authority'] = 'checked'
            for gap in inv.gaps:
                if gap['gap_id'] == 'authority':
                    gap['status'] = 'resolved'
            inv.sync_authority(self.store)
            self.assertIn('rag_search', allowed_tools(inv))
        return planner, state, inv

    def test_per_tool_required_arguments_are_structural(self):
        planner, state, inv = self._planner_and_state(advance=True)
        by_name = {d['function']['name']: d['function']['parameters']
                   for d in planner.tool_definitions(state)}
        self.assertNotIn('propose_next_action', by_name)
        self.assertIn('query', by_name['memory_read']['required'])
        self.assertIn('query', by_name['rag_search']['required'])
        # memory_read restricts query to the enumerated views; rag_search does
        # not inherit that enum (the shared-parameter merge bug).
        self.assertIn('enum', by_name['memory_read']['properties']['query'])
        self.assertNotIn('enum', by_name['rag_search']['properties']['query'])
        # Investigator metadata stays expressed, because the model emitted it.
        self.assertIn('gap_id', by_name['rag_search']['required'])
        self.assertIn('expected_observation', by_name['rag_search']['required'])

    def test_executor_only_fields_never_reach_the_model_schema(self):
        planner, state, _ = self._planner_and_state()
        by_name = {d['function']['name']: d['function']['parameters']
                   for d in planner.tool_definitions(state)}
        write_props = set(by_name['memory_write']['properties'])
        self.assertIn('operation', write_props)
        # Hydrated from real observations; the prompt forbids the model to
        # supply them, so the advertised schema must not offer them either.
        for hydrated in ('warnings', 'context_refs', 'reported_event_ref', 'warning_ref'):
            self.assertNotIn(hydrated, write_props)

    def test_terminal_exposes_only_respond(self):
        planner, state, inv = self._planner_and_state()
        inv.termination_reason = 'checks_completed'
        names = [d['function']['name'] for d in planner.tool_definitions(state)]
        self.assertEqual(['respond'], names)

    def test_evidence_id_is_exposed_once_evidence_exists(self):
        planner, state, inv = self._planner_and_state()
        inv.authority_read = True
        inv.checks['authority'] = 'checked'
        for gap in inv.gaps:
            if gap['gap_id'] == 'authority':
                gap['status'] = 'resolved'
        inv.sync_authority(self.store)
        inv.evidence_refs.append('ev-x')
        by_name = {d['function']['name']: d['function']['parameters']
                   for d in planner.tool_definitions(state)}
        self.assertIn('read_evidence', by_name)
        self.assertIn('evidence_id', by_name['read_evidence']['required'])
        self.assertNotIn('evidence_id', by_name.get('rag_search', {}).get('properties', {}))

    def test_named_call_becomes_the_existing_internal_proposal(self):
        planner, state, _ = self._planner_and_state(advance=True)
        call = NS(function=NS(name='rag_search', arguments=json.dumps(
            {'query': '阿司匹林 布洛芬 相互作用', 'gap_id': 'claim:23cd3ebf4138',
             'expected_observation': '说明书相互作用证据'}, ensure_ascii=False)))
        response = NS(choices=[NS(message=NS(tool_calls=[call], content=None))])
        proposal = planner._parse_response(response)
        self.assertEqual({'decision': 'tool', 'tool': 'rag_search',
                          'arguments': {'query': '阿司匹林 布洛芬 相互作用'},
                          'gap_id': 'claim:23cd3ebf4138',
                          'expected_observation': '说明书相互作用证据'}, proposal)
        # The converted proposal then flows through the UNCHANGED validator.
        self.assertTrue(planner.guard.validate(state, proposal).valid)

    def test_named_call_missing_query_is_still_rejected_not_filled_in(self):
        planner, state, _ = self._planner_and_state(advance=True)
        call = NS(function=NS(name='rag_search', arguments=json.dumps(
            {'gap_id': 'claim:23cd3ebf4138', 'expected_observation': 'x'}, ensure_ascii=False)))
        response = NS(choices=[NS(message=NS(tool_calls=[call], content=None))])
        proposal = planner._parse_response(response)
        validation = planner.guard.validate(state, proposal)
        self.assertFalse(validation.valid)
        self.assertIn('missing_required_arguments',
                      [e['code'] for e in validation.errors])

    def test_respond_call_maps_to_respond_decision(self):
        planner, state, inv = self._planner_and_state()
        inv.termination_reason = 'checks_completed'
        call = NS(function=NS(name='respond', arguments=json.dumps({'rationale': '证据足够'})))
        response = NS(choices=[NS(message=NS(tool_calls=[call], content=None))])
        self.assertEqual({'decision': 'respond', 'rationale': '证据足够'},
                         planner._parse_response(response))

    def test_parsing_preserves_every_argument_the_provider_returned(self):
        """The recorded live proposals omitted ``arguments`` entirely; this
        pins that the parser itself never drops argument keys, so a future
        omission can only come from the model, not from response handling."""
        planner, state, _ = self._planner_and_state(advance=True)
        returned = {'query': '阿司匹林 布洛芬', 'section': '相互作用', 'top_k': 5,
                    'gap_id': 'claim:x', 'expected_observation': 'y'}
        call = NS(function=NS(name='rag_search',
                              arguments=json.dumps(returned, ensure_ascii=False)))
        response = NS(choices=[NS(message=NS(tool_calls=[call], content=None))])
        proposal = planner._parse_response(response)
        self.assertEqual({'query': '阿司匹林 布洛芬', 'section': '相互作用', 'top_k': 5},
                         proposal['arguments'])
        self.assertEqual('claim:x', proposal['gap_id'])
        self.assertEqual('y', proposal['expected_observation'])

    def test_decide_executes_a_named_function_call_end_to_end(self):
        """The shared decide()/propose() path that handle(), the LangGraph
        runner and the persistent-todo worker all call."""
        planner, state, inv = self._planner_and_state(advance=True)
        gap = next(g['gap_id'] for g in inv.gaps if g['status'] == 'open')
        returned = {'query': '阿司匹林 布洛芬 相互作用', 'gap_id': gap,
                    'expected_observation': '说明书相互作用证据'}
        call = NS(function=NS(name='rag_search', arguments=json.dumps(returned, ensure_ascii=False)))
        planner.client = NS(chat=NS(completions=NS(create=lambda **_: NS(
            choices=[NS(message=NS(tool_calls=[call], content=None))]))))
        planner.model = 'test-model'

        # planner IS agent.planner.llm_planner: the same HybridPlanner the
        # handle()/graph/todo entry points drive, bound to the executor catalog.
        agent, _, _ = scripted_agent(self.store, [])
        agent.planner.llm_planner = planner
        agent.planner.enabled = True
        action = agent.planner.decide(state)
        self.assertIsNotNone(action)
        self.assertEqual('rag_search', action.tool)
        self.assertEqual('阿司匹林 布洛芬 相互作用', action.arguments['query'])
        self.assertEqual(gap, action.gap_id)

    def test_legacy_unified_contract_is_still_parseable(self):
        planner, state, _ = self._planner_and_state()
        call = NS(function=NS(name='propose_next_action', arguments=json.dumps(
            {'decision': 'tool', 'tool': 'rag_search', 'arguments': {'query': 'q'}})))
        response = NS(choices=[NS(message=NS(tool_calls=[call], content=None))])
        self.assertEqual({'decision': 'tool', 'tool': 'rag_search', 'arguments': {'query': 'q'}},
                         planner._parse_response(response))

    def test_wire_request_carries_named_tools_and_required_query(self):
        """The decisive assertion: what the provider is actually sent."""
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return NS(choices=[NS(message=NS(content=json.dumps({'decision': 'respond'})))])

        planner, state, inv = self._planner_and_state(
            advance=True, client=NS(chat=NS(completions=NS(create=create))))
        planner.model = 'test-model'
        with patch.dict(os.environ, {'PLANNER_PROVIDER_RETRIES': '0'}):
            try:
                planner.propose(state)
            except Exception:
                pass  # only the serialized request matters here
        self.assertTrue(captured, 'propose() never reached the provider client')
        tools = {t['function']['name']: t['function']['parameters'] for t in captured['tools']}
        self.assertIn('rag_search', tools)
        self.assertIn('query', tools['rag_search']['required'])
        self.assertEqual('required', captured['tool_choice'])
        payload = json.loads(captured['messages'][1]['content'])
        self.assertIn('correction_task', payload)
        self.assertEqual(captured['tools'], payload['tool_functions'])


if __name__ == '__main__':
    unittest.main()
