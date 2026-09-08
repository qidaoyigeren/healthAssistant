import tempfile
import unittest
from pathlib import Path

from stage0.memory import MemoryStore
from stage0.product import ProductStore, ProductError


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'memory.db'
        self.memory = MemoryStore(self.path, llm_enabled=False)
        self.product = ProductStore(self.memory)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def import_case(self, key='import-1', rows=None):
        return self.product.import_csv(key, rows or 'name,dose,unit,schedule,date,subject\n氨氯地平,5,mg,每日一次,2026-09-07,local-demo\n阿司匹林,100,mg,每日一次,2026-09-07,local-demo\n')

    def accept(self, case, index=0, key='accept-1'):
        active = next((t for t in self.product.objects('care_task') if t.get('case_id') == case['id'] and t['status'] not in ('completed', 'cancelled', 'failed')), None)
        return self.product.decide(case['case_id'], case['items'][index]['item_id'], key,
                                   case['base_revision'], 'accept', {}, {'task_id': active['id'], 'revision': active['revision']} if active else None)

    def test_candidates_and_independent_import_identity(self):
        a, b = self.import_case(), self.import_case('import-2')
        self.assertEqual([], self.memory.current_medications())
        self.assertNotEqual(a['case_id'], b['case_id'])
        self.assertEqual(a['document_id'], b['document_id'])
        self.assertEqual(a['case_id'], self.import_case()['case_id'])
        with self.assertRaises(ProductError):
            self.product.import_csv('import-1', 'different')

    def test_two_rows_restart_receipt_and_payload_conflict(self):
        case = self.import_case()
        result = self.accept(case)
        self.assertGreater(result['base_revision']['medications'], case['base_revision']['medications'])
        self.memory.close()
        self.memory = MemoryStore(self.path, llm_enabled=False)
        self.product = ProductStore(self.memory)
        self.assertEqual(result, self.accept(case))
        with self.assertRaises(ProductError):
            self.product.decide(case['case_id'], case['items'][0]['item_id'], 'accept-1', case['base_revision'], 'keep', {})
        updated = self.product.case(case['case_id'])
        self.accept(updated, 1, 'accept-2')
        self.assertEqual(2, len(self.memory.current_medications()))
        self.assertEqual('completed', self.product.case(case['case_id'])['status'])

    def test_external_revision_requires_refresh_and_review(self):
        case = self.import_case()
        self.memory.apply_medication_change(action='add', name='外部记录', ingredients=[], session_id='other', turn_id='other', source='caregiver')
        with self.assertRaises(ProductError) as err:
            self.accept(case)
        self.assertEqual(409, err.exception.status)
        self.assertEqual(1, len(self.memory.current_medications()))
        refreshed = self.product.refresh(case['case_id'])
        self.assertTrue(any(i['kind'] == 'not_listed' for i in refreshed['items']))
        self.accept(refreshed)

    def test_invalid_fields_and_location_never_project(self):
        case = self.import_case(rows='name,dose,unit,schedule,date,subject\n不明药,0.?,,每日一次,bad,另一个人\n')
        self.assertEqual('unresolved', case['items'][0]['kind'])
        self.assertEqual(2, case['items'][0]['candidate']['locations']['name']['line'])
        with self.assertRaises(ProductError):
            self.accept(case)
        self.assertEqual([], self.memory.current_medications())

    def test_fault_rolls_back_domain_and_receipt(self):
        case = self.import_case()
        self.product.fault_hook = lambda: (_ for _ in ()).throw(RuntimeError('crash after projection'))
        with self.assertRaises(RuntimeError):
            self.accept(case)
        self.assertEqual([], self.memory.current_medications())
        self.product.fault_hook = None
        self.accept(case)
        self.assertEqual(1, len(self.memory.current_medications()))


if __name__ == '__main__':
    unittest.main()
