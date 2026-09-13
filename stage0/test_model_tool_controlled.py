"""Controlled-interface safety and measurement contracts; no remote calls."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stage0.agent import AgentState, CareEvent, LLMPlanner
from stage0.harness.runtime import RunContext
from stage0.harness.tools import ToolResult
from stage0.test_harness_p1_b import FakeRAG, make_agent
from stage0.memory import MemoryStore


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.env = patch.dict(os.environ, {'AGENT_EVIDENCE_INTERFACE': 'B2'})
        self.env.start()

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def agent(self, text='氨氯地平的说明书原文。'):
        return make_agent(self.store, rag_tool=FakeRAG([
            {'text': text, 'drug_name': '氨氯地平', 'section': '注意事项',
             'source_url': 'label://fixture', 'corpus_version': 'fixture-v1'}]))

    def run_acquire(self, agent, **args):
        state = AgentState('s', 'r', CareEvent('user_message', '核查用药'))
        ctx = RunContext(run_id='r', turn_id='r', session_id='s')
        return agent.executor.execute(ctx, 'acquire_evidence', {'query': '氨氯地平', **args}, state=state)

    def test_original_and_metadata_are_authorized_and_audited(self):
        agent = self.agent()
        result = self.run_acquire(agent)
        self.assertTrue(result.ok, result.error)
        page = result.value['pages'][0]
        self.assertEqual(page['content'], '氨氯地平的说明书原文。')
        self.assertEqual(page['corpus_version'], 'fixture-v1')
        self.assertIsNone(page['publication_date'])
        self.assertFalse(result.value['conclusion_approved'])
        self.assertEqual([o['tool'] for o in result.value['operations']],
                         ['rag_search', 'read_evidence', 'verify_source_metadata'])

    def test_truncation_does_not_claim_whole_document(self):
        result = self.run_acquire(self.agent('氨氯地平。' * 600))
        page = result.value['pages'][0]
        self.assertTrue(page['truncated'])
        self.assertEqual(page['returned_chars'], 2000)
        self.assertIn('remaining_original_pages', result.value['unfinished_checks'])

    def test_no_result_and_failed_search_are_distinct(self):
        agent = make_agent(self.store, rag_tool=FakeRAG([]))
        self.assertTrue(self.run_acquire(agent).value['no_results'])
        with patch.dict(agent.executor.handlers, {'rag_search': lambda r: 1 / 0}):
            value = self.run_acquire(agent).value
        self.assertFalse(value['no_results'])
        self.assertFalse(value['search_ok'])
        self.assertTrue(value['failures'])

    def test_inner_permission_denial_cannot_return_original(self):
        agent = self.agent()
        spec = agent.executor.specs['read_evidence']
        from dataclasses import replace
        agent.executor.specs['read_evidence'] = replace(spec, required_permission='no-such-permission')
        value = self.run_acquire(agent).value
        self.assertEqual(value['pages'], [])
        self.assertTrue(value['failures'])
        self.assertTrue(value['unread_refs'])

    def test_hash_tamper_and_foreign_scope_cannot_return_original(self):
        for column, value in [('content', 'tampered'), ('scope_id', 'another-patient')]:
            with self.subTest(column=column):
                agent = self.agent(column)
                read = agent.executor.handlers['read_evidence']
                def corrupt(request):
                    self.store.connection.execute('UPDATE evidence_records SET ' + column + '=? WHERE evidence_id=?',
                        (value, request.arguments['evidence_id']))
                    self.store.connection.commit()
                    return read(request)
                with patch.dict(agent.executor.handlers, {'read_evidence': corrupt}):
                    result = self.run_acquire(agent)
                self.assertEqual(result.value['pages'], [])
                self.assertTrue(result.value['failures'])

    def test_bound_and_default_off(self):
        agent = self.agent()
        self.assertFalse(self.run_acquire(agent, top_k=4).ok)
        with patch.dict(os.environ, {'AGENT_EVIDENCE_INTERFACE': 'B1'}):
            self.assertNotIn('acquire_evidence', self.agent().executor.catalog())


class LedgerAndMultiCallTests(unittest.TestCase):
    def test_real_budget_ledger_matches_success_refusal_retry_and_timeout(self):
        from stage0.turn_budget import budget_scope
        class RateLimitError(Exception):
            status_code = 429
        response = SimpleNamespace(id='r', model='fixture',
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content='{"decision":"respond"}'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        script = [RateLimitError('synthetic 429'), response, TimeoutError('synthetic timeout')]
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            result = script.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        client = SimpleNamespace(timeout=1, chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options = lambda **kwargs: client
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'PLANNER_PROVIDER_RETRIES': '1', 'PLANNER_PROVIDER_REFUND_REFUSALS': '0',
                'AGENT_TURN_CALL_BUDGET': '3', 'AGENT_TURN_TOKEN_BUDGET': '150000'}):
            with MemoryStore(Path(directory) / 'db', llm_enabled=False) as store:
                store.workflow_run_start(run_id='r', graph_version='legacy')
                planner = LLMPlanner(client=client, model='fixture')
                state = AgentState('s', 'r', CareEvent('user_message', '核查'))
                with budget_scope(store, 'r'), patch.object(planner, '_rate_limit_backoff'):
                    planner.propose(state)
                    attempts = list(planner.last_provider_attempts)
                    with self.assertRaises(Exception):
                        planner.propose(state)
                    attempts += planner.last_provider_attempts
                rows = [dict(r) for r in store.connection.execute('SELECT * FROM llm_attempts ORDER BY rowid')]
                self.assertEqual([a['attempt_id'] for a in attempts], [r['attempt_id'] for r in rows])
                self.assertEqual([a['outcome'] for a in attempts], ['rate_limit', 'response', 'timeout'])
                self.assertEqual([a['ledger_status'] for a in attempts], ['failed_estimate', 'actual', 'unknown'])
                self.assertEqual(len(requests), 3)
                self.assertTrue(all(r['parallel_tool_calls'] is False for r in requests))

    def test_rejected_response_keeps_attempt_and_all_unexecuted_items(self):
        from stage0.agent import HybridPlanner
        planner = LLMPlanner(model='fixture', proposal_provider=lambda p: {})
        planner.last_provider_attempts = [{'attempt_id': 'a', 'outcome': 'response'}]
        planner.last_multi_call_dropped = ['rag_search']
        planner.last_multi_call_dropped_detail = [{'call_id': 'c2', 'tool': 'rag_search', 'status': 'not_executed'}]
        hybrid = HybridPlanner(llm_planner=planner, enabled=True)
        import time
        trace = hybrid._trace('rejected', {}, 'rejected', False, [], None, time.perf_counter())
        self.assertEqual(trace['provider_attempts'], planner.last_provider_attempts)
        self.assertEqual(trace['not_executed_calls'][0]['call_id'], 'c2')
        self.assertIn('safety validation', planner._dropped_calls_note()['selection_policy'])

    def test_multicall_preserves_ids_and_arguments_without_executing(self):
        planner = LLMPlanner(model='fixture', proposal_provider=lambda p: {})
        calls = [SimpleNamespace(id='one', function=SimpleNamespace(name='memory_read', arguments='{"query":"snapshot"}')),
                 SimpleNamespace(id='two', function=SimpleNamespace(name='memory_write', arguments='{"operation":"x"}'))]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls, content=None))])
        planner._parse_response(response)
        detail = planner._dropped_calls_note()['not_executed'][0]
        self.assertEqual(detail['call_id'], 'two')
        self.assertEqual(detail['arguments'], '{"operation":"x"}')
        self.assertEqual(detail['status'], 'not_executed')


if __name__ == '__main__':
    unittest.main()
