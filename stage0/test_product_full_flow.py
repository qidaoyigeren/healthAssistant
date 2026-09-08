import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stage0.test_product_p1 import _App
from stage0.care_tasks import CareTasks
from stage0.evidence_quality import replace_source
from stage0.harness.evidence import EvidenceStore
from stage0.product import ProductError


class ProductFullFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = _App(Path(self.tmp.name))
        self.p = self.app.app.state.product

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_material_worker_task_and_summary(self):
        client = self.app.client
        case = client.post('/v1/materials/csv', json={'key': 'upload', 'text': 'name,dose,unit,schedule,date,subject\n氨氯地平,5,mg,每日一次,2026-09-08,local-demo\n'}).json()
        task = client.post('/v1/care-tasks', json={'key': 'task', 'goal_type': 'reconcile_material', 'case_id': case['id']}).json()
        payload = {'key': 'confirm', 'expected_revision': case['base_revision'], 'action': 'accept', 'task_context': {'task_id': task['id'], 'revision': task['revision']}}
        response = client.post(f"/v1/reconciliations/{case['id']}/items/{case['items'][0]['item_id']}", json=payload)
        self.assertEqual(200, response.status_code, response.text)
        after = response.json()
        rev = self.app.store.scope_revision('medications')
        self.app.worker.drain_once()
        # A safety follow-up must not reapply the imported dosage.
        self.assertEqual(rev, self.app.store.scope_revision('medications'))
        run = self.app.store.workflow_run_get(after['items'][0]['safety_check']['run_id'])
        self.assertEqual('succeeded', run['status'])
        done = CareTasks(self.p).resume(task['id'], 'resume', self.p.get(task['id'])['revision'])
        self.assertEqual('completed', done['status'])
        summary = CareTasks(self.p).create('summary', 'visit_summary')
        rendered = CareTasks(self.p).resume(summary['id'], 'render', 1)
        download = client.get(f"/v1/visit-summaries/{rendered['result_refs'][0]}/download?format=html")
        self.assertEqual(200, download.status_code)
        self.assertIn('氨氯地平', download.text)
        self.assertIn('attachment', download.headers['content-disposition'])

    def test_source_lineage_preserves_old_and_invalidates_only_dependents(self):
        evidence = EvidenceStore(self.app.store.connection, self.app.store._lock)
        old = evidence.put(content='原版明确证据', source_uri='https://example.invalid/label', corpus_version='v1')
        new = evidence.put(content='新版明确证据', source_uri='https://example.invalid/label', corpus_version='v2')
        # Real conclusion dependency uses the existing public record entrypoint.
        with self.app.store._lock, self.app.store.connection:
            cursor = self.app.store.connection.execute("INSERT INTO conclusions(session_id,turn_id,kind,text,memory_refs_json,source_refs_json,created_at,status) VALUES('s','t','ddi_warning','合成','[]',?,datetime('now'),'current')", ('[{"evidence_id":"' + old.evidence_id + '"}]',))
            cid = cursor.lastrowid
        result = replace_source(self.p, 'replace', old.evidence_id, new.evidence_id, 'label-series-1', '发布方明确的新版本替代')
        self.assertEqual([cid], result['affected_conclusions'])
        self.assertEqual('原版明确证据', evidence.read(old.evidence_id, scope_id='local-demo', limit=100)['content'])
        with self.assertRaises(ProductError):
            replace_source(self.p, 'bad', new.evidence_id, new.evidence_id, 'series', 'bad')

    def test_waiting_review_stale_facts_do_not_complete(self):
        case = self.p.import_csv('import', 'name,dose,unit,schedule,date,subject\n氨氯地平,5,mg,每日一次,2026-09-08,local-demo\n')
        case = self.p.decide(case['id'], case['items'][0]['item_id'], 'accept', case['base_revision'], 'accept', {})
        run_id = case['items'][0]['safety_check']['run_id']
        self.app.store.workflow_run_update(run_id, status='waiting_review')
        service = CareTasks(self.p)
        task = service.create('task', 'reconcile_material', case['id'])
        waiting = service.resume(task['id'], 'wait', 1)
        self.assertEqual('waiting_review', waiting['status'])
        self.app.store.apply_medication_change(action='add', name='外部更正', ingredients=[], session_id='s', turn_id='other', source='caregiver')
        stale = service.resume(task['id'], 'stale', waiting['revision'])
        self.assertEqual('waiting_input', stale['status'])
        self.assertIn('重新核对', str(stale['missing_inputs']))

    def test_cancel_owned_queued_check_keeps_committed_medication(self):
        case = self.p.import_csv('import', 'name,dose,unit,schedule,date,subject\n氨氯地平,5,mg,每日一次,2026-09-08,local-demo\n')
        tasks = CareTasks(self.p)
        task = tasks.create('task', 'reconcile_material', case['id'])
        case = self.p.decide(case['id'], case['items'][0]['item_id'], 'confirm', case['base_revision'], 'accept', {}, {'task_id': task['id'], 'revision': 1})
        tasks.resume(task['id'], 'cancel', self.p.get(task['id'])['revision'], 'cancel')
        self.app.worker.drain_once()
        run = self.app.store.workflow_run_get(case['items'][0]['safety_check']['run_id'])
        self.assertEqual('cancelled', run['status'])
        self.assertEqual(1, len(self.app.store.current_medications()))
