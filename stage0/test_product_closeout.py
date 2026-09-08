"""Product P0/P1 closeout regression tests; isolated synthetic stores only."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from stage0.test_product_p1 import (_App, _seed_pair_state, ADD_AMLODIPINE,
                                  DOSE_CHANGE_AMLODIPINE)
from stage0.harness.evidence import EvidenceStore, capture_from_rag_result, capture_from_ddi_warnings
from stage0.product_evals import run_eval


class CloseoutTests(unittest.TestCase):
    def test_graph_runner_publishes_bundle_and_attributed_impact(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AGENT_GRAPH_RUNNER': '1'}):
            api = _App(Path(directory))
            try:
                _seed_pair_state(api)
                result = api.commit_event('graph-correction', DOSE_CHANGE_AMLODIPINE)
                self.assertEqual(result['response']['answer_bundle']['bundle_version'], 'answer-bundle@1')
                impact = api.client.get('/v1/change-impact', params={'run_id': result['run_id']})
                self.assertEqual(impact.status_code, 200, impact.text)
                self.assertTrue(impact.json()['affected_conclusions'])
            finally:
                api.close()

    def test_alert_detail_does_not_verify_tampered_or_foreign_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                payload = _seed_pair_state(api)
                warning = payload['response']['warnings'][0]
                evidence_id = warning['citations'][0]['evidence_id']
                alert_id = int(warning['audit_trail']['conclusion'].split(':')[-1].split('@')[0])
                for column, value in [('content', 'tampered'), ('scope_id', 'foreign')]:
                    with api.store.connection:
                        api.store.connection.execute(
                            'UPDATE evidence_records SET content=?,scope_id=? WHERE evidence_id=?',
                            (warning['source_text'], 'local-demo', evidence_id))
                        api.store.connection.execute(
                            f'UPDATE evidence_records SET {column}=? WHERE evidence_id=?',
                            (value, evidence_id))
                    detail = api.client.get(f'/v1/alert-records/{alert_id}').json()
                    self.assertEqual(detail['evidence_refs'][0]['status'], 'unavailable')
                    self.assertNotEqual(detail['evidence_refs'][0].get('integrity'), 'verified')
                    self.assertNotIn('meta', detail['evidence_refs'][0])
                    self.assertFalse(detail['evidence_available'])
            finally:
                api.close()

    def test_evidence_metadata_preserves_content_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                rec = evidence.put(content='synthetic', source_uri=None, content_ref='rag:chunk-7')
                body = api.client.get(f'/v1/evidence/{rec.evidence_id}').json()
                self.assertEqual(body['source']['content_ref'], 'rag:chunk-7')
            finally:
                api.close()

    def test_since_excludes_old_invalidations_and_rejects_bad_time(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                _seed_pair_state(api)
                api.commit_event('correction', DOSE_CHANGE_AMLODIPINE)
                body = api.client.get('/v1/change-impact', params={'since': '2099-01-01T00:00:00Z'}).json()
                self.assertEqual(body['affected_conclusions'], [])
                self.assertEqual(api.client.get('/v1/change-impact?since=garbage').status_code, 422)
            finally:
                api.close()

    def test_run_impact_receipt_replays_without_later_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                _seed_pair_state(api)
                payload = api.commit_event('correction', DOSE_CHANGE_AMLODIPINE)
                run_id = payload['run_id']
                body = api.client.get('/v1/change-impact', params={'run_id': run_id}).json()
                self.assertEqual(body['attribution'], 'run_audit')
                self.assertEqual(body['run_id'], run_id)
                affected = {item['conclusion_id'] for item in body['affected_conclusions']}
                self.assertTrue(affected)
                self.assertTrue(body['changed_facts'])
                self.assertTrue(all(item['target_type'] in {'semantic', 'medication'} for item in body['changed_facts']))
                api.commit_event('later', {**DOSE_CHANGE_AMLODIPINE,
                                          'payload': {**DOSE_CHANGE_AMLODIPINE['payload'], 'dose': '15mg'}})
                replay = api.client.get('/v1/change-impact', params={'run_id': run_id}).json()
                self.assertEqual(affected, {item['conclusion_id'] for item in replay['affected_conclusions']})
                self.assertEqual(body['changed_facts'], replay['changed_facts'])
                unknown = api.client.get('/v1/change-impact?run_id=unknown')
                self.assertEqual(unknown.status_code, 404)
            finally:
                api.close()

    def test_empty_held_out_and_phase_filter_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'held_out').mkdir()
            report = run_eval.run_suite('held_out', tasks_dir=root)
            self.assertEqual(report['overall'], 'unavailable')
        self.assertEqual(run_eval.run_suite('dev', phases=['nonexistent'])['overall'], 'unavailable')

    def test_capture_does_not_confuse_mode_with_corpus_version(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                rag = capture_from_rag_result(evidence, {'mode': 'hybrid', 'results': [
                    {'text': 'synthetic-rag', 'drug_name': 'A', 'section': 'test'}]},
                    run_id='r', query='A', patient_revision=1)
                ddi = capture_from_ddi_warnings(evidence, {'warnings': [
                    {'source_text': 'synthetic-ddi', 'drug_a': 'A', 'drug_b': 'B',
                     'detection_path': 'kegg+rag+llm'}]}, run_id='r', patient_revision=1)
                for item in rag + ddi:
                    self.assertIsNone(evidence.get_meta(item['evidence_id'])['corpus_version'])
            finally:
                api.close()

    def test_browser_gate_rejects_empty_or_incomplete_result(self):
        from stage0.run_product_acceptance import parse_browser, REQUIRED_BROWSER
        with self.assertRaises(ValueError):
            parse_browser('### Result\n' + json.dumps({'checks': {}}))
        body = {'checks': dict.fromkeys(REQUIRED_BROWSER, True), 'errors': [], 'serverErrors': []}
        self.assertEqual(parse_browser('### Result\n' + json.dumps(body)), body)
        with self.assertRaises(ValueError):
            parse_browser('### Error\nfailed\n### Result\n' + json.dumps(body))

    def test_legacy_run_without_attribution_is_not_zero_impact(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                payload = api.commit_event('add', ADD_AMLODIPINE)
                run_id = payload['run_id']
                with api.store.connection:
                    api.store.connection.execute(
                        "UPDATE operation_receipts SET result_json=json_remove(result_json,'$.audit_attribution_version') "
                        "WHERE run_id=?", (run_id,))
                self.assertEqual(api.client.get('/v1/change-impact', params={'run_id': run_id}).status_code, 409)
            finally:
                api.close()

    def test_eval_rejects_wrong_content_and_error_content_leak(self):
        fixture = Mock()
        fixture.resolve_ref.side_effect = lambda value: value
        fixture.seeded_evidence = [{'content': 'truth'}]
        response = Mock(status_code=200)
        response.json.return_value = {'evidence_id': 'ev-1', 'content': 'wrong',
                                      'offset': 0, 'returned_chars': 5, 'total_chars': 5}
        fixture.client.get.return_value = response
        fixture.evidence.get_meta.return_value = {'content_hash': run_eval.content_hash('truth')}
        with self.assertRaises(AssertionError):
            run_eval._exec_read_page(fixture, {'evidence_id': 'ev-1'}, {'content_matches_hash': True})
        response.status_code = 422
        response.json.return_value = {'error': {'code': 'invalid_arguments', 'details': {'content': 'secret'}}}
        with self.assertRaises(AssertionError):
            run_eval._exec_api_status(fixture, {'evidence_id': 'ev-1'}, {'error_kind': 'invalid_arguments'})


if __name__ == '__main__':
    unittest.main()
