"""Offline contracts only. Scripted corrections are never model capability evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stage0 import rag
from stage0.agent import AgentState, CareEvent, LLMPlanner, Observation, RAGTool
from stage0.harness.retrieval import catalog, search
from stage0.harness.runtime import RunContext, Principal
from stage0.investigation import InvestigationState
from stage0.memory import MemoryStore
from stage0.test_harness_p1_b import make_agent, FakeRAG

CHUNKS = [
    {'chunk_id': 'target', 'drug_name': '氨氯地平', 'section': '用法用量',
     'source_url': 'label://target', 'text': '氨氯地平常用起始剂量为每日一次5mg；剂量调整需监测血压。'},
    {'chunk_id': 'indication', 'drug_name': '对照药', 'section': '适应症',
     'source_url': 'label://control', 'text': '用于高血压。'},
]


class RetrievalContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.index = patch.object(rag, 'INDEX_DIR', self.root)
        self.index.start()
        self.write_chunks(CHUNKS)
        self.tool = RAGTool(exact_only=True)

    def tearDown(self):
        self.index.stop()
        self.tmp.cleanup()

    def write_chunks(self, chunks):
        (self.root / 'chunks.jsonl').write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding='utf-8')

    def run_search(self, **args):
        return search(self.tool, {'query': '氨氯地平', **args}, scope_id='local-demo')

    def test_invalid_filter_never_dispatches_or_selects_next_query(self):
        with patch.object(self.tool, 'search_in_scope', side_effect=AssertionError('must not dispatch')):
            result = self.run_search(section='用药信息')
        self.assertEqual(result['status'], 'invalid_filter')
        self.assertFalse(result['search_executed'])
        self.assertEqual(result['applied_filters'], {'section': '用药信息'})
        self.assertEqual(result['directory']['sections'], ['用法用量', '适应症'])
        self.assertNotIn('suggested_query', result)

    def test_empty_legal_combination_no_match_and_found_are_distinct(self):
        empty = self.run_search(section='适应症', drug_name='氨氯地平')
        self.assertEqual(empty['status'], 'empty_filter_scope')
        self.assertFalse(empty['search_executed'])
        wrong = self.run_search(section='适应症')
        self.assertEqual(wrong['status'], 'no_match')
        self.assertEqual(wrong['scope']['filtered_chunks'], 1)
        self.assertTrue(wrong['search_executed'])
        self.assertEqual(self.run_search()['status'], 'found')
        self.assertEqual(self.run_search(section='用法用量')['status'], 'found')

    def test_unfiltered_genuine_absence_and_query_rewrite(self):
        self.assertEqual(self.run_search(query='完全不存在的检测指标')['status'], 'no_match')
        self.assertEqual(self.run_search(query='确认氨氯地平的用药信息是否准确。', section='用法用量')['status'], 'no_match')
        self.assertEqual(self.run_search(query='氨氯地平', section='用法用量')['status'], 'found')

    def test_execution_failure_is_not_no_match_or_filtered_absence(self):
        with patch.object(self.tool, 'search_in_scope', side_effect=TimeoutError('secret path and private section')):
            result = self.run_search()
        self.assertEqual(result['status'], 'retrieval_error')
        self.assertTrue(result['search_executed'])
        self.assertNotIn('secret', json.dumps(result))

    def test_budget_and_lease_interrupts_are_not_swallowed_as_retrieval_errors(self):
        from stage0.turn_budget import BudgetExceeded
        with patch.object(self.tool, 'search_in_scope', side_effect=BudgetExceeded('cap')):
            with self.assertRaises(BudgetExceeded):
                self.run_search()

    def test_existing_exact_fallback_preserved_with_explicit_primary_failure(self):
        tool = RAGTool()
        with patch.object(tool, '_get_retriever', side_effect=RuntimeError('primary backend unavailable')):
            found = search(tool, {'query': '氨氯地平', 'section': '用法用量'}, scope_id='local-demo')
            missing = search(tool, {'query': '不存在的文字'}, scope_id='local-demo')
        self.assertEqual(found['status'], 'found')
        self.assertTrue(found['retrieval_degraded'])
        self.assertEqual(found['applied_filters'], {'section': '用法用量'})
        self.assertEqual(found['error']['attribution'], 'deterministic_internal')
        self.assertEqual(missing['status'], 'retrieval_error')

    def test_version_change_invalidates_old_section_without_caching(self):
        old = catalog(self.tool, 'local-demo', {})
        self.write_chunks([dict(CHUNKS[0], section='剂量资料')])
        result = self.run_search(section='用法用量')
        self.assertEqual(result['status'], 'invalid_filter')
        self.assertNotEqual(result['corpus_version'], old['corpus_version'])
        self.assertEqual(result['directory']['sections'], ['剂量资料'])

    def test_foreign_metadata_does_not_change_visible_version_counts_or_results(self):
        before = catalog(self.tool, 'local-demo', {})
        self.write_chunks(CHUNKS + [dict(CHUNKS[0], scope_id='foreign', section='私密章节', drug_name='私密药')])
        self.assertEqual(before, catalog(self.tool, 'local-demo', {}))
        result = self.run_search(section='私密章节')
        self.assertEqual(result['status'], 'invalid_filter')
        self.assertNotIn('私密药', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['directory'], before['directory'])

    def test_bounded_directory_can_page_to_omitted_section(self):
        self.write_chunks([dict(CHUNKS[0], section=f's{i:03}') for i in range(40)])
        first = catalog(self.tool, 'local-demo', {})
        self.assertEqual(first['directory']['omitted_sections'], 8)
        second = catalog(self.tool, 'local-demo', {'offset': first['directory']['next_offset']})
        self.assertEqual(len(second['directory']['sections']), 8)
        self.assertIsNone(second['directory']['next_offset'])
        self.assertEqual(self.run_search(section='s039')['status'], 'found')

    def test_candidate_truncation_reports_backend_total_and_unknown_total(self):
        self.write_chunks([dict(CHUNKS[0], chunk_id=str(i)) for i in range(4)])
        result = self.run_search(top_k=1)
        self.assertTrue(result['truncation']['candidates_truncated'])
        self.assertEqual(result['truncation']['total_matches'], 4)
        unknown = search(FakeRAG(CHUNKS), {'query': '氨氯地平'}, scope_id='local-demo')
        self.assertIsNone(unknown['truncation']['candidates_truncated'])
        self.assertFalse(unknown['truncation']['total_matches_known'])

    def test_shared_executor_b1_b2_and_scoped_original_capture(self):
        with MemoryStore(self.root / 'db', llm_enabled=False) as store, patch.dict(os.environ, {'AGENT_EVIDENCE_INTERFACE': 'B2'}):
            self.write_chunks([dict(CHUNKS[0], scope_id='local-demo')])
            agent = make_agent(store, rag_tool=self.tool)
            state = AgentState('s', 'r', CareEvent('user_message', '核查'))
            ctx = RunContext(run_id='r', turn_id='r')
            for name in ['rag_search', 'acquire_evidence']:
                bad = agent.executor.execute(ctx, name, {'query': '氨氯地平', 'section': '用药信息'}, state=state)
                self.assertEqual(bad.value['status'], 'invalid_filter')
                self.assertFalse(bad.value['search_executed'])
            found = agent.executor.execute(ctx, 'acquire_evidence', {'query': '氨氯地平'}, state=state)
            self.assertEqual(found.value['pages'][0]['content'], CHUNKS[0]['text'])
            with self.assertRaises(Exception):
                agent.evidence_store.read(found.evidence_refs[0], scope_id='foreign')

    def test_attempt_search_budget_and_repeated_invalid_guard(self):
        with MemoryStore(self.root / 'db', llm_enabled=False) as store:
            agent = make_agent(store, rag_tool=self.tool)
            inv = InvestigationState(goal='核查', scope_id='local-demo')
            state = AgentState('s', 'r', CareEvent('user_message', '核查'), investigation=inv)
            state.ctx = RunContext(run_id='r', turn_id='r')
            def observe(args):
                result = agent.executor.execute(state.ctx, 'rag_search', args, state=state)
                obs = Observation('rag_search', 'investigation:x', args, result.value, cycle=1, evidence_refs=result.evidence_refs)
                inv.observe(obs, agent.evidence_store)
                return obs
            args = {'query': '氨氯地平', 'section': '用药信息'}
            verdicts = [agent._progress_verdict(state, observe(args)) for _ in range(3)]
            self.assertEqual(verdicts[-1], 'stop')
            self.assertEqual(len(inv.queries), 0)
            self.assertEqual(inv.retrieval_attempts, 3)
            corrected = observe({'query': '氨氯地平', 'section': '用法用量'})
            self.assertTrue(corrected.added_information)
            self.assertEqual(len(inv.queries), 1)
            self.assertEqual(inv.retrieval_attempts, 4)

    def test_feedback_and_truncation_survive_next_and_old_model_payloads(self):
        result = self.run_search(section='用药信息')
        obs = Observation('rag_search', 'investigation:x', {'query': '氨氯地平', 'section': '用药信息'}, result, cycle=1)
        state = AgentState('s', 'r', CareEvent('user_message', '核查'), observations=[obs], cycle=2)
        payload = LLMPlanner._bounded_observations(state)[0]
        self.assertEqual(payload['result']['status'], 'invalid_filter')
        self.assertEqual(payload['result']['directory'], result['directory'])
        state.cycle = 8
        old = LLMPlanner._bounded_observations(state)[0]
        self.assertEqual(old['retrieval_feedback']['status'], 'invalid_filter')
        found = self.run_search()
        found['results'][0]['text'] = '长原文' * 400
        state.observations = [Observation('rag_search', 'x', {}, found, cycle=8)]
        self.assertIn('已截断', LLMPlanner._bounded_observations(state)[0]['result']['results'][0]['text'])


class CorrectionLoopTests(unittest.TestCase):
    def test_scripted_feedback_correction_original_and_task_completion_both_interfaces(self):
        from stage0.agent_evals import run_visitprep as runner
        task = next(t for t in json.loads(runner.DATA.read_text(encoding='utf-8')) if t['task_id'] == 'vp-conflict-003b')
        task = copy.deepcopy(task)
        task['budget']['max_cycles'] = 14
        task['expected']['search_budget'] = 2
        original = runner.model_double
        for interface in ['B1', 'B2']:
            for first_args, first_status in [
                ({'query': '氨氯地平', 'section': '用药信息'}, 'invalid_filter'),
                ({'query': '确认氨氯地平的用药信息是否准确。', 'section': '药物相互作用'}, 'no_match')]:
                with self.subTest(interface=interface, first_status=first_status):
                    seen = []
                    def provider(payload):
                        proposal = original(payload)
                        if proposal.get('tool') == 'rag_search':
                            feedback = [o for o in payload['observations'] if o.get('tool') in {'rag_search', 'acquire_evidence'}]
                            proposal['tool'] = 'acquire_evidence' if interface == 'B2' else 'rag_search'
                            proposal['arguments'] = first_args if not feedback else {'query': '氨氯地平', 'top_k': 2}
                            if feedback:
                                value = feedback[-1].get('result') or feedback[-1].get('retrieval_feedback')
                                seen.append(value['status'])
                        return proposal
                    with patch.dict(os.environ, {'AGENT_EVIDENCE_INTERFACE': interface}), patch.object(runner, 'model_double', provider):
                        result = runner.run_task(task, 'scripted')
                    inv = result['observed']['investigation']
                    self.assertEqual(seen, [first_status])
                    self.assertTrue(inv['read_refs'])
                    self.assertEqual(inv['termination_reason'], 'checks_completed')
                    self.assertEqual(len(inv['queries']), 1 if first_status == 'invalid_filter' else 2)
                    self.assertEqual(inv['retrieval_attempts'], 2)
                    self.assertEqual(result['observed']['attribution']['policy_fallback'], 0)


if __name__ == '__main__':
    unittest.main()
