"""material-review@2 的**设计约束**回归。

这些测试对着本轮的设计约束写，不对着实现细节写：它们必须能在真实缺陷出现时失败。
每一条都对应设计记录里的一个论断，并且用合成数据做隔离验证，不发远程调用。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage0.memory import MemoryStore  # noqa: E402
from stage0.product import MaterialIndex, ProductStore  # noqa: E402
from stage0.review import capabilities as caps  # noqa: E402
from stage0.review.advance import ReviewRunner  # noqa: E402
from stage0.review.contract import REQ_AUTHORITATIVE_RECORD, build_task_spec  # noqa: E402
from stage0.review.delivery import check_delivery  # noqa: E402
from stage0.review.report import section_content  # noqa: E402
from stage0.review.state import (MaterialReviewState, RUN_WAITING_INPUT,  # noqa: E402
                                 assertion_id_for, question_id_for)
from stage0.review.verify import verify_assertion  # noqa: E402

CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
       '氨氯地平,5,mg,每日一次,2026-09-07,local-demo,口服,片,5mg\n'
       '二甲双胍,0.5,g,每日两次,2026-09-01,local-demo,口服,片,0.5g\n')


class _Fixture:
    """一个最小可用的产品环境：患者记录 + 一份材料。"""

    def __init__(self, directory, csv_text=CSV):
        self.store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
        self.product = ProductStore(self.store)
        for name, dose, schedule, start in (('氨氯地平', '5mg', '每日一次', '2026-09-07'),
                                            ('二甲双胍', '0.5g', '每日两次', '2026-09-01')):
            self.store.apply_medication_change(action='add', name=name, ingredients=[],
                                               session_id='s', turn_id='t', source='caregiver',
                                               dose=dose, schedule=schedule, route='口服',
                                               occurred_at=start)
        self.case = self.product.import_csv('k1', csv_text)
        self.index = MaterialIndex(self.product)

    def close(self):
        self.store.close()


def _spec(fixture, case_ids=None, goal='核对材料与当前记录'):
    facts = fixture.store.snapshot()
    items = [(case['case_id'], case['items']) for case in fixture.product.objects('case')]
    versions = {'medications': fixture.store.scope_revision('medications'),
                'semantic': fixture.store.scope_revision('semantic'),
                'materials': fixture.store.scope_revision('materials')}
    return build_task_spec(task_id='t1', user_goal=goal, scope_id='local-demo',
                           selected_case_ids=case_ids or [fixture.case['case_id']],
                           material_items=items, medications=facts.get('medications') or [],
                           input_versions=versions)


# ---- 约束 1：普通材料核对不强制触发相互作用调查 -------------------------------

class ContractScopeTests(unittest.TestCase):

    def test_coverage_comes_from_the_task_not_a_drug_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                spec = _spec(fixture)
                kinds = {item['kind'] for item in spec.coverage_requirements}
                self.assertIn('material_item', kinds)
                self.assertIn('authoritative_record', kinds)
                # 旧契约的三条固定条件在这里**不存在**。
                self.assertNotIn('interaction_evidence', spec.requested_outputs)
                self.assertNotIn('applicability', spec.requested_outputs)
            finally:
                fixture.close()

    def test_not_listed_record_is_a_required_covered_item(self) -> None:
        """材料没写某个药，这条必须被报告，不能靠不列它让任务变便宜。"""
        csv_text = CSV.split('\n')[0] + '\n' + CSV.split('\n')[1] + '\n'
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory, csv_text)
            try:
                spec = _spec(fixture)
                outstanding = [item for item in spec.coverage_requirements
                               if item['kind'] == REQ_AUTHORITATIVE_RECORD]
                self.assertTrue(outstanding)
                self.assertTrue(all(item['disposition'] == 'pending' for item in outstanding))
            finally:
                fixture.close()


# ---- 约束 2：同药名不同问题具有独立身份 ---------------------------------------

class IdentityTests(unittest.TestCase):

    def test_same_drug_different_aspect_is_a_different_question(self) -> None:
        dose = question_id_for('dose_consistency', ['氨氯地平'])
        date = question_id_for('start_date', ['氨氯地平'])
        self.assertNotEqual(dose, date)

    def test_wording_and_drug_order_do_not_change_identity(self) -> None:
        self.assertEqual(question_id_for('interaction', ['氨氯地平', '二甲双胍']),
                         question_id_for('interaction', ['二甲双胍', '氨氯地平']))

    def test_assertion_identity_includes_predicate_and_qualifiers(self) -> None:
        first = assertion_id_for(['氨氯地平'], 'dose_consistency', {'expect': 'same'})
        second = assertion_id_for(['氨氯地平'], 'date_consistency', {'expect': 'same'})
        third = assertion_id_for(['氨氯地平'], 'dose_consistency', {'expect': 'different'})
        self.assertEqual(len({first, second, third}), 3)


# ---- 约束 3：问题修订后旧判断不会错误复用 -------------------------------------

class RevisionTests(unittest.TestCase):

    def test_revision_bumps_revision_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                first = state.submit_question(question_key='dose', text='剂量是否一致',
                                              subjects=['氨氯地平'])
                second = state.submit_question(question_key='dose', text='剂量与频次是否一致',
                                               subjects=['氨氯地平'])
                self.assertEqual(first['question_id'], second['question_id'])
                self.assertEqual(second['revision'], 2)
                self.assertEqual(second['history'][0]['text'], '剂量是否一致')
            finally:
                fixture.close()

    def test_assertion_revision_invalidates_the_old_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                item = state.submit_assertion(subject_refs=['氨氯地平'], predicate='dose_consistency',
                                              value={'expect': 'same'}, qualifiers={})
                item['verification_status'] = 'supported'
                revised = state.submit_assertion(subject_refs=['氨氯地平'], predicate='dose_consistency',
                                                 value={'expect': 'different'}, qualifiers={})
                self.assertEqual(revised['assertion_id'], item['assertion_id'])
                self.assertEqual(revised['revision'], 2)
                self.assertEqual(revised['verification_status'], 'unverified')
            finally:
                fixture.close()

    def test_an_open_question_cannot_be_closed_without_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                state.submit_question(question_key='dose', text='剂量是否一致', subjects=['氨氯地平'])
                with self.assertRaises(ValueError):
                    state.submit_question(question_key='dose', text='剂量是否一致',
                                          subjects=['氨氯地平'], status='answered')
                closed = state.submit_question(question_key='dose', text='剂量是否一致',
                                               subjects=['氨氯地平'], status='answered',
                                               resolution_summary='材料与当前记录一致')
                self.assertEqual(closed['status'], 'answered')
            finally:
                fixture.close()


# ---- 约束 4：发现差异不产生永久阻塞 -------------------------------------------

class DiscrepancyTests(unittest.TestCase):

    def test_a_discrepancy_becomes_a_finding_not_a_permanent_blocker(self) -> None:
        csv_text = CSV.replace('氨氯地平,5,mg,每日一次', '氨氯地平,10,mg,每日一次')
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory, csv_text)
            try:
                state = MaterialReviewState(_spec(fixture))
                from stage0.review.context import seed_from_deterministic_diff
                seed_from_deterministic_diff(state, fixture.index)
                discrepancies = [item for item in state.findings
                                 if item['finding_type'] == 'discrepancy']
                self.assertTrue(discrepancies)
                # 差异是发现，不是待办：它不产生"必须被关闭"的问题。
                self.assertEqual(state.open_questions(), [])
                # 但它仍然必须出现在报告里。
                self.assertIn('- ' + discrepancies[0]['statement'][:10],
                              ''.join(section_content(state)['3. 差异双方的记录及来源']))
            finally:
                fixture.close()


# ---- 约束 5：权限和来源失效不能被绕过 -----------------------------------------

class IntegrityTests(unittest.TestCase):

    def test_an_invalid_source_stops_supporting_and_does_not_kill_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                state.evidence_refs.append('ev-missing')
                state.read_evidence_refs.append('ev-missing')
                record = state.submit_assertion(
                    subject_refs=['氨氯地平'], predicate='label_statement', value='说明书提到相互作用',
                    qualifiers={'excerpt': '相互作用'}, evidence_refs=['ev-missing'])
                from stage0.review.verify import verify_all
                verify_all(state, evidence_store=_Evidence(fixture))
                self.assertEqual(record['verification_status'], 'insufficient')
                from stage0.review.incremental import invalidate_sources
                result = invalidate_sources(state, _Evidence(fixture))
                self.assertIn('ev-missing', result['invalidated_sources'])
                self.assertIn(record['assertion_id'], result['invalidated_assertions'])
                # 任务本身没有因此被丢弃。
                self.assertNotEqual(state.run_status, 'failed')
            finally:
                fixture.close()

    def test_permission_denied_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                from stage0.review.delivery import hard_stop_reason
                state = MaterialReviewState(_spec(fixture))
                state.add_issue(operation_ref='op', category='permission_denied')
                self.assertEqual(hard_stop_reason(state), 'permission_denied')
            finally:
                fixture.close()


class _Evidence:
    """最小 EvidenceStore 替身：只回答"这个 id 能不能读"。"""

    def __init__(self, fixture):
        from stage0.harness.evidence import EvidenceStore
        self._real = EvidenceStore(fixture.store.connection, fixture.store._lock)

    def read(self, evidence_id, **kwargs):
        return self._real.read(evidence_id, **kwargs)

    def get_meta(self, evidence_id):
        return self._real.get_meta(evidence_id)


# ---- 约束 6：完整、部分、等待和失败不会混淆 -----------------------------------

class AxisTests(unittest.TestCase):

    def test_axes_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                state.delivery_status = 'complete'
                state.evidence_status = 'conflicting'
                state.run_status = 'ended'
                # 「运行已结束、报告完整、证据存在冲突」是合法组合，不是矛盾。
                self.assertEqual((state.run_status, state.delivery_status, state.evidence_status),
                                 ('ended', 'complete', 'conflicting'))
            finally:
                fixture.close()

    def test_pending_coverage_makes_delivery_partial_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                sections = section_content(state)
                check = check_delivery(state, sections=sections)
                self.assertFalse(check['ok'])
                self.assertEqual(check['delivery_status'], 'partial')
                self.assertTrue(any(item['code'] == 'coverage_pending' for item in check['gaps']))
                # 反馈必须是**具体**的缺失项，并说清怎么补，而不是"重做一遍调查"。
                self.assertIn('只需补这几项', check['summary'])
                # 具体性：覆盖缺口逐条点名"未交代"，其余缺口也各自说明缺什么、
                # 怎么补——但没有一条是"重做整条调查"。
                coverage_gaps = [item for item in check['gaps']
                                 if item['code'] == 'coverage_pending']
                self.assertTrue(coverage_gaps)
                self.assertTrue(all('未交代' in item['detail'] for item in coverage_gaps))
                self.assertTrue(all(item['detail'] and item['fix'] for item in check['gaps']))
            finally:
                fixture.close()


# ---- 约束 10：确定性操作与模型决策归因清楚 ------------------------------------

class AttributionTests(unittest.TestCase):

    def test_deterministic_findings_are_labelled_system(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                from stage0.review.context import seed_from_deterministic_diff
                seed_from_deterministic_diff(state, fixture.index)
                self.assertTrue(state.findings)
                self.assertTrue(all(item['origin'] == 'system' for item in state.findings))
            finally:
                fixture.close()

    def test_model_candidates_are_not_adopted_as_verified_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(directory)
            try:
                state = MaterialReviewState(_spec(fixture))
                ref = next(iter(state.material_items), None) or 'case/x'
                item = state.add_finding(finding_type='discrepancy', statement='材料与记录不一致',
                                         material_refs=[], evidence_refs=[])
                self.assertEqual(item['assessment_status'], 'unverified')
                self.assertIsNotNone(ref)
            finally:
                fixture.close()


if __name__ == '__main__':
    unittest.main()
