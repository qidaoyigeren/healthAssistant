"""Offline regressions for failures observed with the real configured model."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from stage0.agent import AgentState, CareEvent, DDITool, MedicationCoordinatorAgent, Observation, PlannerPolicyGuard
from stage0.harness.runtime import RunContext
from stage0.memory import MemoryStore
from stage0 import extract_ddi


class LiveRegressionTests(unittest.TestCase):
    def test_zhipu_credentials_override_old_provider_and_disable_thinking(self):
        with patch.object(extract_ddi, '_load_dotenv'), patch.dict('os.environ', {
            'ZHIPU_API_KEY': 'test-zhipu', 'TOKENDANCE_API_KEY': 'test-old',
        }, clear=True):
            config = extract_ddi.resolve_llm_config()
            self.assertEqual('zhipu', config['provider'])
            self.assertEqual('test-zhipu', config['api_key'])
            self.assertEqual('glm-4.7-flash', config['model'])
            self.assertEqual('https://open.bigmodel.cn/api/paas/v4/', config['base_url'])
            self.assertEqual({'max_tokens': 4096, 'extra_body': {'thinking': {'type': 'disabled'}}},
                             extract_ddi.llm_completion_options())

    def test_explicit_provider_beats_the_auto_detected_precedence(self):
        """LLM_PROVIDER is the deliberate switch; presence of a Zhipu key must
        not override it (that precedence exists only for auto-detection)."""
        with patch.object(extract_ddi, '_load_dotenv'), patch.dict('os.environ', {
            'ZHIPU_API_KEY': 'test-zhipu', 'TOKENDANCE_API_KEY': 'test-tokendance',
            'LLM_PROVIDER': 'tokendance',
        }, clear=True):
            config = extract_ddi.resolve_llm_config()
            self.assertEqual('tokendance', config['provider'])
            self.assertEqual('test-tokendance', config['api_key'])
            self.assertEqual('glm-5.3-flash', config['model'])
            self.assertEqual('https://tokendance.space/gateway/v1', config['base_url'])
            # llm_completion_options resolves the provider on its own; it must
            # follow the same selector or the call carries the wrong options.
            self.assertEqual({'max_tokens': 1024, 'extra_body': {'thinking': {'type': 'disabled'}}},
                             extract_ddi.llm_completion_options())

    def test_unknown_or_unkeyed_selector_fails_loudly(self):
        with patch.object(extract_ddi, '_load_dotenv'), patch.dict(
                'os.environ', {'LLM_PROVIDER': 'not_a_provider'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'LLM_PROVIDER must be one of'):
                extract_ddi.resolve_llm_config()
        with patch.object(extract_ddi, '_load_dotenv'), patch.dict(
                'os.environ', {'LLM_PROVIDER': 'tokendance'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'API key is not configured'):
                extract_ddi.resolve_llm_config()

    def test_siliconflow_is_reachable_only_by_deliberate_selection(self):
        """A candidate added for a latency experiment must not become the
        endpoint that handles patient data because its key happens to be in a
        file.  Auto-detection therefore ignores SILICONFLOW_API_KEY entirely;
        only an explicit LLM_PROVIDER reaches it."""
        with patch.object(extract_ddi, '_load_dotenv'), patch.dict(
                'os.environ', {'SILICONFLOW_API_KEY': 'test-siliconflow'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'No LLM API key configured'):
                extract_ddi.resolve_llm_config()
        with patch.object(extract_ddi, '_load_dotenv'), patch.dict('os.environ', {
                'SILICONFLOW_API_KEY': 'test-siliconflow',
                'ZHIPU_API_KEY': 'test-zhipu', 'LLM_PROVIDER': 'siliconflow',
        }, clear=True):
            config = extract_ddi.resolve_llm_config()
            self.assertEqual('siliconflow', config['provider'])
            self.assertEqual('https://api.siliconflow.cn/v1', config['base_url'])
            # No thinking knob: the product's own resolution governs, and for
            # this provider it sends none.  The probe must agree (thinking_option).
            self.assertEqual({'max_tokens': 1024}, extract_ddi.llm_completion_options())

    def test_live_authorization_fails_closed_for_unlisted_targets(self):
        listed = {'provider': 'tokendance', 'model': 'glm-5.3-flash',
                  'base_url': 'https://tokendance.space/gateway/v1'}
        extract_ddi.assert_live_authorized(listed)  # no raise
        for tampered in (
                {**listed, 'base_url': 'https://evil.example/v1'},
                {**listed, 'model': 'glm-4.7'},
                {**listed, 'provider': 'openai_compatible'}):
            with self.assertRaisesRegex(RuntimeError, 'Live authorization does not cover'):
                extract_ddi.assert_live_authorized(tampered)

    def test_fingerprint_scopes_are_nested_and_deliberately_distinct(self):
        """The trap this guards: two entry points hashed different file sets,
        so their digests could never match and a comparison read as 'source
        changed'. Scope is now explicit, named and nested."""
        from stage0.source_fingerprint import (SCOPE_APP, SCOPE_REPO, SCOPE_RUNTIME,
                                               source_files, source_fingerprint)
        runtime = {str(p) for p in source_files(SCOPE_RUNTIME)}
        app = {str(p) for p in source_files(SCOPE_APP)}
        repo = {str(p) for p in source_files(SCOPE_REPO)}
        self.assertTrue(runtime < app < repo, 'scopes must be strictly nested')
        self.assertIn('scripts', ''.join(repo - app))       # repo adds scripts/
        self.assertIn('frontend', ''.join(app - runtime))   # app adds the frontend
        # Distinct, so a cross-scope comparison is visibly wrong rather than
        # silently plausible.
        self.assertEqual(3, len({source_fingerprint(s) for s in (SCOPE_RUNTIME, SCOPE_APP, SCOPE_REPO)}))

    def test_fingerprint_rejects_unknown_scope_and_normalises_extra(self):
        import hashlib
        from stage0.source_fingerprint import (ROOT, SCOPE_APP, source_files,
                                               source_fingerprint_map)
        with self.assertRaisesRegex(ValueError, 'unknown fingerprint scope'):
            source_files('not_a_scope')
        relative = ('requirements-harness-observability.txt',)
        absolute = (ROOT / 'requirements-harness-observability.txt',)
        self.assertEqual(source_fingerprint_map(SCOPE_APP, extra=relative),
                         source_fingerprint_map(SCOPE_APP, extra=absolute))
        entry = source_fingerprint_map(SCOPE_APP, extra=relative)['requirements-harness-observability.txt']
        self.assertEqual(hashlib.sha256((ROOT / relative[0]).read_bytes()).hexdigest(), entry)

    def test_standalone_recheck_records_terminal_status_without_refilling_budget(self):
        from stage0.turn_budget import BudgetExceeded
        with tempfile.TemporaryDirectory() as directory, MemoryStore(Path(directory)/'memory.db') as store:
            agent=MedicationCoordinatorAgent(store)
            for index, (error, expected) in enumerate([(None,'succeeded'),(BudgetExceeded('wall_clock'),'degraded'),(RuntimeError('offline failure'),'failed')]):
                with self.subTest(expected=expected), patch.object(agent,'_bounded_recheck',return_value={'checked':True},side_effect=error):
                    if error:
                        with self.assertRaises(type(error)):
                            agent._recheck_hook(store,{'id':index})
                    else:
                        self.assertEqual({'checked':True},agent._recheck_hook(store,{'id':index}))
                    row=store.connection.execute('SELECT run_id FROM workflow_runs WHERE run_id LIKE ?', (f'recheck:{index}:%',)).fetchone()
                    run=store.workflow_run_get(row[0])
                    self.assertEqual(expected,run['status'])
                    self.assertEqual(0,run['budget']['calls_attempted'])

    def test_duplicate_label_text_uses_one_extraction_input_with_source_lineage(self):
        from stage0 import ddi_engine as d
        a,b = d.normalize_medications(['甲硝唑','华法林'])
        chunks = [{'chunk_id':str(i),'drug_name':'甲硝唑片','section':'药物相互作用',
                   'text':'本品能增强华法林等抗凝药物的作用。','source_url':f'https://example.invalid/{i}'} for i in range(2)]
        with patch.object(d,'_get_retriever',return_value=NS(search=lambda *a,**k:chunks)):
            results = d._rag_candidates(a,b)
        self.assertEqual(1,len(results))
        self.assertEqual(chunks[0],results[0][0])
        self.assertEqual([{'chunk_id':'1','source_url':'https://example.invalid/1','retained_chunk_id':'0'}],
                         d.EVIDENCE_TRACE.get()['duplicate_extraction_inputs'])

    def test_budget_fallback_with_unknown_effect_keeps_grounded_warning(self):
        warning = {'drug_a':'阿司匹林','drug_b':'二甲双胍','severity':'unknown','confidence':'medium',
                   'effect':None,'citations':[{'uri':'https://example.invalid/ddi','quote':None}],
                   'audit_trail':{'warning_memory':'memory:episodic:1@v1','conclusion':'memory:conclusion:1@v1',
                                  'memory_refs':['memory:episodic:1@v1']}}
        with tempfile.TemporaryDirectory() as directory, MemoryStore(Path(directory)/'memory.db') as store:
            agent = MedicationCoordinatorAgent(store, response_provider=lambda _: '')
            state = AgentState('s','t',CareEvent('user_message','解释相互作用'))
            state.observations.append(Observation('memory_write','persist',{'operation':'consolidate_event'},
                {'consolidation':{},'recorded_warnings':[warning],'memory_refs':['memory:episodic:1@v1']}))
            state.degraded_reason = 'budget_exhausted:wall_clock'
            response = agent._respond(state)
            self.assertIn('本次处理在预算内未完成全部检查',response.text)
            self.assertIn('阿司匹林×二甲双胍（unknown/medium）：已记录警告。来源：https://example.invalid/ddi',response.text)
            self.assertIn('memory:episodic:1@v1',response.text)
            self.assertEqual([warning],response.warnings)

    def test_grounded_open_question_executes_without_null_optional_focus(self):
        with tempfile.TemporaryDirectory() as directory, MemoryStore(Path(directory) / 'memory.db') as store:
            seen = []
            agent = MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda meds: seen.append(meds) or []))
            guard = PlannerPolicyGuard(agent.executor.catalog(), medication_grounding=lambda:[{'display_name':'氨氯地平'}])
            state = AgentState('s','t',CareEvent('user_message','解释相互作用'))
            action = guard.materialize(state, {'decision':'tool','tool':'ddi_check',
                'arguments':{'medications':['伪造药名'],'focus_medication':'伪造药名'}})
            self.assertNotIn('focus_medication', action.arguments)
            result = agent.executor.execute(RunContext(run_id='t',turn_id='t'), action.tool, action.arguments, state=state)
            self.assertTrue(result.ok, result.error)
            self.assertEqual([['氨氯地平']], seen)

    def test_incomplete_extraction_is_not_negative_or_retried(self):
        for reason in ('length', 'content_filter', 'stop'):
            with self.subTest(reason=reason):
                response = NS(choices=[NS(finish_reason=reason,message=NS(tool_calls=[]))])
                with patch.object(extract_ddi, 'completion_call', return_value=response) as call:
                    with self.assertRaises(extract_ddi.ExtractionResponseError):
                        extract_ddi._production_call(None,'test','甲硝唑片','本品能增强华法林等抗凝药物的作用。')
                    self.assertEqual(1, call.call_count)

    def test_explicit_complete_empty_triples_remains_valid_negative(self):
        response = NS(choices=[NS(finish_reason='tool_calls',message=NS(tool_calls=[NS(function=NS(arguments='{"triples":[]}'))]))])
        with patch.object(extract_ddi,'completion_call',return_value=response) as call:
            self.assertEqual([],extract_ddi._production_call(None,'test','合成药','无相互作用信息'))
            self.assertEqual(1,call.call_count)

    def test_truncated_extraction_gets_parse_error_cache(self):
        from stage0 import ddi_engine as d
        a,b = d.normalize_medications(['甲硝唑','华法林'])
        chunk = {'chunk_id':'synthetic','drug_name':'甲硝唑片','text':'本品能增强华法林等抗凝药物的作用。'}
        with patch.object(d,'_cache_versions',return_value={'test':'v1'}), \
             patch.object(d,'_load_fallback_cache',return_value={'pairs':{}}), \
             patch.object(d,'_rag_candidates',return_value=[(chunk,a,b)]), \
             patch.object(d.extract_ddi,'resolve_llm_config',return_value={'model':'test'}), \
             patch.object(d.extract_ddi,'create_llm_client',return_value=None), \
             patch.object(d.extract_ddi,'_production_call',side_effect=d.extract_ddi.ExtractionResponseError('incomplete_extraction:length')), \
             patch.object(d,'_save_fallback_entry') as save:
            self.assertEqual([],d._live_fallback(a,b))
            entry = save.call_args.args[1]
            self.assertEqual('parse_error',entry['status'])
            self.assertIn('incomplete_extraction:length',entry['parse_errors'][0])
