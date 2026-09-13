"""material-review@2 走**正常产品入口**的闭环（离线，无远程调用）。

覆盖：上传材料 → 核对报告 → 查看来源 → 补充信息 → 报告修订及变化原因。
用的入口是 ``CareTasks``（前端调的那一个）与 worker 发布路径，不是测试专用捷径。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage0.agent import MedicationCoordinatorAgent  # noqa: E402
from stage0.care_tasks import REVIEW_CONTRACT, CareTasks  # noqa: E402
from stage0.memory import MemoryStore  # noqa: E402
from stage0.product import ProductStore  # noqa: E402

# 材料缺二甲双胍的单位与频次；氨氯地平剂量与记录不一致。
CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
       '氨氯地平,10,mg,每日一次,2026-09-07,local-demo,口服,片,10mg\n'
       '二甲双胍,0.5,,,2026-09-01,local-demo,口服,片,0.5g\n')


class _Scripted:
    """脚本化的模型：只决定"下一步做什么"，其余全部走真实实现。

    它读的是**模型真正收到的 payload**（不是内部状态），所以这些测试也顺带锁住了
    "模型看到了什么"。
    """

    def __init__(self, decide):
        self.decide_fn = decide
        self.payloads = []

    def __call__(self, payload, timeout=None):
        self.payloads.append(payload)
        return json.dumps(self.decide_fn(payload), ensure_ascii=False)


def _decide(payload):
    """一个老老实实按上下文行动的脚本模型：读没读过的材料、问真正缺的信息、
    没有可做的就请求交付。它不重算差异，也不自己拼 scope/版本/预算。"""
    review = payload['review']
    context = review['trusted_context']
    for material in context['materials']:
        for item in material['items']:
            if not item['read_back']:
                return {'decision': 'tool', 'tool': 'read_material',
                        'arguments': {'material_ref': item['material_ref']}}
    if review['open_input_requests']:
        return {'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}
    # 有待核实的问题时，就**照着它**问用户要信息——问题文本来自上下文，
    # 不是脚本自己编的。
    question = next((entry for entry in context['pending'] if entry['kind'] == 'question'), None)
    if question is not None:
        return {'decision': 'tool', 'tool': 'request_information',
                'arguments': {'question_text': '请补充：' + question['statement'],
                              'required_fields': ['dose', 'schedule'],
                              'why_needed': '材料里这一条的字段不完整，无法判断是否与当前记录一致'}}
    return {'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}


class MaterialReviewProductTests(unittest.TestCase):

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
        self.refs = [f"{self.case['case_id']}/{item['item_id']}" for item in self.case['items']]
        self.model = _Scripted(_decide)
        self.agent = MedicationCoordinatorAgent(self.memory, llm_planner_enabled=True,
                                                proposal_provider=self.model)
        self.tasks = CareTasks(self.product, agent_factory=lambda: self.agent)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def _advance(self, task, key):
        """走 worker 的发布路径（enqueue + 出队执行），不是测试专用的直调。"""
        resumed = self.tasks.resume(task['id'], key, task['revision'], 'continue', enqueue=False)
        return self.product.get(resumed['id'], 'care_task')

    def test_full_closed_loop_with_incremental_update(self) -> None:
        # ---- 1. 用户提交核对目标与指定材料 ---------------------------------
        task = self.tasks.create('create-1', 'material_review', self.case['case_id'],
                                 goal='核对这份材料与当前记录，准备就诊')
        self.assertEqual(task['review_contract'], REVIEW_CONTRACT)

        # ---- 2. 核对：报告生成，模型只做策略 ------------------------------
        first = self._advance(task, 'resume-1')
        self.assertIn(first['status'], ('ready', 'waiting_input'))
        report_id = first['report_refs'][0]
        report = self.product.get(report_id, 'material_review_report')
        markdown = report['markdown']
        self.assertIn('## 1. 本次核对目标和覆盖范围', markdown)
        self.assertIn('## 3. 差异双方的记录及来源', markdown)

        # 确定性差异由代码算好并落成发现；模型没有被要求重算它。
        review = first['review_state']
        self.assertTrue(any(item['origin'] == 'system' for item in review['findings']))
        # 强制安全检查单独记录，不混进本任务的发现。
        self.assertTrue(report['safety_checks'])
        self.assertNotIn('强制安全检查', json.dumps(review['findings'], ensure_ascii=False))

        # ---- 3. 查看来源 ---------------------------------------------------
        self.assertIn('材料来源', markdown)
        # 基础覆盖读了两条；模型自己一条也没读——两者在报告里分得清。
        self.assertEqual(review['read_attribution']['system'], sorted(self.refs))
        self.assertEqual(review['material_read_refs'], [])
        # 每条作为结论出现的差异都点名了双方。
        self.assertIn('当前记录', markdown)

        # ---- 4. 补充**材料说明**：材料上写的单位与频次 ----------------
        self.assertEqual(first['status'], 'waiting_input')
        self.assertTrue(first['missing_inputs'])
        request = first['missing_inputs'][0]
        self.assertEqual(request['subjects'], ['二甲双胍'])
        self.assertIn('schedule', request['fields'])
        self.assertEqual(request['purpose'], 'material_note')
        # 覆盖不取决于模型：两条材料都已交代，缺的只是字段。
        self.assertEqual(first['coverage_progress']['items_pending'], 0)
        unresolved = next(item for item in review['findings']
                          if item['finding_type'] == 'contextual_note'
                          and '二甲双胍' in item['statement'])
        before_meds = self.memory.current_medications()

        second = self.tasks.record_input(
            task['id'], 'input-1', first['revision'],
            answers=[{'request_id': request['request_id'], 'field': 'unit', 'value': 'g',
                      'kind': 'material_note'},
                     {'request_id': request['request_id'], 'field': 'schedule',
                      'value': '每日两次', 'kind': 'material_note'}],
            review_request_ids=[request['request_id']])
        self.assertTrue(second['answered_requests'])
        # **普通提交回答不改权威记录**：这一次补充没有写进任何一条用药。
        self.assertEqual(second['authoritative_writes'], 0)
        self.assertEqual(self.memory.current_medications(), before_meds)

        after_input = self.product.get(task['id'], 'care_task')
        third = self._advance(after_input, 'resume-2')
        report_two = self.product.get(third['report_refs'][-1], 'material_review_report')

        # ---- 5. 报告修订及变化原因 -----------------------------------------
        self.assertNotEqual(report_two['id'], report_id)          # 新版本不覆盖旧报告
        self.assertGreater(report_two['review_revision'], report['review_revision'])
        self.assertTrue(report_two['revision_diff'])
        self.assertTrue(any('依据' in line for line in report_two['revision_diff']))
        # 变化依据里说明白：这是**您的补充**，不是当前记录被改了。
        self.assertTrue(any('您的补充' in line for line in report_two['revision_diff']))
        self.assertTrue(any('未' in line and '改变当前记录' in line
                            for line in report_two['revision_diff']))
        # 被说明取代的那条"字段不完整"不再作为当前结论出现。
        conclusions = '\n'.join((report_two['sections'].get('2. 一致项与主要变化') or [])
                                + (report_two['sections'].get('3. 差异双方的记录及来源') or []))
        self.assertNotIn(unresolved['statement'], conclusions)
        # 结论是按补上来的字段**比**出来的，而且标明了那一格来自用户说明。
        self.assertTrue(any('按您补充的说明' in line for line in
                            report_two['sections']['3. 差异双方的记录及来源']
                            + report_two['sections']['2. 一致项与主要变化']))
        # ……而它留下的历史仍然可回看（不是被删掉）。
        after_findings = third['review_state']['findings']
        superseded = next(item for item in after_findings
                          if item['finding_id'] == unresolved['finding_id'])
        self.assertTrue(superseded['stale'])
        self.assertTrue(superseded.get('superseded_by'))
        # 当前记录自始至终没有被这次补充改动过。
        self.assertEqual(self.memory.current_medications(), before_meds)

    def test_an_explicit_confirmation_updates_the_record_and_the_report(self) -> None:
        """**显式**的权威更新走既有的受控写入流程，并留下修改回执。

        这一条与上一条必须成对出现：材料说明与权威修改是两件事，谁也不能冒充谁。
        """
        task = self.tasks.create('create-x', 'material_review', self.case['case_id'],
                                 goal='核对这份材料与当前记录')
        first = self._advance(task, 'resume-1')
        before = {item['display_name']: item['dose'] for item in self.memory.current_medications()}
        self.assertEqual(before['氨氯地平'], '5mg')

        # 用户**明确确认**：当前记录里的氨氯地平应为 10mg（走 medications 通道）。
        result = self.tasks.record_input(
            task['id'], 'input-x', first['revision'],
            medications=[{'name': '氨氯地平', 'dose': '10mg', 'action': 'dose_change'}])
        self.assertEqual(result['authoritative_writes'], 1)
        after = {item['display_name']: item['dose'] for item in self.memory.current_medications()}
        self.assertEqual(after['氨氯地平'], '10mg')

        current = self.product.get(task['id'], 'care_task')
        again = self._advance(current, 'resume-x')
        report = self.product.get(again['report_refs'][-1], 'material_review_report')
        # 记录变了 → 比较对象变了 → 材料这一条不再是不一致。
        self.assertTrue(any('依据（当前记录）' in line for line in report['revision_diff']))
        # 旧报告仍在，没有被覆盖。
        self.assertEqual(len(again['review_state']['reports']), 2)
        self.assertNotEqual(again['review_state']['reports'][0]['report_id'],
                            again['review_state']['reports'][1]['report_id'])

    def test_only_the_affected_part_is_recomputed(self) -> None:
        """补充信息只影响相关条目：其余发现、证据与结论继续有效。"""
        task = self.tasks.create('create-2', 'material_review', self.case['case_id'],
                                 goal='核对这份材料与当前记录')
        first = self._advance(task, 'resume-1')
        before = {item['finding_id'] for item in first['review_state']['findings']}
        # 与本次补充无关的那一条：二甲双胍字段不完整产生的系统问题。
        unaffected = [item['question_id'] for item in first['review_state']['questions']
                      if '二甲双胍' in item['text']]
        self.assertTrue(unaffected)
        self.tasks.record_input(task['id'], 'input-1', first['revision'],
                                medications=[{'name': '氨氯地平', 'dose': '10mg',
                                              'action': 'dose_change'}])
        current = self.product.get(task['id'], 'care_task')
        second = self._advance(current, 'resume-2')
        after = second['review_state']['findings']
        retained = {item['finding_id'] for item in after if not item.get('stale')}
        # 被补充直接影响的那条**没有被删除**，只是不再作为当前结论。
        self.assertTrue(before - retained)
        self.assertTrue(before <= {item['finding_id'] for item in after})
        # 与本次补充无关的问题原样保留，没有被全量重扫掉。
        questions = {item['question_id']: item['status']
                     for item in second['review_state']['questions']}
        self.assertTrue(all(questions.get(qid) is not None for qid in unaffected))

    def test_chat_entry_and_worker_entry_share_one_core(self) -> None:
        """两个入口对同一输入必须给出同一个核心结果——业务循环只有一份。"""
        from stage0.review.advance import ReviewRunner
        from stage0.product import MaterialIndex
        task = self.tasks.create('create-3', 'material_review', self.case['case_id'],
                                 goal='核对这份材料与当前记录')
        runner = ReviewRunner.for_product(self.product, agent=self.agent,
                                          planner_transport=self.agent.planner.llm_planner)
        direct = runner.advance(task_id=task['id'], run_id='run:direct',
                                goal='核对这份材料与当前记录', scope_id='local-demo',
                                selected_case_ids=[self.case['case_id']],
                                review_index=MaterialIndex(self.product))
        worker = self._advance(task, 'resume-1')
        self.assertEqual(direct['delivery_status'], worker['report_axes']['delivery'])
        self.assertEqual(direct['evidence_status'], worker['report_axes']['evidence'])
        self.assertEqual(sorted(f['finding_id'] for f in direct['review']['findings']),
                         sorted(f['finding_id'] for f in worker['review_state']['findings']))

    def test_a_failed_run_still_delivers_the_accurate_part(self) -> None:
        """模型不可用时，规则整理的结果照样交付，且标明"未经模型核查"。"""
        tasks = CareTasks(self.product, agent_factory=lambda: self.agent)
        self.agent.planner.llm_planner.proposal_provider = None
        self.agent.planner.llm_planner.client = None
        self.agent.planner.llm_planner.model = None
        task = tasks.create('create-4', 'material_review', self.case['case_id'],
                            goal='核对这份材料与当前记录')
        result = tasks.resume(task['id'], 'resume-1', task['revision'], 'continue', enqueue=False)
        current = self.product.get(result['id'], 'care_task')
        report = self.product.get(current['report_refs'][0], 'material_review_report')
        self.assertIn('## 1. 本次核对目标和覆盖范围', report['markdown'])
        self.assertEqual(report['delivery_status'], 'partial')
        self.assertIn('部分结果', report['cycle_note'])


if __name__ == '__main__':
    unittest.main()
