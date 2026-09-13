"""material-review@2 的**任务推进**回归（离线，无远程调用）。

脚本化的 ``proposal_provider`` 只提供"模型决策"这一个变量，其余（上下文准备、
校验、执行、采纳、交付检查、报告渲染、持久化）全部走真实实现。这样失败时能指出
究竟坏在哪一层，而不是笼统地"模型不行"。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage0.memory import MemoryStore  # noqa: E402
from stage0.product import MaterialIndex, ProductStore  # noqa: E402
from stage0.review.advance import GRAPH_VERSION, ReviewRunner  # noqa: E402
from stage0.review.state import RUN_ENDED, RUN_WAITING_INPUT  # noqa: E402

# 一份**有真问题**的材料：一条剂量与当前记录不一致，一条缺单位/频次。
CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
       '氨氯地平,10,mg,每日一次,2026-09-07,local-demo,口服,片,10mg\n'
       '二甲双胍,0.5,,,2026-09-01,local-demo,口服,片,0.5g\n')


class _Double:
    """脚本化的"模型"：按预设序列提交提案，除此之外不做任何判断。"""

    def __init__(self, script):
        self.script = list(script)
        self.payloads = []
        self.tool_names_seen = []

    def __call__(self, payload, timeout=None):
        self.payloads.append(payload)
        self.tool_names_seen.append([item['function']['name'] for item in payload['tool_functions']])
        step = self.script.pop(0) if self.script else {'decision': 'tool', 'tool': 'request_delivery',
                                                       'arguments': {}}
        return json.dumps(step, ensure_ascii=False)


def _transport(double):
    from stage0.agent import LLMPlanner
    return LLMPlanner(proposal_provider=double, tool_schemas={})


class ReviewFlowTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.product = ProductStore(self.memory)
        for name, dose, schedule, start in (('氨氯地平', '5mg', '每日一次', '2026-09-07'),
                                            ('二甲双胍', '0.5g', '每日两次', '2026-09-01')):
            self.memory.apply_medication_change(action='add', name=name, ingredients=[],
                                                session_id='s', turn_id='t', source='caregiver',
                                                dose=dose, schedule=schedule, route='口服',
                                                occurred_at=start)
        self.case = self.product.import_csv('k1', CSV)
        self.index = MaterialIndex(self.product)
        from stage0.harness.evidence import EvidenceStore
        self.evidence = EvidenceStore(self.memory.connection, self.memory._lock)
        self.refs = [f"{self.case['case_id']}/{item['item_id']}" for item in self.case['items']]
        self.by_name = {item['candidate']['fields']['name']: ref
                        for ref, item in zip(self.refs, self.case['items'])}

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def runner(self, script):
        double = _Double(script)
        return double, ReviewRunner(memory=self.memory, evidence_store=self.evidence,
                                    product=self.product, material_index=self.index,
                                    planner_transport=_transport(double))

    def advance(self, script, **kwargs):
        double, runner = self.runner(script)
        result = runner.advance(task_id='task:1', run_id='run:1', goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[self.case['case_id']],
                                review_index=self.index, **kwargs)
        return double, runner, result

    # -- 约束 1 / 10：普通材料核对不触发相互作用调查；确定性操作归因清楚 ----

    def test_deterministic_diff_is_done_by_code_not_by_the_model(self) -> None:
        double, _, result = self.advance([])
        review = result['review']
        self.assertTrue(review['findings'])
        self.assertTrue(all(item['origin'] == 'system' for item in review['findings']))
        # 模型一次都没有被要求重算差异：它连一条 memory_read 都没见到。
        for names in double.tool_names_seen:
            self.assertNotIn('memory_read', names)
            self.assertNotIn('list_materials', names)
            self.assertNotIn('ddi_check', names)

    def test_the_model_never_fills_in_execution_context(self) -> None:
        double, _, _ = self.advance([])
        schema_names = set()
        for payload in double.payloads:
            for item in payload['tool_functions']:
                schema_names.update(item['function']['parameters']['properties'])
        # scope / 版本 / 预算 / principal / 回执一律不在模型参数里。
        for forbidden in ('scope_id', 'principal', 'budget', 'revision', 'receipt_id',
                          'gap_id', 'expected_observation'):
            self.assertNotIn(forbidden, schema_names)

    # -- 约束 6：等待与部分交付可区分 --------------------------------------

    def test_waiting_for_input_is_partial_and_the_reason_is_specific(self) -> None:
        """等待补充时交付是**部分**，而且原因说得出是哪一条。

        报告本身是完整的（它准确地写出还剩什么没定），但用户要的那件事还没有
        交付完——"已经交付了完整的报告"和"这件事做完了"不是同一句话。
        """
        double, _, result = self.advance([])
        self.assertEqual(result['run_status'], RUN_WAITING_INPUT)
        self.assertEqual(result['delivery_status'], 'partial')
        self.assertEqual(result['report']['delivery_status'], 'partial')
        all_gaps = result['report']['all_gaps']
        self.assertTrue(any(item['code'] == 'awaiting_user_input' for item in all_gaps))
        # 反馈必须点名缺哪一项，且**只**说缺哪一项。
        self.assertTrue(all(item['detail'] and item['fix'] for item in all_gaps))
        self.assertIn('需要您补充', result['report']['markdown'])
        self.assertIn('部分结果', result['report']['cycle_note'])

    def test_material_coverage_completes_without_the_model_reading_anything(self) -> None:
        """模型一条材料都没读，覆盖照样完成，缺项照样被准确地问出来。

        这一条锁的是本轮的核心：基础覆盖**不依赖**模型愿不愿意逐条调用工具。
        """
        double, _, result = self.advance([])
        progress = result['review']['coverage_progress']
        self.assertEqual(progress['items_pending'], 0, '选中的材料必须全部有处置')
        self.assertEqual(progress['sources_read_by_system'], 2)
        both_read = sorted([self.by_name['氨氯地平'], self.by_name['二甲双胍']])
        self.assertEqual(result['review']['read_attribution']['system'], both_read)
        self.assertEqual(result['review']['material_read_refs'], [], '模型一条都没读')
        requests = result['report']['input_requests']
        self.assertTrue(requests)
        request = requests[0]
        # 请求声明了**对象**与**待补字段**：界面不需要猜，也不该由系统去猜。
        self.assertEqual(request['subjects'], ['二甲双胍'])
        self.assertIn('schedule', request['required_fields'])
        self.assertEqual(request['purpose'], 'material_note')
        self.assertEqual(request['target']['material_ref'],
                         self.by_name['二甲双胍'])
        # 归因分得清：报告写明确定性部分由代码完成，并说明模型参与了什么。
        attribution = result['report']['attribution']
        self.assertEqual(attribution['system']['materials_read'], both_read)
        self.assertEqual(attribution['model']['materials_read'], [])

    def test_an_unresolved_material_item_becomes_a_question_not_a_conclusion(self) -> None:
        _, _, result = self.advance([])
        review = result['review']
        questions = [item for item in review['questions'] if item['origin'] == 'system']
        self.assertTrue(questions)
        # 它出现在"待确认"一节，而不是被写成一致项。
        self.assertIn('待核实', result['report']['markdown'])
        self.assertNotIn('未命名条目：与当前记录一致', result['report']['markdown'])

    # -- 约束 3 / 8：交付只在要求满足时通过 ---------------------------------

    def test_read_back_material_then_submit_a_grounded_assertion(self) -> None:
        flow = self.by_name['氨氯地平']
        _, _, result = self.advance([
            {'decision': 'tool', 'tool': 'read_material', 'arguments': {'material_ref': flow}},
            {'decision': 'tool', 'tool': 'submit_assertion',
             'arguments': {'predicate': 'record_consistency', 'value': {'expect': 'different'},
                           'subject_refs': ['氨氯地平'], 'qualifiers': {'material_refs': [flow]},
                           'purpose': '材料与当前记录的剂量不一致'}},
            {'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}},
        ])
        assertions = result['review']['assertions']
        self.assertTrue(assertions)
        self.assertEqual(assertions[0]['verification_status'], 'supported')

    def test_a_citation_needs_a_verified_read_not_a_tool_call(self) -> None:
        """引用材料的前提是它**被有效读过**，不是"模型调用过 read_material"。

        覆盖 pass 在同样的权限与完整性校验下读过的条目同样算数——它比较的就是那些
        读进来的字段。所以这条断言可以做，而它确实与记录不一致。与此同时，归因必须
        分得清：读到这条的是**系统**，模型的读取记录是空的。
        """
        flow = self.by_name['氨氯地平']
        double, _, result = self.advance([
            {'decision': 'tool', 'tool': 'submit_assertion',
             'arguments': {'predicate': 'record_consistency', 'value': {'expect': 'different'},
                           'subject_refs': ['氨氯地平'], 'qualifiers': {'material_refs': [flow]}}},
        ])
        assertions = result['review']['assertions']
        self.assertTrue(assertions)
        self.assertEqual(assertions[0]['verification_status'], 'supported')
        self.assertIn(flow, result['review']['read_attribution']['system'])
        self.assertEqual(result['review']['read_attribution']['model'], [])
        self.assertEqual(result['review']['material_read_refs'], [])

    def test_a_citation_outside_the_selected_scope_is_refused(self) -> None:
        """没读过的引用仍然被拒——索引里出现过不等于读过原文。"""
        double, _, result = self.advance([
            {'decision': 'tool', 'tool': 'submit_assertion',
             'arguments': {'predicate': 'record_consistency', 'value': {'expect': 'different'},
                           'subject_refs': ['氨氯地平'],
                           'qualifiers': {'material_refs': ['case:no-such/material-item']}}},
        ])
        self.assertEqual(result['review']['assertions'], [])
        rejected = [entry for entry in result['trace'] if entry.get('rejected')]
        self.assertTrue(rejected)
        self.assertIn('material_not_in_scope', rejected[0]['rejected'][0])

    def test_a_structured_assertion_that_contradicts_the_diff_is_not_supported(self) -> None:
        """材料剂量 10mg 与记录 5mg 不同；说它们"一致"必须被否决。"""
        flow = self.by_name['氨氯地平']
        _, _, result = self.advance([
            {'decision': 'tool', 'tool': 'read_material', 'arguments': {'material_ref': flow}},
            {'decision': 'tool', 'tool': 'submit_assertion',
             'arguments': {'predicate': 'record_consistency', 'value': {'expect': 'same'},
                           'subject_refs': ['氨氯地平'], 'qualifiers': {'material_refs': [flow]}}},
        ])
        assertion = result['review']['assertions'][0]
        self.assertEqual(assertion['verification_status'], 'contradicted')
        # 被反驳的说法不能从报告里消失。
        self.assertIn('现有依据不支持', result['report']['markdown'])

    # -- 约束 7 / 增量：请求补充后只处理受影响部分 ---------------------------

    def test_request_information_pauses_the_run(self) -> None:
        flow = self.by_name['二甲双胍']
        _, _, result = self.advance([
            {'decision': 'tool', 'tool': 'read_material', 'arguments': {'material_ref': flow}},
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '二甲双胍的剂量单位和服用频次是多少？',
                           'subjects': ['二甲双胍'], 'required_fields': ['unit', 'schedule'],
                           'why_needed': '材料缺少这两项，无法判断是否与当前记录一致'}},
        ])
        self.assertEqual(result['run_status'], RUN_WAITING_INPUT)
        self.assertTrue(result['report']['input_requests'])
        self.assertIn('需要您补充', result['report']['markdown'])
        # 等待补充时仍然交付一份部分报告，且部分报告里的内容依然准确。
        self.assertEqual(result['delivery_status'], 'partial')

    def test_repeating_the_same_request_does_not_create_a_second_one(self) -> None:
        """系统已经问过的缺项，模型换个措辞问还是**同一条**请求。

        这一条锁的是"补问创建之后没有清楚的等待边界、模型继续重复提问"：不仅请求
        没有变成两条，模型在这一次推进里也**只被调用了一次**——没有新决策可做时
        循环就停下来了。
        """
        flow = self.by_name['二甲双胍']
        double, _, result = self.advance([
            {'decision': 'tool', 'tool': 'read_material', 'arguments': {'material_ref': flow}},
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '二甲双胍一天吃几次、每次多少？',
                           'subjects': ['二甲双胍'], 'required_fields': ['unit', 'schedule']}},
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '请问二甲双胍的剂量单位和频次是多少？',
                           'subjects': ['二甲双胍'], 'required_fields': ['unit', 'schedule']}},
        ])
        requests = result['review']['input_requests']
        self.assertEqual(len(requests), 1, '换个措辞不该变成第二条请求')
        self.assertEqual(sorted(requests[0]['required_fields']), ['schedule', 'unit'])
        self.assertTrue(result['trace'][-1].get('input_request_reused'))
        self.assertEqual(result['run_status'], RUN_WAITING_INPUT)
        self.assertEqual(len(double.payloads), 2, '问过之后就不再调用模型了')

    def test_the_same_object_with_a_disjoint_field_is_a_separate_request(self) -> None:
        """同一对象的**不相交**字段不能被合并——合并了，另一个字段永远等不到答案。"""
        flow = self.by_name['氨氯地平']
        _, runner, first = self.advance([
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '氨氯地平的剂量是多少？',
                           'subjects': ['氨氯地平'], 'required_fields': ['dose']}},
        ])
        _, runner2 = self.runner([
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '氨氯地平的服用频次是多少？',
                           'subjects': ['氨氯地平'], 'required_fields': ['schedule']}}])
        second = runner2.advance(task_id='task:1', run_id='run:2', goal='核对材料与当前记录',
                                 scope_id='local-demo', selected_case_ids=[self.case['case_id']],
                                 review_index=self.index, initial_state=first['review'])
        requests = [item for item in second['review']['input_requests']
                    if item['subjects'] == ['氨氯地平']]
        self.assertEqual(len(requests), 2)
        self.assertEqual(sorted(item['required_fields'][0] for item in requests),
                         ['dose', 'schedule'])

    def test_a_request_without_a_stated_object_is_refused_with_a_reason(self) -> None:
        """说不清对象的补充请求被明确拒绝，而不是留下一个无法恢复的阻塞请求。

        系统**不**去推测这条补充应该写进哪条患者记录。
        """
        double, _, result = self.advance([
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '请补充一下那条记录的剂量。'}},
        ])
        model_requests = [item for item in result['review']['input_requests']
                          if item['origin'] == 'model']
        self.assertEqual(model_requests, [])
        rejected = [entry for entry in result['trace'] if entry.get('rejected')]
        self.assertTrue(rejected)
        self.assertIn('missing_argument:subjects', rejected[0]['rejected'])

    # -- 恢复：不重复副作用，不丢证据 ---------------------------------------

    def test_resume_does_not_repeat_side_effects(self) -> None:
        flow = self.by_name['氨氯地平']
        _, runner, first = self.advance([
            {'decision': 'tool', 'tool': 'read_material', 'arguments': {'material_ref': flow}},
            {'decision': 'tool', 'tool': 'submit_finding',
             'arguments': {'finding_type': 'discrepancy', 'statement': '剂量不一致（人工核对）',
                           'material_refs': [flow], 'subject_refs': ['氨氯地平']}},
        ])
        before = len(first['review']['findings'])
        _, runner2 = self.runner([{'decision': 'tool', 'tool': 'request_delivery',
                                   'arguments': {}}])
        second = runner2.advance(task_id='task:1', run_id='run:2', goal='核对材料与当前记录',
                                 scope_id='local-demo', selected_case_ids=[self.case['case_id']],
                                 review_index=self.index, initial_state=first['review'])
        self.assertEqual(len(second['review']['findings']), before)
        self.assertEqual([item['finding_id'] for item in second['review']['findings']],
                         [item['finding_id'] for item in first['review']['findings']])

    # -- 约束 8：取消不重复副作用、不丢已有结果 ------------------------------

    def test_cancellation_keeps_the_partial_result(self) -> None:
        from stage0.harness.progress import cancel_event_for
        _, runner = self.runner([{'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}])
        cancel_event_for('run:cancel').set()
        result = runner.advance(task_id='task:c', run_id='run:cancel',
                                goal='核对材料与当前记录', scope_id='local-demo',
                                selected_case_ids=[self.case['case_id']],
                                review_index=self.index)
        self.assertEqual(result['run_status'], 'cancelled')
        self.assertTrue(result['review']['findings'])
        self.assertIn('材料核对与就诊准备报告', result['report']['markdown'])

    # -- 新版本不覆盖旧报告 --------------------------------------------------

    def test_a_new_run_publishes_a_new_report_version(self) -> None:
        _, runner, first = self.advance([])
        second = runner.advance(task_id='task:1', run_id='run:2', goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[self.case['case_id']],
                                review_index=self.index, initial_state=first['review'])
        reports = second['review']['reports']
        self.assertGreaterEqual(len(reports), 1)
        self.assertNotEqual(reports[-1]['report_id'], first['report']['report_id'])


if __name__ == '__main__':
    unittest.main()
