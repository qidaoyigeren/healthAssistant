"""material-review@2 —— 材料覆盖、补问恢复与完整交付（离线，无远程调用）。

这一组锁的是本轮的设计约束本身，对应验收场景 A–H 与它们的错误反例：

* **基础覆盖由运行器完成**，不取决于模型愿不愿意逐条调用工具；
* 只有**真的读过并通过校验**的来源才算已覆盖——索引里出现过不算；
* 补问是一个**状态转换**，不是一个普通的工具调用；
* 材料说明、用户陈述与权威记录更新是**三件事**；
* 模型缺席、来源失效、零次检索都不被伪装成"核查完成"。

刻意不重复锁已经由 ``test_material_review*.py`` 覆盖的实现细节：这里每一条都
对应一个会真实发生、而且以前会被写错的场景。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage0.memory import MemoryStore  # noqa: E402
from stage0.product import MaterialIndex, ProductError, ProductStore  # noqa: E402
from stage0.review.advance import ReviewRunner  # noqa: E402

# A：两条材料字段都完整，且都与当前记录一致。
COMPLETE_CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
                '氨氯地平,5,mg,每日一次,2026-09-07,local-demo,口服,片,5mg\n'
                '二甲双胍,0.5,g,每日两次,2026-09-01,local-demo,口服,片,0.5g\n')
# B：第二条缺单位与频次。
INCOMPLETE_CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
                  '氨氯地平,5,mg,每日一次,2026-09-07,local-demo,口服,片,5mg\n'
                  '二甲双胍,0.5,,,2026-09-01,local-demo,口服,片,0.5g\n')
# A'：一条不一致，用来证明差异不会被"覆盖完成"抹掉。
CHANGED_CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
               '氨氯地平,10,mg,每日一次,2026-09-07,local-demo,口服,片,10mg\n'
               '二甲双胍,0.5,g,每日两次,2026-09-01,local-demo,口服,片,0.5g\n')

RECORDS = (('氨氯地平', '5mg', '每日一次', '2026-09-07'),
           ('二甲双胍', '0.5g', '每日两次', '2026-09-01'))


class _Double:
    """脚本化的"模型"：只提供决策这一个变量，其余全走真实实现。"""

    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def __call__(self, payload, timeout=None):
        self.payloads.append(payload)
        step = self.script.pop(0) if self.script else {
            'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}
        return json.dumps(step, ensure_ascii=False)


class _BrokenIndex(MaterialIndex):
    """一条材料**读不到**：仍在索引里，但打开它就会失败。

    这正是"索引里出现过一个条目"与"原文确实被读过"之间的区别。
    """

    def __init__(self, store, broken_ref):
        super().__init__(store)
        self.broken_ref = broken_ref

    def item(self, case_id, item_id):
        if f'{case_id}/{item_id}' == self.broken_ref:
            raise ProductError('材料条目无法打开', 500)
        return super().item(case_id, item_id)


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

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def case(self, csv, key='k1'):
        case = self.product.import_csv(key, csv)
        refs = {item['candidate']['fields']['name']: f"{case['case_id']}/{item['item_id']}"
                for item in case['items'] if item.get('candidate')}
        return case, refs

    def run_review(self, case, script=None, *, index=None, model=True, run_id='run:1',
                   initial_state=None, transport=None, requested=None):
        double = _Double(script or [])
        runner = ReviewRunner(
            memory=self.memory, evidence_store=self._evidence(), product=self.product,
            material_index=index or MaterialIndex(self.product),
            planner_transport=transport if transport is not None
            else (self._transport(double) if model else None))
        result = runner.advance(task_id='task:1', run_id=run_id, goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[case['case_id']],
                                review_index=index or MaterialIndex(self.product),
                                initial_state=initial_state, requested=requested)
        return double, runner, result

    def _transport(self, double):
        from stage0.agent import LLMPlanner
        return LLMPlanner(proposal_provider=double, tool_schemas={})

    def _evidence(self):
        from stage0.harness.evidence import EvidenceStore
        return EvidenceStore(self.memory.connection, self.memory._lock)

    def assertCovered(self, result, refs):
        coverage = {item['requirement_id']: item['disposition']
                    for item in result['review']['spec']['coverage_requirements']}
        for ref in refs:
            self.assertEqual(coverage.get(f'material_item:{ref}'), 'covered',
                             f'{ref} 应当被基础覆盖交代')
        self.assertEqual(result['review']['coverage_progress']['items_pending'], 0)


# ---- A. 多条材料均可读取且字段完整 -------------------------------------------

class ScenarioA(_Base):

    def test_every_selected_item_is_covered_even_if_the_model_reads_none(self) -> None:
        """模型一条材料都没读，全部条目照样有处置，报告照样覆盖全部指定条目。

        这正是"必须覆盖的材料仍依赖模型逐条选择"的止点：覆盖是运行器的职责。
        """
        case, refs = self.case(COMPLETE_CSV)
        double, _, result = self.run_review(case)
        self.assertCovered(result, refs.values())
        self.assertEqual(result['review']['material_read_refs'], [])
        self.assertEqual(len(result['review']['read_attribution']['system']), 2)
        # 两条都一致 → 都进入"一致项"，而不是留在未覆盖范围里。
        matched = result['report']['sections']['2. 一致项与主要变化']
        self.assertTrue(any('氨氯地平' in line for line in matched))
        self.assertTrue(any('二甲双胍' in line for line in matched))
        self.assertEqual(result['report']['sections']['6. 未覆盖范围和执行限制'], [])

    def test_a_complete_material_review_can_be_delivered_as_complete(self) -> None:
        """全部可读、全部一致、模型参与了判断 → 交付是**完整**而不是永远 partial。"""
        case, _ = self.case(COMPLETE_CSV)
        double, _, result = self.run_review(case)
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertEqual(result['report']['gaps'], [])
        self.assertNotIn('部分结果', result['report']['cycle_note'])

    def test_a_known_difference_is_not_erased_by_completing_coverage(self) -> None:
        """覆盖完成不等于"没有问题"：已知差异照样出现在报告里。"""
        case, refs = self.case(CHANGED_CSV, 'k-changed')
        _, _, result = self.run_review(case)
        self.assertCovered(result, refs.values())
        differences = result['report']['sections']['3. 差异双方的记录及来源']
        self.assertTrue(any('氨氯地平' in line and '不一致' in line for line in differences))

    def test_the_same_run_reports_who_did_what(self) -> None:
        case, _ = self.case(COMPLETE_CSV)
        _, _, result = self.run_review(case)
        attribution = result['report']['attribution']
        self.assertEqual(attribution['system']['materials_read'],
                         sorted(result['review']['read_attribution']['system']))
        self.assertEqual(attribution['model']['materials_read'], [])
        self.assertEqual(attribution['unattributed']['materials_not_read'], [])
        self.assertGreaterEqual(attribution['system']['field_comparisons'], 2)


# ---- B. 一条材料缺字段 ---------------------------------------------------------

class ScenarioB(_Base):

    def test_one_incomplete_item_does_not_stop_the_rest(self) -> None:
        case, refs = self.case(INCOMPLETE_CSV)
        _, _, result = self.run_review(case)
        coverage = {item['requirement_id']: item['disposition']
                    for item in result['review']['spec']['coverage_requirements']}
        # 完整的那些条目照常核对完毕。
        self.assertEqual(coverage[f"material_item:{refs['氨氯地平']}"], 'covered')
        # 缺字段的那条被明确标成"依据不足"，而且**带原因**。
        requirement = next(item for item in result['review']['spec']['coverage_requirements']
                           if item['requirement_id'] == f"material_item:{refs['二甲双胍']}")
        self.assertEqual(requirement['disposition'], 'insufficient')
        self.assertTrue(requirement['disposition_reason'])
        self.assertEqual(result['review']['coverage_progress']['items_pending'], 0)

    def test_the_request_names_the_object_and_the_fields(self) -> None:
        case, refs = self.case(INCOMPLETE_CSV)
        _, _, result = self.run_review(case)
        self.assertEqual(result['run_status'], 'waiting_input')
        requests = result['report']['input_requests']
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request['subjects'], ['二甲双胍'])
        self.assertEqual(sorted(request['required_fields']), ['schedule', 'unit'])
        self.assertEqual(request['purpose'], 'material_note')
        self.assertEqual(request['target']['material_ref'], refs['二甲双胍'])

    def test_nothing_repeats_while_waiting(self) -> None:
        """等待期间不继续产生无效模型请求：模型被调用一次，请求只有一条。"""
        case, _ = self.case(INCOMPLETE_CSV)
        double, _, result = self.run_review(case, [
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '材料里二甲双胍这一条缺什么？',
                           'subjects': ['二甲双胍'], 'required_fields': ['unit', 'schedule']}}])
        self.assertEqual(result['run_status'], 'waiting_input')
        self.assertEqual(len(result['review']['input_requests']), 1)
        self.assertLessEqual(len(double.payloads), 2)

    def test_the_summary_cannot_be_reached_by_marking_everything_insufficient(self) -> None:
        """把条目一律标成"依据不足"**不能**换来完整交付。

        错误的处置没有依据、也没有对应的后续动作，交付检查会把它当成缺口，而不是
        当成"已经交代过了"。
        """
        from stage0.review.delivery import check_delivery
        from stage0.review.state import MaterialReviewState
        case, refs = self.case(INCOMPLETE_CSV)
        _, _, result = self.run_review(case)
        from stage0.review.contract import TaskSpec
        state = MaterialReviewState(TaskSpec.from_dict(result['review']['spec']))
        state.source_reads = {}
        for requirement in state.spec.coverage_requirements:
            requirement['disposition'] = 'insufficient'
            requirement.pop('disposition_reason', None)
            requirement['finding_refs'] = []
            requirement['issue_refs'] = []
        check = check_delivery(state)
        self.assertTrue(any(item['code'] == 'uncertainty_without_basis'
                            for item in check['gaps']))
        self.assertFalse(check['ok'])


# ---- C. 两条补问，只回答一条 ---------------------------------------------------

class ScenarioC(_Base):

    def test_answering_one_of_two_requests_leaves_the_other_open(self) -> None:
        case, refs = self.case(INCOMPLETE_CSV)
        _, runner, first = self.run_review(case, [
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '氨氯地平的剂量是多少？',
                           'subjects': ['氨氯地平'], 'required_fields': ['dose']}},
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '氨氯地平的服用频次是多少？',
                           'subjects': ['氨氯地平'], 'required_fields': ['schedule']}}])
        self.assertEqual(first['run_status'], 'waiting_input')
        open_ids = [item['request_id'] for item in first['report']['input_requests']]
        self.assertEqual(len(open_ids), 2)
        answered_id = open_ids[0]

        from stage0.care_tasks import CareTasks
        tasks = CareTasks(self.product, agent_factory=lambda: None)
        task = tasks.create('create-c', 'material_review', case['case_id'],
                            goal='核对材料与当前记录')
        task['review_state'] = first['review']
        with self.product.transaction():
            self.product.save('care_task', task)
        record = tasks.record_input(
            task['id'], 'input-c', task['revision'],
            answers=[{'request_id': answered_id, 'field': 'dose', 'value': '5mg',
                      'kind': 'material_note'}],
            review_request_ids=[answered_id])
        self.assertEqual(record['answered_requests'], [answered_id])

        review = self.product.get(task['id'], 'care_task')['review_state']
        by_id = {item['request_id']: item['status'] for item in review['input_requests']}
        self.assertEqual(by_id[answered_id], 'answered')
        self.assertEqual(by_id[open_ids[1]], 'open')
        # 已经完成的覆盖没有被重做：两条材料的读取凭据都还在。
        self.assertEqual(len(review['read_attribution']['system']), 2)


# ---- D. 用户补充材料描述 -------------------------------------------------------

class ScenarioD(_Base):

    def test_a_material_note_updates_the_report_without_touching_the_record(self) -> None:
        case, refs = self.case(INCOMPLETE_CSV)
        _, runner, first = self.run_review(case)
        request = first['report']['input_requests'][0]
        before = self.memory.current_medications()

        from stage0.care_tasks import CareTasks
        tasks = CareTasks(self.product, agent_factory=lambda: None)
        task = tasks.create('create-d', 'material_review', case['case_id'],
                            goal='核对材料与当前记录')
        task['review_state'] = first['review']
        with self.product.transaction():
            self.product.save('care_task', task)
        receipt = tasks.record_input(
            task['id'], 'input-d', task['revision'],
            answers=[{'request_id': request['request_id'], 'field': 'unit', 'value': 'g',
                      'kind': 'material_note'},
                     {'request_id': request['request_id'], 'field': 'schedule',
                      'value': '每日两次', 'kind': 'material_note'}],
            review_request_ids=[request['request_id']])
        # 权威记录**分毫未动**：这是"材料上写的是什么"，不是"把药单改成什么"。
        self.assertEqual(receipt['authoritative_writes'], 0)
        self.assertEqual(self.memory.current_medications(), before)

        stored = self.product.get(task['id'], 'care_task')['review_state']
        answer = [item for item in stored['answers'] if item['field'] == 'unit'][0]
        self.assertEqual(answer['verification_status'], 'recorded_as_reported')
        self.assertFalse(answer['applied_authoritative'])

        _, _, second = self.run_review(case, [
            {'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}],
            run_id='run:2', initial_state=stored)
        # 结论按补上来的字段重新比出来了，而且报告说明了那一格来自用户说明。
        lines = (second['report']['sections']['2. 一致项与主要变化']
                 + second['report']['sections']['3. 差异双方的记录及来源'])
        self.assertTrue(any('按您补充的说明' in line for line in lines))
        # 变化依据里写清楚这次的依据是"您的补充"，并且**没有**改当前记录。
        basis = '\n'.join(second['report']['revision_diff'])
        self.assertIn('您的补充', basis)
        self.assertIn('改变当前记录', basis)
        self.assertEqual(self.memory.current_medications(), before)


# ---- E. 用户通过明确流程确认事实变更 -------------------------------------------

class ScenarioE(_Base):

    def test_an_explicit_update_changes_the_record_and_publishes_a_new_report(self) -> None:
        case, _ = self.case(CHANGED_CSV, 'k-e')
        _, runner, first = self.run_review(case)
        difference = next(item for item in first['review']['findings']
                          if item['finding_type'] == 'discrepancy')

        from stage0.care_tasks import CareTasks
        tasks = CareTasks(self.product, agent_factory=lambda: None)
        task = tasks.create('create-e', 'material_review', case['case_id'],
                            goal='核对材料与当前记录')
        task['review_state'] = first['review']
        with self.product.transaction():
            self.product.save('care_task', task)
        receipt = tasks.record_input(
            task['id'], 'input-e', task['revision'],
            medications=[{'name': '氨氯地平', 'dose': '10mg', 'action': 'dose_change'}])
        self.assertEqual(receipt['authoritative_writes'], 1)

        stored = self.product.get(task['id'], 'care_task')['review_state']
        written = [item for item in stored['answers']
                   if item['kind'] == 'authoritative_update']
        self.assertTrue(written, '权威修改必须留下可追溯的记录')

        _, _, second = self.run_review(case, [
            {'decision': 'tool', 'tool': 'request_delivery', 'arguments': {}}],
            run_id='run:2', initial_state=stored)
        # 旧报告还在，新报告是**另一个** id，且说明了依据是当前记录被更新。
        self.assertEqual(len(second['review']['reports']), 2)
        self.assertNotEqual(second['review']['reports'][0]['report_id'],
                            second['review']['reports'][1]['report_id'])
        self.assertIn('依据（当前记录）', '\n'.join(second['report']['revision_diff']))
        # 记录真的变了，因此原来那条"不一致"不再成立。
        self.assertTrue(any(item['display_name'] == '氨氯地平'
                            and item['dose'] == '10mg'
                            for item in self.memory.current_medications()))
        superseded = next(item for item in second['review']['findings']
                          if item['finding_id'] == difference['finding_id'])
        self.assertTrue(superseded['stale'])


# ---- F. 一份来源失效或无法读取 -------------------------------------------------

class ScenarioF(_Base):

    def test_an_unreadable_source_is_recorded_and_does_not_support_conclusions(self) -> None:
        case, refs = self.case(COMPLETE_CSV)
        broken = refs['二甲双胍']
        index = _BrokenIndex(self.product, broken)
        _, _, result = self.run_review(case, index=index)
        coverage = {item['requirement_id']: item for item
                    in result['review']['spec']['coverage_requirements']}
        # 读不到的那条：处置是"无法读取"，而且有原因，不冒充"没有差异"。
        self.assertEqual(coverage[f'material_item:{broken}']['disposition'], 'unreadable')
        self.assertTrue(coverage[f'material_item:{broken}']['disposition_reason'])
        # 它不能支撑任何结论。
        self.assertNotIn(broken, result['review']['read_attribution']['system'])
        self.assertNotIn(broken, result['report']['material_refs'])
        # 其余结果照常交付。
        self.assertEqual(coverage[f"material_item:{refs['氨氯地平']}"]['disposition'], 'covered')
        self.assertTrue(any('氨氯地平' in line
                            for line in result['report']['sections']['2. 一致项与主要变化']))
        # 未覆盖范围里点名了它。
        self.assertTrue(any('无法读取' in line or '没有核对到' in line
                            for line in result['report']['sections']['6. 未覆盖范围和执行限制']))

    def test_an_item_that_is_only_in_the_index_is_not_covered(self) -> None:
        """索引里出现过、但从未被读过 → 不是"已覆盖"。"""
        from stage0.review.contract import build_task_spec
        from stage0.review.state import MaterialReviewState
        case, refs = self.case(COMPLETE_CSV)
        facts = self.memory.snapshot()
        spec = build_task_spec(task_id='t', user_goal='g', scope_id='local-demo',
                               selected_case_ids=[case['case_id']],
                               material_items=[(case['case_id'], case['items'])],
                               medications=facts.get('medications') or [],
                               input_versions={'medications': 1, 'semantic': 0, 'materials': 1})
        state = MaterialReviewState(spec)
        state.facts = facts
        state.material_fingerprints = {ref: state.material_fingerprints.get(ref)
                                       for ref in refs.values()}
        # 没有跑过覆盖 pass：没有读取凭据。
        for requirement in state.spec.coverage_requirements:
            if requirement['kind'] == 'material_item':
                requirement['disposition'] = 'insufficient'
                requirement['disposition_reason'] = '测试构造'
        self.assertEqual(state.read_credential(refs['氨氯地平']), 'source_not_read')
        self.assertEqual(state.read_refs(), [])


# ---- G. 模型不可用或请求超时 ---------------------------------------------------

class ScenarioG(_Base):

    def test_a_purely_deterministic_task_completes_without_a_model(self) -> None:
        """模型不可用，但纯确定性的任务已经完成全部必需要求 → **可以 complete**。

        归因必须写明这是系统核对的结果，**不能**宣称"模型已经分析过"。
        """
        case, refs = self.case(CHANGED_CSV, 'k-g')
        _, _, result = self.run_review(case, model=False)
        self.assertCovered(result, refs.values())
        self.assertEqual(result['review']['model_cycles'], 0)
        # 差异照样出现在报告里（那是代码比对出来的，不需要模型）。
        self.assertTrue(result['report']['sections']['3. 差异双方的记录及来源'])
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertEqual(result['report']['gaps'], [])
        # 归因分得清：模型那一栏是零，正文也说明了它是怎么得出来的。
        self.assertEqual(result['report']['attribution']['model']['cycles'], 0)
        self.assertGreater(result['report']['attribution']['system']['field_comparisons'], 0)
        scope_lines = '\n'.join(result['report']['sections']['1. 本次核对目标和覆盖范围'])
        self.assertIn('没有模型参与', scope_lines)
        self.assertIn('确定性字段核对', scope_lines)

    def test_a_semantic_requirement_without_a_model_stays_partial(self) -> None:
        """任务**明确要求**依据资料回答问题时，没做成就是没完成——哪怕字段都核对完了。"""
        case, refs = self.case(COMPLETE_CSV, 'k-g3')
        _, _, result = self.run_review(
            case, model=False,
            requested=[{'kind': 'answer_from_source',
                        'question': '这份材料里的用法与说明书是否一致？',
                        'subjects': ['氨氯地平']}])
        self.assertCovered(result, refs.values())
        self.assertEqual(result['delivery_status'], 'partial')
        self.assertTrue(any(item['code'] == 'requirement_unsatisfied'
                            for item in result['report']['gaps']))
        requirement = next(item for item in result['review']['spec']['delivery_requirements']
                           if item['kind'] == 'answer_from_source')
        self.assertEqual(requirement['status'], 'awaiting_evidence')

    def test_a_provider_error_is_not_reported_as_a_completed_review(self) -> None:
        """供应商故障是一个**暂时**状态：有语义要求时它不能被记成完成。"""
        case, _ = self.case(COMPLETE_CSV, 'k-g2')

        class _Failing:
            proposal_provider = object()

            def __init__(self):
                self.system_prompt = None
                self.prompt_payload = None
                self.tool_definitions = None
                self.tool_schemas = None

            def propose(self, state):
                from stage0.agent import PlannerProposalError
                raise PlannerProposalError('provider_error', 'provider_error', '429')

        _, _, result = self.run_review(
            case, transport=_Failing(),
            requested=[{'kind': 'answer_from_source',
                        'question': '这份材料里的用法与说明书是否一致？'}])
        self.assertTrue(result['degraded_reason'].startswith('provider_error'))
        self.assertEqual(result['delivery_status'], 'partial')
        self.assertEqual(result['review']['model_cycles'], 0)


# ---- H. 无需额外搜索的普通核对 -------------------------------------------------

class ScenarioH(_Base):

    def test_a_plain_material_check_does_not_require_a_search(self) -> None:
        """零次检索不是失败：普通材料核对本来就不要求查说明书。"""
        case, refs = self.case(COMPLETE_CSV)
        _, _, result = self.run_review(case)
        self.assertEqual(result['review']['research_decisions'], [])
        self.assertEqual(result['review']['queries'], [])
        self.assertEqual(result['delivery_status'], 'complete')

    def test_a_user_asked_source_comparison_is_an_actual_requirement(self) -> None:
        """用户**明确要求**的来源调查是一条交付要求，不能被默默省略。

        模型只声明一条问题、没有去查，**不能满足**它——"请用户确认说明书结论"
        不是回答。任务因此停在未完成。
        """
        case, _ = self.case(COMPLETE_CSV)
        _, _, result = self.run_review(
            case,
            [{'decision': 'tool', 'tool': 'submit_question',
              'arguments': {'question_key': 'label_comparison',
                            'text': '请比较材料与说明书中的用法用量是否一致',
                            'subjects': ['氨氯地平'],
                            'direction': 'reference_evidence',
                            'serves_requirement_id': 'user:0:answer_from_source'}}],
            requested=[{'kind': 'answer_from_source',
                        'question': '材料里的用法与说明书是否一致？',
                        'subjects': ['氨氯地平']}])
        requirement = next(item for item in result['review']['spec']['delivery_requirements']
                           if item['kind'] == 'answer_from_source')
        self.assertEqual(requirement['status'], 'awaiting_evidence')
        self.assertEqual(result['delivery_status'], 'partial')

    def test_a_user_answer_cannot_stand_in_for_the_source(self) -> None:
        """用户不是资料的替代品：挂在"应从资料调查"的问题上的请求，用户答了也不关闭。"""
        from stage0.review.state import MaterialReviewState
        case, _ = self.case(COMPLETE_CSV, 'k-h2')
        _, _, result = self.run_review(
            case,
            [{'decision': 'tool', 'tool': 'submit_question',
              'arguments': {'question_key': 'label_comparison',
                            'text': '请比较材料与说明书中的用法用量是否一致',
                            'subjects': ['氨氯地平'],
                            'direction': 'reference_evidence',
                            'serves_requirement_id': 'user:0:answer_from_source'}},
             {'decision': 'tool', 'tool': 'request_information',
              'arguments': {'question_text': '请确认说明书里怎么写的',
                            'subjects': ['氨氯地平'],
                            'related_question_ids': []}}],
            requested=[{'kind': 'answer_from_source',
                        'question': '材料里的用法与说明书是否一致？'}])
        model_requests = [item for item in result['review']['input_requests']
                          if item['origin'] == 'model']
        self.assertTrue(model_requests)
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        self.assertFalse(state.request_answerable_by_user(model_requests[0]['request_id']))

    def test_a_research_action_must_name_the_question_it_serves(self) -> None:
        """研究动作必须说得出它服务于哪个问题——没有归属的检索回答不了
        "这次到底在查什么"。"""
        from stage0.review import capabilities as caps
        from stage0.review.contract import TaskSpec
        from stage0.review.state import MaterialReviewState
        case, _ = self.case(COMPLETE_CSV)
        _, _, result = self.run_review(case)
        state = MaterialReviewState(TaskSpec.from_dict(result['review']['spec']))
        self.assertIn('research_evidence', caps.allowed_review_tools(state))
        self.assertIn('question_id', caps.REVIEW_TOOL_SCHEMAS['research_evidence']['required'])
        # 指向一个不存在的问题被明确拒绝，而不是被当成"查了一下，没查到"。
        self.assertEqual(
            caps.proposal_errors(state, {'tool': 'research_evidence',
                                         'arguments': {'question_id': 'q:nope',
                                                       'query': '氨氯地平'}}),
            ['unknown_question'])


class ReportSelfConsistency(_Base):
    """报告**说自己是什么**，必须和它**实际是什么**一致。

    这两条都是真实批次里暴露出来的：正文写着"交付=尚无报告"而它本身就是一份报告；
    一次被下一步改对的参数笔误被写成"执行限制"。
    """

    def test_the_status_line_matches_the_report_it_is_in(self) -> None:
        case, _ = self.case(INCOMPLETE_CSV, 'k-status')
        _, _, result = self.run_review(case)
        report = result['report']
        status_line = next(line for line in report['sections']['1. 本次核对目标和覆盖范围']
                           if line.startswith('- 本次状态：'))
        self.assertIn('交付=部分报告', status_line)
        self.assertNotIn('尚无报告', status_line)
        self.assertEqual(report['delivery_status'], 'partial')
        # 正文与产物说的是同一件事。
        self.assertIn('交付=部分报告', report['markdown'])

    def test_a_self_corrected_step_is_not_reported_as_a_failure(self) -> None:
        """模型某一步参数不对 → 不进"未覆盖范围和执行限制"；**真故障仍然要写出来**。

        真实批次里就是这样的：一次 ``submit_question`` 参数不完整被拒，模型下一步
        做成了，而报告第 6 节写着"工具 submit_question 未能成功执行
        （invalid_arguments）"——照护者读到的是"这个系统出故障了"。
        """
        from stage0.review.report import section_content
        from stage0.review.state import (ISSUE_CONNECTION, ISSUE_INVALID_ARGUMENTS,
                                         MaterialReviewState)
        case, _ = self.case(COMPLETE_CSV, 'k-corrected')
        _, _, result = self.run_review(case)
        state = MaterialReviewState.restore(result['review'], 'local-demo')
        # 一次参数不完整、**没有执行**的动作：审计记录留着，但正文不写它。
        state.add_issue(operation_ref='submit_question:abc', category=ISSUE_INVALID_ARGUMENTS,
                        remote_outcome='not_executed',
                        user_visible_summary='有一次记下一条待确认的问题的参数不完整，系统没有执行它。')
        # 一次真的执行失败（远端结果未知）：它必须写出来。
        state.add_issue(operation_ref='research:x', category=ISSUE_CONNECTION,
                        user_visible_summary='查一次依据时连接或超时，远端结果未知。')
        limits = section_content(state)['6. 未覆盖范围和执行限制']
        self.assertFalse(any('参数不完整' in line for line in limits),
                         f'没执行过的参数问题不该成为执行限制：{limits}')
        self.assertTrue(any('连接或超时' in line for line in limits),
                        '真正的执行故障必须仍然写出来')
        # 审计记录一条都没少——不写进正文不等于删掉。
        self.assertEqual(len(state.issues), 2)

    def test_an_optional_question_gets_its_own_requirement(self) -> None:
        """可选建议不是"无主的问题"：它有自己的要求记录，且不阻塞原任务。"""
        case, _ = self.case(COMPLETE_CSV, 'k-optional')
        _, _, result = self.run_review(case, [
            {'decision': 'tool', 'tool': 'submit_question',
             'arguments': {'question_key': 'typography', 'text': '剂型是否需要核对',
                           'subjects': ['氨氯地平'], 'direction': 'selected_material',
                           'optional': True}},
        ])
        question = next(item for item in result['review']['questions']
                        if item['question_key'] == 'typography')
        self.assertTrue(question['optional'])
        self.assertTrue(question.get('serves_requirement_id'))
        requirement = next(item for item in result['review']['spec']['delivery_requirements']
                           if item['requirement_id'] == question['serves_requirement_id'])
        self.assertFalse(requirement['required'])
        self.assertEqual(requirement['origin'], 'model_proposed')
        # 一个开放的可选问题**不**让交付变成部分。
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertNotIn('invalid_arguments', result['report']['markdown'])


class RequestPresentation(_Base):
    """补充请求面向用户展示的部分，不展开内部标识。"""

    def test_an_internal_subject_ref_is_shown_as_the_medication_name(self) -> None:
        """模型可以用内部引用指明对象，但用户看到的是药名。

        真实批次里模型就是这样做的（``subjects: ["memory:medication:1@v1"]``），
        页面上原样显示等于把内部标识摊给照护者看。
        """
        case, _ = self.case(COMPLETE_CSV, 'k-subject')
        ref = self.memory.current_medications()[0]['ref']
        self.assertTrue(ref.startswith('memory:medication:'))
        _, _, result = self.run_review(case, [
            {'decision': 'tool', 'tool': 'request_information',
             'arguments': {'question_text': '请确认这一条的剂型与规格',
                           'subjects': [ref], 'required_fields': ['form']}}])
        request = next(item for item in result['review']['input_requests']
                       if item['origin'] == 'model')
        self.assertEqual(request['subjects'], ['二甲双胍'])
        self.assertEqual(request['target']['subjects'], ['二甲双胍'])
        self.assertNotIn('memory:medication', json.dumps(result['report']['input_requests'],
                                                         ensure_ascii=False))
        # 展示换了，判断没换：请求仍然挂在真实对象上，仍然挡着它该挡的问题。
        self.assertTrue(request['request_id'])


# ---- 覆盖的有界性与可恢复性 ---------------------------------------------------

class CoverageMechanics(_Base):
    """长材料按页读、崩溃恢复不重复、材料改版只失效受影响的部分。"""

    LONG_CSV = ('name,dose,unit,schedule,date,subject,route,form,strength\n'
                '氨氯地平,5,mg,' + ('每日一次' * 120) + ',2026-09-07,local-demo,口服,片,5mg\n')

    def test_a_long_material_is_read_in_bounded_pages(self) -> None:
        """原文不能一次全灌进来：按页读，进度留下，最后仍然读完。"""
        from stage0.review import coverage as coverage_mod
        case, refs = self.case(self.LONG_CSV, 'k-long')
        original = coverage_mod.PAGE_CHARS
        coverage_mod.PAGE_CHARS = 200
        try:
            _, _, result = self.run_review(case)
        finally:
            coverage_mod.PAGE_CHARS = original
        review = result['review']
        self.assertGreaterEqual(len(review['coverage_runs']), 2, '长材料应当分多趟读完')
        record = review['source_reads'][refs['氨氯地平']]
        self.assertGreater(record['chars_total'], 200, '构造的材料必须长于单页')
        self.assertEqual(record['chars_read'], record['chars_total'], '最终应当读完整条')
        self.assertEqual(review['coverage_progress']['items_pending'], 0)
        self.assertEqual(review['coverage_progress']['truncated'], [])

    def test_a_page_budget_that_runs_out_is_reported_not_hidden(self) -> None:
        """一趟读不完时，**明确记成截断与未处理**，不是当成"没有差异"。"""
        from stage0.review import coverage as coverage_mod
        from stage0.review.contract import build_task_spec
        from stage0.review.state import MaterialReviewState
        case, refs = self.case(self.LONG_CSV, 'k-trunc')
        facts = self.memory.snapshot()
        spec = build_task_spec(task_id='t', user_goal='g', scope_id='local-demo',
                               selected_case_ids=[case['case_id']],
                               material_items=[(case['case_id'], case['items'])],
                               medications=facts.get('medications') or [],
                               input_versions={'medications': 1, 'semantic': 0, 'materials': 1})
        state = MaterialReviewState(spec)
        state.facts = facts
        original = (coverage_mod.PAGE_CHARS, coverage_mod.MAX_PAGES_PER_ITEM)
        coverage_mod.PAGE_CHARS, coverage_mod.MAX_PAGES_PER_ITEM = 50, 1
        try:
            run = coverage_mod.run_coverage_pass(state, MaterialIndex(self.product),
                                                 batch_limit=1, at='now')
        finally:
            coverage_mod.PAGE_CHARS, coverage_mod.MAX_PAGES_PER_ITEM = original
        # 读到的部分是真的（凭据成立），没读完的部分被**明确**记下来。
        self.assertTrue(state.read_credential(refs['氨氯地平']))
        self.assertTrue(state.source_reads[refs['氨氯地平']]['truncated'])
        self.assertIn(refs['氨氯地平'], run['truncated'])
        self.assertIn(refs['氨氯地平'], run['unprocessed'])
        progress = state.coverage_progress()
        self.assertIn(refs['氨氯地平'], progress['truncated'])
        self.assertIn(refs['氨氯地平'], progress['unprocessed'])

    def test_a_second_pass_does_not_redo_finished_work(self) -> None:
        """崩溃恢复不会重复已经完成的有效覆盖。"""
        case, refs = self.case(COMPLETE_CSV, 'k-resume')
        _, runner, first = self.run_review(case)
        second = runner.advance(task_id='task:1', run_id='run:2', goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[case['case_id']],
                                review_index=MaterialIndex(self.product),
                                initial_state=first['review'])
        last = second['review']['coverage_runs'][-1]
        self.assertEqual(last['processed'], [], '重跑不该重复处理已经读过的条目')
        self.assertEqual(last['skipped'], len(refs))
        self.assertEqual(second['review']['read_attribution']['system'],
                         first['review']['read_attribution']['system'])

    def test_a_changed_record_invalidates_only_the_items_it_touches(self) -> None:
        """比较对象变了以后，**只有受影响的那一条**需要重新读取。"""
        case, refs = self.case(CHANGED_CSV, 'k-version')
        _, runner, first = self.run_review(case)
        self.assertEqual(first['review']['coverage_progress']['items_pending'], 0)
        # 权威记录更新（走既有的受控写入路径）：只有氨氯地平那一条变了。
        self.memory.apply_medication_change(action='dose_change', name='氨氯地平',
                                            ingredients=[], session_id='s', turn_id='t2',
                                            source='caregiver', dose='10mg')
        second = runner.advance(task_id='task:1', run_id='run:2', goal='核对材料与当前记录',
                                scope_id='local-demo', selected_case_ids=[case['case_id']],
                                review_index=MaterialIndex(self.product),
                                initial_state=first['review'])
        review = second['review']
        touched = review['read_attribution']['system']
        self.assertIn(refs['氨氯地平'], touched)
        self.assertIn(refs['二甲双胍'], touched, '未受影响的条目仍然可用，不该被丢掉')
        # 未受影响的那一条指纹没变 → 它没有被重新处理。
        self.assertEqual(review['material_fingerprints'][refs['二甲双胍']],
                         first['review']['material_fingerprints'][refs['二甲双胍']])
        self.assertGreaterEqual(review['coverage_runs'][-1]['skipped'], 1)
        self.assertEqual(review['coverage_progress']['items_pending'], 0)


if __name__ == '__main__':
    unittest.main()
