"""material-review@2 —— 完成条件、字段级比较与问题分流（离线，无远程调用）。

这一组锁的是本轮的设计约束本身，对应验收场景 A–J：

* 完成与否由**交付要求**决定，不由"报告里有哪几节"决定，也不由"模型参没参与"决定；
* "列出差异与缺项"与"确认规格完全一致"是**两种**要求，满足条件不同；
* 字段级比较能把"材料写了、当前记录没有这一项"与"一致"分开；
* 问题的答案该从哪来是**约束**：用户不是资料的替代品，资料也不是用户事实的替代品；
* 可选建议可以挂着，不阻塞原任务，也不让任务一直转下去；
* 运行数据从任务保存的真实 run 引用读，读不到就是 unknown，不是 0。
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
from stage0.review.advance import ReviewRunner  # noqa: E402
from stage0.review.contract import REQ_SATISFIED  # noqa: E402

# 材料写了剂型与规格；当前记录**没有**这两列（药品记录本来就不跟踪它们）。
CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
       '氨氯地平,5,mg,每日一次,2026-09-07,local-demo,口服,片,5mg\n'
       '二甲双胍,0.5,g,每日两次,2026-09-01,local-demo,口服,片,0.5g\n')
RECORDS = (('氨氯地平', '5mg', '每日一次', '2026-09-07'),
           ('二甲双胍', '0.5g', '每日两次', '2026-09-01'))


class _Double:
    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def __call__(self, payload, timeout=None):
        self.payloads.append(payload)
        step = self.script.pop(0) if self.script else {
            'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}
        return json.dumps(step, ensure_ascii=False)


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.product = ProductStore(self.memory)
        for name, dose, schedule, start in RECORDS:
            self.memory.apply_medication_change(action='add', name=name, ingredients=[],
                                                session_id='s', turn_id='t', source='caregiver',
                                                dose=dose, schedule=schedule, route='口服',
                                                occurred_at=start)
        self.case = self.product.import_csv('k1', CSV)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def run_review(self, script=None, *, requested=None, model=True, run_id='run:1',
                   initial_state=None):
        double = _Double(script or [])
        runner = ReviewRunner(
            memory=self.memory, evidence_store=self._evidence(), product=self.product,
            material_index=MaterialIndex(self.product),
            planner_transport=self._transport(double) if model else None)
        result = runner.advance(task_id='task:1', run_id=run_id, goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[self.case['case_id']],
                                review_index=MaterialIndex(self.product),
                                initial_state=initial_state, requested=requested)
        return double, runner, result

    def _transport(self, double):
        from stage0.agent import LLMPlanner
        return LLMPlanner(proposal_provider=double, tool_schemas={})

    def _evidence(self):
        from stage0.harness.evidence import EvidenceStore
        return EvidenceStore(self.memory.connection, self.memory._lock)

    @staticmethod
    def requirement(result, requirement_id):
        return next(item for item in result['review']['spec']['delivery_requirements']
                    if item['requirement_id'] == requirement_id)


# ---- A. 只要求列出差异，可选问题不阻塞 -----------------------------------------

class ScenarioA(_Base):

    def test_an_optional_question_does_not_block_the_requested_task(self) -> None:
        """用户只要"列出差异与缺项"，模型顺带提了一条背景问题——原任务仍然完成。"""
        double, _, result = self.run_review([
            {'decision': 'tool', 'tool': 'submit_question',
             'arguments': {'question_key': 'background', 'text': '这两种药一起吃要注意什么',
                           'subjects': ['氨氯地平'], 'direction': 'reference_evidence',
                           'optional': True}},
        ], requested=[{'kind': 'list_differences'}])
        self.assertEqual(self.requirement(result, 'user:0:list_differences')['status'],
                         REQ_SATISFIED)
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertEqual(result['run_status'], 'ended')
        # 可选问题仍然被展示出来，只是不阻塞。
        optional = [item for item in result['review']['spec']['delivery_requirements']
                    if not item['required']]
        self.assertTrue(optional)
        self.assertIn('可选', result['report']['markdown'])

    def test_a_plain_run_without_requirements_still_finishes(self) -> None:
        """没勾任何用户要求时，契约自身那两项就是全部完成条件。"""
        _, _, result = self.run_review([])
        self.assertEqual(result['delivery_status'], 'complete')

    def test_the_most_important_finding_is_never_withheld(self) -> None:
        """代码生成的结论不会被交付前的措辞检查误撤。

        真实浏览器验收里发生过：把几个字段名连起来写（"服用频次…给药途径"）撞上了
        处方措辞检查，于是"材料未列出这条当前记录"这种最该被读到的发现整句变成了
        "本报告不复述"。换的是**标签**，检查本身没有放宽。
        """
        csv = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
               '氨氯地平,5,mg,每日一次,2026-09-07,local-demo,口服,片,5mg\n')
        case = self.product.import_csv('k-unlisted', csv)
        double = _Double([])
        runner = ReviewRunner(memory=self.memory, evidence_store=self._evidence(),
                              product=self.product, material_index=MaterialIndex(self.product),
                              planner_transport=self._transport(double))
        result = runner.advance(task_id='task:unlisted', run_id='run:u', goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[case['case_id']],
                                review_index=MaterialIndex(self.product),
                                requested=[{'kind': 'list_differences'}])
        text = result['report']['markdown']
        self.assertNotIn('本报告不复述', text)
        self.assertIn('材料未列出这条当前记录', text)
        # 用户读到的正文里不出现工具名与错误枚举。
        self.assertNotIn('invalid_arguments', text)


# ---- B / C. 缺规格：两种要求，两种结果 -----------------------------------------

class ScenarioBC(_Base):

    def test_confirming_a_field_that_the_record_lacks_cannot_be_satisfied(self) -> None:
        """要求"确认规格一致"，但当前记录里没有规格 → 不得显示"一致"，补问并 partial。"""
        _, _, result = self.run_review(
            [], requested=[{'kind': 'confirm_field', 'field': 'strength', 'expect': 'equal'}])
        requirement = self.requirement(result, 'user:0:confirm_field')
        self.assertEqual(requirement['status'], 'awaiting_user')
        self.assertIn('规格', requirement['reason'])
        self.assertEqual(result['delivery_status'], 'partial')
        self.assertEqual(result['run_status'], 'waiting_input')
        # **不能**因为报告里写了"规格未知"就算完成。
        self.assertNotEqual(requirement['status'], REQ_SATISFIED)

    def test_the_ask_names_the_field_and_what_it_blocks(self) -> None:
        """系统自己建的补问要说清缺什么、挡住哪一项要求、为什么材料给不了。"""
        _, _, result = self.run_review(
            [], requested=[{'kind': 'confirm_field', 'field': 'strength', 'expect': 'equal'}])
        request = result['report']['input_requests'][0]
        # 两条材料的规格都没有可比对的记录值 → 一条请求把两个对象一起问完。
        self.assertEqual(sorted(request['subjects']), ['二甲双胍', '氨氯地平'])
        self.assertIn('strength', request['required_fields'])
        self.assertIn('规格', request['missing_fact'])
        self.assertTrue(request['why_material_insufficient'])
        self.assertEqual(request['blocks_requirement_ids'], ['user:0:confirm_field'])

    def test_listing_the_gap_is_enough_when_that_is_all_that_was_asked(self) -> None:
        """同样缺规格，但用户只要"列出差异与缺项"——准确列出缺项就满足了。"""
        _, _, result = self.run_review([], requested=[{'kind': 'list_differences'}])
        requirement = self.requirement(result, 'user:0:list_differences')
        self.assertEqual(requirement['status'], REQ_SATISFIED)
        self.assertEqual(result['delivery_status'], 'complete')
        # 缺的那一项确实被列了出来，而不是被当成"一致"。
        field_lines = '\n'.join(result['report']['sections']['3. 差异双方的记录及来源'])
        self.assertIn('规格', field_lines)
        self.assertIn('当前记录里没有这一项', field_lines)

    def test_a_row_is_never_labelled_consistent_while_a_field_is_uncompared(self) -> None:
        """行级"一致"不能掩盖字段未确认：摘要按核心字段派生，缺项单独列。"""
        _, _, result = self.run_review([], requested=[{'kind': 'list_differences'}])
        rows = next(iter(result['review']['field_comparisons'].values()))
        by_field = {row['field']: row['comparison_status'] for row in rows}
        self.assertEqual(by_field['dose'], 'equal')
        self.assertEqual(by_field['strength'], 'missing_right')
        summary = None
        for ref, item in result['review']['material_items'].items():
            summary = item['comparison']['summary']
            break
        self.assertEqual(summary['core']['undecided'], 0)
        self.assertIn('strength', summary['record_absent_fields'])
        self.assertIn('规格', summary['record_absent_labels'])


# ---- D. 用户明确要求依据资料回答 -----------------------------------------------

class ScenarioD(_Base):

    def test_a_waiting_note_is_not_an_answer(self) -> None:
        """只写一条"请用户确认说明书结论"不能满足"依据资料回答"这条要求。"""
        _, _, result = self.run_review(
            [{'decision': 'tool', 'tool': 'submit_question',
              'arguments': {'question_key': 'label', 'text': '说明书里怎么写的',
                            'subjects': ['氨氯地平'], 'direction': 'reference_evidence',
                            'serves_requirement_id': 'user:0:answer_from_source'}}],
            requested=[{'kind': 'answer_from_source',
                        'question': '这份材料里的用法与说明书是否一致？'}])
        requirement = self.requirement(result, 'user:0:answer_from_source')
        self.assertNotEqual(requirement['status'], REQ_SATISFIED)
        self.assertEqual(requirement['status'], 'awaiting_evidence')
        self.assertEqual(result['delivery_status'], 'partial')

    def test_the_user_cannot_answer_a_reference_question(self) -> None:
        """用户不是资料的替代品：这类请求用户答了也不关闭。"""
        _, _, result = self.run_review(
            [{'decision': 'tool', 'tool': 'submit_question',
              'arguments': {'question_key': 'label', 'text': '说明书里怎么写的',
                            'subjects': ['氨氯地平'], 'direction': 'reference_evidence',
                            'serves_requirement_id': 'user:0:answer_from_source'}},
             {'decision': 'tool', 'tool': 'request_information',
              'arguments': {'question_text': '请确认说明书里的写法', 'subjects': ['氨氯地平'],
                            'blocks_requirement_id': 'user:0:answer_from_source'}}],
            requested=[{'kind': 'answer_from_source',
                        'question': '这份材料里的用法与说明书是否一致？'}])
        from stage0.review.state import MaterialReviewState
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        request = [item for item in state.input_requests if item['origin'] == 'model'][0]
        self.assertFalse(state.request_answerable_by_user(request['request_id']))
        answered, recorded_only = state.answer_input_requests(request_ids=[request['request_id']])
        self.assertEqual(answered, [])
        self.assertEqual(recorded_only, [request['request_id']])

    def test_a_professional_review_question_cannot_be_closed_by_the_user(self) -> None:
        _, _, result = self.run_review(
            [{'decision': 'tool', 'tool': 'submit_question',
              'arguments': {'question_key': 'clinical', 'text': '这个剂量是否需要调整',
                            'subjects': ['氨氯地平'], 'direction': 'professional_review',
                            'optional': True}}])
        from stage0.review.state import MaterialReviewState
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        question = next(item for item in state.questions if item['question_key'] == 'clinical')
        state.add_input_request(question_text='请确认是否需要调整剂量', subjects=['氨氯地平'],
                                related_question_ids=[question['question_id']])
        request = state.input_requests[0]
        self.assertFalse(state.request_answerable_by_user(request['request_id']))


# ---- E. 用户实际使用情况不能由资料顶替 -----------------------------------------

class ScenarioE(_Base):

    def test_evidence_does_not_replace_a_fact_only_the_user_has(self) -> None:
        """问题的方向是"用户提供事实"时，证据再充分也不能把它判成已回答。"""
        _, _, result = self.run_review(
            [{'decision': 'tool', 'tool': 'submit_question',
              'arguments': {'question_key': 'actual_use', 'text': '用户实际每天吃几次',
                            'subjects': ['氨氯地平'], 'direction': 'user_input',
                            'serves_requirement_id': 'user:0:answer_from_source'}}],
            requested=[{'kind': 'answer_from_source',
                        'question': '这位用户实际每天服用几次？'}])
        requirement = self.requirement(result, 'user:0:answer_from_source')
        self.assertEqual(requirement['status'], 'awaiting_user')
        self.assertEqual(result['run_status'], 'waiting_input')

    def test_a_question_about_the_material_does_not_go_back_to_the_user(self) -> None:
        """材料上已经比出结论的对象，不该回头问用户同一件事。"""
        from stage0.review import capabilities as caps
        from stage0.review.contract import TaskSpec
        from stage0.review.state import MaterialReviewState
        _, _, result = self.run_review([], requested=[{'kind': 'list_differences'}])
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        errors = caps.proposal_errors(state, {
            'tool': 'submit_question',
            'arguments': {'question_key': 'dose_again', 'text': '氨氯地平的剂量是多少',
                          'subjects': ['memory:medication:1@v1'], 'direction': 'user_input'}})
        self.assertIn('material_already_answers_this', errors)

    def test_a_research_question_needs_a_requirement_or_must_be_optional(self) -> None:
        from stage0.review import capabilities as caps
        from stage0.review.state import MaterialReviewState
        _, _, result = self.run_review([])
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        errors = caps.proposal_errors(state, {
            'tool': 'submit_question',
            'arguments': {'question_key': 'x', 'text': '随便问一句',
                          'subjects': ['氨氯地平'], 'direction': 'reference_evidence'}})
        self.assertIn('question_needs_requirement_or_optional', errors)


# ---- F. 同一药物两个要求，只回答一个 -------------------------------------------

class ScenarioF(_Base):

    def test_answering_one_field_updates_only_that_requirement(self) -> None:
        _, runner, first = self.run_review(
            [], requested=[{'kind': 'confirm_field', 'field': 'dose'},
                           {'kind': 'confirm_field', 'field': 'schedule'}])
        # 材料里两条记录的剂量与频次都写全了 —— 两个要求都应当已经满足。
        self.assertEqual(self.requirement(first, 'user:0:confirm_field')['status'], REQ_SATISFIED)
        self.assertEqual(self.requirement(first, 'user:1:confirm_field')['status'], REQ_SATISFIED)

    def test_only_the_answered_field_changes(self) -> None:
        """只补了剂量，频次那一项保持原样——不做全量重算。"""
        csv = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
               '氨氯地平,5,,,2026-09-07,local-demo,口服,片,5mg\n')
        case = self.product.import_csv('k-partial', csv)
        double = _Double([])
        runner = ReviewRunner(memory=self.memory, evidence_store=self._evidence(),
                              product=self.product, material_index=MaterialIndex(self.product),
                              planner_transport=self._transport(double))
        first = runner.advance(task_id='task:f', run_id='run:f1', goal='核对材料与当前记录',
                               scope_id='local-demo', selected_case_ids=[case['case_id']],
                               review_index=MaterialIndex(self.product),
                               requested=[{'kind': 'confirm_field', 'field': 'dose'},
                                          {'kind': 'confirm_field', 'field': 'schedule'}])
        # 剂量与频次都缺 → 两项都在等用户。
        self.assertEqual(self.requirement(first, 'user:0:confirm_field')['status'],
                         'awaiting_user')
        self.assertEqual(self.requirement(first, 'user:1:confirm_field')['status'],
                         'awaiting_user')
        requests = {tuple(sorted(item['required_fields'])): item['request_id']
                    for item in first['review']['input_requests']}
        self.assertEqual(len(requests), 1, '同一对象的缺项应当合并成一条请求')
        request_id = next(iter(requests.values()))

        from stage0.care_tasks import CareTasks
        tasks = CareTasks(self.product, agent_factory=lambda: None)
        task = tasks.create('create-f', 'material_review', case['case_id'], goal='核对材料与当前记录')
        task['review_state'] = first['review']
        task['requested'] = [{'kind': 'confirm_field', 'field': 'dose'}]
        with self.product.transaction():
            self.product.save('care_task', task)
        tasks.record_input(task['id'], 'input-f', task['revision'],
                           answers=[{'request_id': request_id, 'field': 'dose', 'value': '5',
                                     'kind': 'material_note'}],
                           review_request_ids=[request_id])
        stored = self.product.get(task['id'], 'care_task')['review_state']
        by_field = {row['field']: row['comparison_status']
                    for rows in stored['field_comparisons'].values() for row in rows}
        # 补上的那一格有了值；没补的保持原样——不做全量重算，也不假装它已经比过。
        self.assertNotEqual(by_field['schedule'], 'equal', '没补的字段不能变成一致')
        self.assertIn(by_field['schedule'], ('missing_left', 'not_comparable'))


# ---- G. 必要都解决了，可选还开着 -----------------------------------------------

class ScenarioG(_Base):

    def test_optional_work_does_not_reopen_the_task(self) -> None:
        """必需要求全部满足后，可选问题开着也不重新暂停、不重复调用模型。"""
        double, runner, first = self.run_review([
            {'decision': 'tool', 'tool': 'submit_question',
             'arguments': {'question_key': 'optional_check', 'text': '要不要也看看说明书',
                           'subjects': ['氨氯地平'], 'direction': 'reference_evidence',
                           'optional': True}},
        ], requested=[{'kind': 'list_differences'}])
        self.assertEqual(first['delivery_status'], 'complete')
        calls_after_first = len(double.payloads)
        second = runner.advance(task_id='task:1', run_id='run:2', goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[self.case['case_id']],
                                review_index=MaterialIndex(self.product),
                                initial_state=first['review'])
        # 恢复之后立刻收尾：没有可推进的必需工作，可选问题不该让循环继续转。
        self.assertLessEqual(len(double.payloads) - calls_after_first, 1)
        self.assertEqual(second['delivery_status'], 'complete')
        self.assertNotEqual(second['run_status'], 'waiting_input')


# ---- H. 模型不可用 -------------------------------------------------------------

class ScenarioH(_Base):

    def test_a_deterministic_task_completes_and_does_not_claim_model_analysis(self) -> None:
        _, _, result = self.run_review([], requested=[{'kind': 'list_differences'}], model=False)
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertEqual(result['review']['model_cycles'], 0)
        self.assertEqual(result['report']['attribution']['model']['cycles'], 0)
        text = result['report']['markdown']
        self.assertNotIn('模型已经分析', text)
        self.assertNotIn('自主调查成功', text)
        self.assertIn('没有模型参与', text)


# ---- I. 新来源否定旧结论 -------------------------------------------------------

class ScenarioI(_Base):

    def test_a_negated_conclusion_is_updated_not_ignored(self) -> None:
        """最初属于可选调查的结论被新来源否定时，受影响的判断要跟着改。"""
        from stage0.harness.evidence import capture_from_rag_result
        from stage0.review.state import MaterialReviewState
        _, _, result = self.run_review([], requested=[{'kind': 'list_differences'}])
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        store = self._evidence()
        captured = capture_from_rag_result(store, {
            'status': 'found', 'corpus_version': 'v1', 'results': [
                {'chunk_id': 'c1', 'text': '本说明书记载了该药品的服用方法与注意事项。',
                 'source_url': 'https://example.invalid/label', 'section': '用法用量'}]},
            run_id='run:1', query='氨氯地平 用法用量', scope_id='local-demo',
            patient_revision=self.memory.scope_revision('medications'))
        evidence_id = captured[0]['evidence_id']
        state.evidence_refs.append(evidence_id)
        state.read_evidence_refs.append(evidence_id)
        question = state.submit_question(question_key='label', text='说明书里的用法',
                                         subjects=['氨氯地平'],
                                         direction='reference_evidence')
        assertion = state.submit_assertion(
            subject_refs=['氨氯地平'], predicate='label_statement',
            value='说明书标注了服用方法与注意事项',
            qualifiers={'excerpt': '本说明书记载了该药品的服用方法与注意事项。'},
            evidence_refs=[evidence_id])
        assertion['question_refs'] = [question['question_id']]
        assertion['verification_status'] = 'supported'
        self.assertIn(assertion['assertion_id'],
                      [item['assertion_id'] for item in state.assertions
                       if item['verification_status'] == 'supported'])
        # 来源失效：结论跟着失效，不能因为"它本来只是一次可选调查"就留着。
        store.connection.execute(
            "DELETE FROM evidence_records WHERE evidence_id=?", (evidence_id,))
        store.connection.commit()
        from stage0.review.incremental import invalidate_sources
        outcome = invalidate_sources(state, store)
        self.assertIn(evidence_id, outcome['invalidated_sources'])
        self.assertEqual(assertion['verification_status'], 'insufficient')
        from stage0.review.report import section_content
        sections = section_content(state)
        marker = '说明书标注了服用方法与注意事项'
        self.assertFalse(any(marker in line for line in sections['2. 一致项与主要变化']))
        self.assertTrue(any(marker in line for line in sections['4. 仍待确认的问题']))


# ---- J. 运行数据从真实 run 引用读 ---------------------------------------------

class ScenarioJ(_Base):

    def _tasks(self):
        from stage0.care_tasks import CareTasks
        return CareTasks(self.product, agent_factory=lambda: None)

    def test_a_missing_run_reference_is_unknown_not_zero(self) -> None:
        """读不到就写 unknown。**0 是一个测量结果**，不是一个默认值。"""
        tasks = self._tasks()
        task = tasks.create('create-j', 'material_review', self.case['case_id'],
                            goal='核对材料与当前记录')
        usage = tasks.usage(task)
        self.assertIsNone(usage['calls'])
        self.assertIsNone(usage['tokens'])
        self.assertFalse(usage['measured'])
        self.assertEqual(usage['reason'], 'no_child_runs')

    def test_usage_is_read_from_the_run_the_task_recorded(self) -> None:
        tasks = self._tasks()
        task = tasks.create('create-j2', 'material_review', self.case['case_id'],
                            goal='核对材料与当前记录')
        run_id = f"care-task:{task['id']}:1"
        self.memory.workflow_run_start(run_id=run_id, graph_version='x', budget={})
        self.memory.workflow_run_update(run_id, status='succeeded', result={})
        with self.memory._lock:
            self.memory.connection.execute(
                "UPDATE workflow_runs SET budget_json=? WHERE run_id=?",
                (json.dumps({'calls_attempted': 4, 'tokens_actual': 1000, 'refused_calls': 1}),
                 run_id))
            self.memory.connection.commit()
        with self.product.transaction():
            task['resource_budget']['child_run_ids'] = [run_id]
            self.product.save('care_task', task)
        usage = tasks.usage(self.product.get(task['id'], 'care_task'))
        self.assertTrue(usage['measured'])
        self.assertEqual(usage['calls'], 4)
        self.assertEqual(usage['tokens'], 1000)
        self.assertEqual(usage['refused_calls'], 1)
        # 任务上也带着同一份读数，不再各算一遍。
        self.assertEqual(tasks.usage(task), usage)

    def test_an_unknown_provider_usage_is_not_reported_as_zero(self) -> None:
        tasks = self._tasks()
        task = tasks.create('create-j3', 'material_review', self.case['case_id'],
                            goal='核对材料与当前记录')
        run_id = f"care-task:{task['id']}:1"
        self.memory.workflow_run_start(run_id=run_id, graph_version='x', budget={})
        with self.memory._lock:
            self.memory.connection.execute(
                "UPDATE workflow_runs SET budget_json=? WHERE run_id=?",
                (json.dumps({'calls_attempted': 2, 'usage_unknown': True}), run_id))
            self.memory.connection.commit()
        with self.product.transaction():
            task['resource_budget']['child_run_ids'] = [run_id]
            self.product.save('care_task', task)
        usage = tasks.usage(self.product.get(task['id'], 'care_task'))
        self.assertFalse(usage['measured'])
        self.assertIsNone(usage['tokens'])
        self.assertEqual(usage['reason'], 'provider_usage_unknown')


if __name__ == '__main__':
    unittest.main()
