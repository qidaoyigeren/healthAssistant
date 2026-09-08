import unittest
from stage0 import test_product_reconciliation as support
from stage0.care_tasks import CareTasks
from stage0.product import ProductError


class CareTaskTests(unittest.TestCase):
    setUp = support.ReconciliationTests.setUp
    tearDown = support.ReconciliationTests.tearDown
    import_case = support.ReconciliationTests.import_case
    accept = support.ReconciliationTests.accept
    def test_resume_missing_input_and_complete(self):
        case = self.import_case()
        service = CareTasks(self.product)
        task = service.create('task-create', 'reconcile_material', case['id'])
        waiting = service.resume(task['id'], 'resume1', task['revision'])
        self.assertEqual('waiting_input', waiting['status'])
        self.assertEqual(waiting, service.resume(task['id'], 'resume1', task['revision']))
        with self.assertRaises(ProductError):
            service.resume(task['id'], 'race', task['revision'])
        updated = self.accept(case)
        accepted = self.accept(updated, 1, 'row2')
        # This isolated contract test supplies the existing worker's terminal
        # states; the API integration test exercises the actual worker.
        for item in accepted['items']:
            if item.get('safety_check'):
                self.memory.workflow_run_update(item['safety_check']['run_id'], status='succeeded')
        task = self.product.get(task['id'])
        done = service.resume(task['id'], 'resume2', task['revision'])
        self.assertEqual('completed', done['status'])
        self.assertEqual(4, done['budget']['spent'])
        self.assertEqual(300000, done['resource_budget']['tokens_reserved'])

    def test_budget_cancel_and_no_forced_completion(self):
        case = self.import_case()
        service = CareTasks(self.product)
        task = service.create('budget-create', 'reconcile_material', case['id'], budget=1)
        with self.assertRaises(ProductError):
            service.resume(task['id'], 'bad', 1, 'completed')
        waiting = service.resume(task['id'], 'one', 1)
        failed = service.resume(task['id'], 'two', waiting['revision'])
        self.assertEqual('failed', failed['status'])
        self.assertEqual(1, failed['budget']['spent'])
        self.accept(case)
        cancelled = service.resume(task['id'], 'cancel', failed['revision'], 'cancel')
        self.assertEqual('cancelled', cancelled['status'])
        self.assertEqual(1, len(self.memory.current_medications()))

    def test_immutable_summary_and_audited_snapshot(self):
        service = CareTasks(self.product)
        task = service.create('summary', 'visit_summary')
        done = service.resume(task['id'], 'render', 1)
        artifact = self.product.get(done['result_refs'][0], 'summary')
        self.accept(self.import_case())
        self.assertNotEqual(artifact['patient_revision'], self.product.revisions())
        self.assertEqual(artifact, self.product.get(artifact['id'], 'summary'))
        query = service.create('query', 'current_medications')
        snapshot_task = service.resume(query['id'], 'read', 1)
        snapshot = self.product.get(snapshot_task['result_refs'][0])
        self.assertEqual(1, len(snapshot['medications']))

    def test_task_budget_cannot_be_bypassed_by_detaching_a_supplement(self):
        case = self.import_case()
        task = CareTasks(self.product).create('task', 'reconcile_material', case['id'])
        with self.assertRaises(ProductError):
            self.product.decide(case['id'], case['items'][0]['item_id'], 'detached', case['base_revision'], 'accept', {})
        with self.product.transaction():
            task['resource_budget']['tokens_reserved'] = task['resource_budget']['token_limit']
            self.product.save('care_task', task)
        with self.assertRaises(ProductError):
            self.accept(case)
        self.assertEqual([], self.memory.current_medications())
        self.assertEqual(0, self.product.get(task['id'])['budget']['spent'])
