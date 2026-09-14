"""回访记录：起因判定、开始/继续、候选变更、结果渲染。

这一套只测**回访这一层**（`review_visits.py`）：它自己不算临床、不改权威记录，
只把"这次回访是什么、走到哪了、结论是什么"记下来。所以这里反复出现的一条断言是
**候选在被确认之前，权威记录一个字节都不动**。
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from stage0 import review_visits as rv
from stage0 import safety_cases as sc
from stage0.memory import MemoryStore
from stage0.product import ProductError, ProductStore


class _VisitFixture(unittest.TestCase):
    """隔离库 + 一件真实事项。沿用 `test_safety_cases.py` 的脚手架。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='review-visit-')
        self.memory = MemoryStore(pathlib.Path(self.tmp.name) / 'memory.db',
                                  llm_enabled=False)
        self.product = ProductStore(self.memory)
        self.cases = sc.SafetyCaseStore(self.product)
        self.visits = rv.ReviewVisitStore(self.product)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def add_drug(self, name, *, dose='5mg', key=None):
        return self.memory.apply_medication_change(
            action='add', name=name, ingredients=[], session_id='s',
            turn_id=key or name, source='caregiver', dose=dose)['medication']

    def open_case(self) -> dict:
        first = self.add_drug('合成药甲', key='v1')
        second = self.add_drug('合成药乙', key='v2')
        text = '合成药甲 × 合成药乙：出血风险升高。'
        conclusion = self.memory.record_conclusion(
            session_id='s', turn_id='c1', kind='warning', text=text,
            memory_refs=[first['ref'], second['ref']],
            source_refs=[{'uri': 'label://x', 'text': text}])
        return self.cases.get(sc.observe_conclusion(self.product, conclusion, key='k1')['id'])

    def start(self, case=None, *, key='visit-1', actor='caregiver') -> dict:
        case = case or self.open_case()
        reason = rv.derive_reason(self.product, case)
        visit, _ = self.visits.open_or_continue(key, case_id=case['id'], reason=reason,
                                                actor=actor,
                                                history_length=len(case.get('history') or []))
        return visit


class OpenOrContinueTests(_VisitFixture):
    def test_a_second_start_continues_the_same_visit(self):
        """"继续本次跟进"必须**接着**那一次走，不新开一访。

        新开一次会让用户已经答过的问题变成上一访的遗留，他回来看到的第一题
        又是原来那道。
        """
        case = self.open_case()
        first = self.start(case, key='visit-a')
        second, created = self.visits.open_or_continue(
            'visit-b', case_id=case['id'],
            reason=rv.derive_reason(self.product, case), actor='caregiver',
            history_length=len(case.get('history') or []))
        self.assertFalse(created, '已经有未结束的回访，不该新开一次')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['opened_at'], second['opened_at'], '继续不改开始时间')
        self.assertEqual(1, len(self.visits.for_case(case['id'])))

    def test_a_later_fact_upgrades_the_reason(self):
        """上次判成"用户主动发起"，这中间安排到期了 —— 更强的理由要覆盖它。"""
        case = self.open_case()
        self.start(case, key='visit-1')
        due = {'kind': rv.REASON_DUE, 'refs': [case['id']], 'detail': '已确认的安排到期'}
        upgraded, created = self.visits.open_or_continue(
            'visit-2', case_id=case['id'], reason=due, actor='caregiver')
        self.assertFalse(created)
        self.assertEqual(rv.REASON_DUE, upgraded['reason']['kind'])

    def test_a_weaker_reason_does_not_overwrite_a_stronger_one(self):
        """反过来不行：到期不能被"用户随手点进来"盖掉。"""
        case = self.open_case()
        due = {'kind': rv.REASON_DUE, 'refs': [], 'detail': '已确认的安排到期'}
        self.visits.open_or_continue('visit-1', case_id=case['id'], reason=due,
                                     actor='caregiver')
        weak = {'kind': rv.REASON_USER_STARTED, 'refs': [], 'detail': '用户主动发起'}
        after, _ = self.visits.open_or_continue('visit-2', case_id=case['id'],
                                                reason=weak, actor='caregiver')
        self.assertEqual(rv.REASON_DUE, after['reason']['kind'])

    def test_the_cursor_points_at_the_previous_visit_end(self):
        case = self.open_case()
        first = self.start(case, key='visit-1')
        self.visits.save_result(first['id'], {'why': {}}, cursor_after=3)
        self.visits.set_status(first['id'], rv.STATUS_COMPLETED)
        second = self.start(case, key='visit-2')
        self.assertEqual(3, second['cursor']['before'],
                         '下一次回访的"上次之后"要从上一访结束的位置起算')
        self.assertEqual(first['id'], second['previous_visit_id'])


class ReasonTests(_VisitFixture):
    def test_a_due_arrangement_outranks_everything(self):
        case = self.open_case()
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_MONITORING, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver',),
            follow_up={'kind': 'review_at', 'at': '2020-01-01T00:00:00+00:00',
                       'owner': 'caregiver'})
        # 让它真的走到 due：扫一遍把它登记成待触发。
        from stage0 import followup_runtime as fr
        fr.scan_follow_ups(self.memory, self.product)
        reason = rv.derive_reason(self.product, self.cases.get(monitored['id']))
        self.assertEqual(rv.REASON_DUE, reason['kind'], reason)

    def test_a_record_change_is_reported_as_a_scope_change(self):
        case = self.open_case()
        self.memory.apply_medication_change(action='add', name='合成药丙', ingredients=[],
                                            session_id='s', turn_id='r1',
                                            source='caregiver', dose='1mg')
        reason = rv.derive_reason(self.product, case)
        self.assertEqual(rv.REASON_RECORD_CHANGE, reason['kind'], reason)
        self.assertIn('用药记录', reason['detail'])

    def test_nothing_new_is_reported_as_user_started_not_as_stability(self):
        case = self.open_case()
        reason = rv.derive_reason(self.product, self.cases.get(case['id']))
        self.assertEqual(rv.REASON_USER_STARTED, reason['kind'], reason)
        self.assertIn('没有发现新的变化', reason['detail'])


class CandidateTests(_VisitFixture):
    def test_a_candidate_does_not_touch_the_authoritative_record(self):
        """候选**只是候选**：登记它之后当前药单一个字节都不变。"""
        case = self.open_case()
        visit = self.start(case)
        before = {(m['display_name'], m['dose'])
                  for m in self.memory.current_medications()}
        self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                  before='5mg', after='10mg',
                                  source=rv.SOURCE_MODEL_PROPOSED,
                                  basis={'kind': 'model_explanation', 'refs': []})
        after = {(m['display_name'], m['dose'])
                 for m in self.memory.current_medications()}
        self.assertEqual(before, after, '候选未经确认就改了权威记录')
        pending = self.visits.pending_candidates(visit['id'])
        self.assertEqual(1, len(pending))
        self.assertEqual('10mg', pending[0]['after'])

    def test_the_two_sources_are_recorded_distinctly(self):
        """用户声明的和模型提议的必须分得开——确认的人要知道自己在确认什么。"""
        case = self.open_case()
        visit = self.start(case)
        self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                  before='5mg', after='10mg',
                                  source=rv.SOURCE_USER_DECLARED,
                                  basis={'kind': 'user_report', 'refs': []})
        self.visits.add_candidate(visit['id'], name='合成药乙', field='schedule',
                                  before=None, after='每日一次',
                                  source=rv.SOURCE_MODEL_PROPOSED,
                                  basis={'kind': 'model_explanation', 'refs': []})
        pending = self.visits.pending_candidates(visit['id'])
        self.assertEqual({rv.SOURCE_USER_DECLARED, rv.SOURCE_MODEL_PROPOSED},
                         {c['source'] for c in pending})

    def test_one_pending_candidate_per_field_is_replaced_not_queued(self):
        """同一味药的同一字段只留一条待确认：两条并存，确认一条之后另一条就对不上了。"""
        case = self.open_case()
        visit = self.start(case)
        for value in ('10mg', '15mg'):
            self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                      before='5mg', after=value,
                                      source=rv.SOURCE_USER_DECLARED,
                                      basis={'kind': 'user_report', 'refs': []})
        pending = self.visits.pending_candidates(visit['id'])
        self.assertEqual(1, len(pending))
        self.assertEqual('15mg', pending[0]['after'])

    def test_a_decided_candidate_cannot_be_decided_again(self):
        case = self.open_case()
        visit = self.start(case)
        self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                  before='5mg', after='10mg',
                                  source=rv.SOURCE_USER_DECLARED,
                                  basis={'kind': 'user_report', 'refs': []})
        candidate = self.visits.pending_candidates(visit['id'])[0]
        self.visits.decide_candidate(visit['id'], candidate['id'],
                                     status=rv.CANDIDATE_CONFIRMED, actor='caregiver')
        with self.assertRaises(ProductError):
            self.visits.decide_candidate(visit['id'], candidate['id'],
                                         status=rv.CANDIDATE_DISMISSED, actor='caregiver')

    def test_only_checkable_fields_can_be_proposed(self):
        case = self.open_case()
        visit = self.start(case)
        with self.assertRaises(ProductError):
            self.visits.add_candidate(visit['id'], name='合成药甲', field='自由文本',
                                      before=None, after='随便什么',
                                      source=rv.SOURCE_USER_DECLARED,
                                      basis={'kind': 'user_report', 'refs': []})

    def test_an_unknown_source_is_refused(self):
        case = self.open_case()
        visit = self.start(case)
        with self.assertRaises(ProductError):
            self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                      before='5mg', after='10mg', source='看起来像用户',
                                      basis={'kind': 'user_report', 'refs': []})


class ResultTests(_VisitFixture):
    def _result(self, visit, case=None):
        case = case or self.cases.get(visit['case_id'])
        return rv.render_result(self.product, case, self.visits.get(visit['id']))

    def test_no_new_records_states_the_information_state_only(self):
        """没有新记录只能说"系统尚未收到"，**不能**说成情况稳定或风险解除。

        这两句话差别很大：前者是系统的信息状态，后者是一句没有人做过的判断。

        判据要能区分"做出这个判断"和"明确否认这个判断"——否定句里当然会出现
        那些词。所以：那句话自己必须带着明确的否定，**其余任何地方**都不许出现
        这些说法。
        """
        visit = self.start()
        result = self._result(visit)
        texts = [line['text'] for line in result['since_last']]
        self.assertIn(rv.NO_NEW_RECORDS, texts)

        # 那句话必须是"否认"形态，不是含糊其辞。
        self.assertTrue(any(marker in rv.NO_NEW_RECORDS
                            for marker in ('不等于', '不表示', '不能读成')),
                        f'这句话没有把边界说清楚：{rv.NO_NEW_RECORDS}')

        # 除了它自己，结果里任何地方都不许出现这些判断。
        others = {key: value for key, value in result.items() if key != 'since_last'}
        blob = str(others)
        for forbidden in ('情况稳定', '风险已解除', '风险解除', '已经稳定'):
            self.assertNotIn(forbidden, blob,
                             f'回访结果里出现了没有人做过的判断：{forbidden}')

    def test_a_first_visit_is_marked_as_such(self):
        """没有上一次就别说"相对上次"——那是假话。"""
        visit = self.start()
        self.assertTrue(self._result(visit)['first_visit'])

    def test_the_next_arrangement_carries_its_confirmation_state(self):
        """下一次安排必须连**确认状态**一起给：已安排 ≠ 已确认。"""
        case = self.open_case()
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_MONITORING, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver',),
            follow_up={'kind': 'review_at', 'at': '2999-01-01T00:00:00+00:00',
                       'owner': 'caregiver'})
        visit = self.start(self.cases.get(monitored['id']), key='visit-due')
        arrangement = self._result(visit, self.cases.get(monitored['id']))['next_arrangement']
        self.assertTrue(arrangement['present'])
        self.assertFalse(arrangement['confirmed'], '给了时间不等于有人确认过')
        self.assertEqual('scheduled', arrangement['schedule_state'])

    def test_no_arrangement_says_so_instead_of_inventing_one(self):
        visit = self.start()
        arrangement = self._result(visit)['next_arrangement']
        self.assertFalse(arrangement['present'])
        self.assertFalse(arrangement['confirmed'])

    def test_every_statement_says_what_its_basis_is(self):
        """程序核对的、用户报的、模型的解释必须分得开。"""
        case = self.open_case()
        visit = self.start(case)
        self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                  before='5mg', after='10mg',
                                  source=rv.SOURCE_USER_DECLARED,
                                  basis={'kind': 'user_report', 'refs': []})
        result = self._result(visit)
        kinds = {line['basis']['kind'] for line in result['statements']}
        self.assertTrue(kinds, result['statements'])
        self.assertTrue(kinds <= {'program_check', 'user_report', 'model_explanation',
                                 'record'}, kinds)
        for group in ('since_last', 'unresolved', 'statements'):
            for line in result[group]:
                self.assertIn(line['basis']['kind'],
                              {'program_check', 'user_report', 'model_explanation',
                               'record'}, (group, line))

    def test_a_pending_candidate_is_unresolved_and_labelled_by_its_source(self):
        case = self.open_case()
        visit = self.start(case)
        self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                  before='5mg', after='10mg',
                                  source=rv.SOURCE_MODEL_PROPOSED,
                                  basis={'kind': 'model_explanation', 'refs': []})
        result = self._result(visit)
        pending = [line for line in result['unresolved']
                   if '待您确认的变更' in line['text']]
        self.assertTrue(pending, result['unresolved'])
        self.assertEqual('model_explanation', pending[0]['basis']['kind'],
                         '模型提议的候选不能看起来像用户说的')

    def test_a_confirmed_candidate_shows_up_as_a_completed_action(self):
        case = self.open_case()
        visit = self.start(case)
        self.visits.add_candidate(visit['id'], name='合成药甲', field='dose',
                                  before='5mg', after='10mg',
                                  source=rv.SOURCE_USER_DECLARED,
                                  basis={'kind': 'user_report', 'refs': []})
        candidate = self.visits.pending_candidates(visit['id'])[0]
        self.visits.decide_candidate(visit['id'], candidate['id'],
                                     status=rv.CANDIDATE_CONFIRMED, actor='caregiver',
                                     applied={'name': '合成药甲', 'dose': '10mg'})
        result = self._result(visit)
        actions = [line for line in result['actions'] if '10mg' in line['text']]
        self.assertTrue(actions, result['actions'])
        self.assertNotIn('待您确认的变更',
                         str([line['text'] for line in result['unresolved']]))


class ModelProposalTests(unittest.TestCase):
    """模型侧只**提议**、绝不写入。这条边界由工具层与采纳层两道闸门共同守着。"""

    def setUp(self):
        from stage0.harness.evidence import EvidenceStore
        from stage0.harness.runtime import Principal, RunContext
        from stage0.harness.tools import ToolExecutor
        from stage0 import investigation
        self.tmp = tempfile.TemporaryDirectory(prefix='propose-')
        self.memory = MemoryStore(pathlib.Path(self.tmp.name) / 'memory.db',
                                  llm_enabled=False)
        self.evidence = EvidenceStore(self.memory.connection, scope_id='local-demo')
        self.executor = ToolExecutor(memory=self.memory)
        from stage0.harness.default_tools import PROPOSE_MEDICATION_CHANGE_SPEC
        self.executor.register(PROPOSE_MEDICATION_CHANGE_SPEC,
                               lambda request: {'submitted': request.arguments,
                                                'status': 'pending_adoption'})
        self.ctx = RunContext(run_id='r1', turn_id='t1', principal=Principal())
        self.inv = investigation.InvestigationState('核查', 'local-demo')
        self.inv.policy = 'safety_case'
        # 规划范围只认权威记录里真有的药名——先把它建出来，否则声明子问题时
        # 会被 `subquestion_entity_not_in_scope` 挡下（那条闸门是对的）。
        self.memory.apply_medication_change(action='add', name='合成药甲', ingredients=[],
                                            session_id='s', turn_id='a', source='caregiver',
                                            dose='5mg')
        self.inv.sync_authority(self.memory)
        self.inv.authority_read = True
        self.inv.sync_authority(self.memory)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def _declare_question(self) -> str:
        inv = self.inv
        existing = [{'statement': q['statement'],
                     'information_target': q.get('information_target'),
                     'strategy': q.get('strategy'), 'subject_refs': q.get('subject_refs'),
                     'target_field': q.get('target_field')} for q in inv.questions]
        errors = inv.accept_questions([*existing, {
            'statement': '合成药甲现在的剂量是多少？',
            'information_target': 'patient_actual_state', 'strategy': 'ask_user',
            'subject_refs': ['合成药甲'], 'target_field': 'dose', 'why': '影响判断'}])
        assert errors == [], errors
        return inv.questions[-1]['question_id']

    def test_the_field_list_is_the_same_everywhere(self):
        """工具 schema、调查校验、确认时的写入——三处必须认**同一组**字段。

        任何一处漂移，模型按工具描述产出的候选都会被另一处拒掉，而拒的理由
        看起来像"模型不会用工具"。
        """
        from stage0 import investigation
        from stage0.harness.default_tools import PROPOSE_MEDICATION_CHANGE_SPEC
        schema_fields = tuple(PROPOSE_MEDICATION_CHANGE_SPEC.argument_schema[
            'properties']['field']['enum'])
        self.assertEqual(rv.CANDIDATE_FIELDS, investigation.CHANGE_CANDIDATE_FIELDS)
        self.assertEqual(rv.CANDIDATE_FIELDS, schema_fields)

    def test_the_tool_records_a_candidate_and_never_writes(self):
        """模型提议一次变更：候选记下来了，**当前药单一个字节都没变**。"""
        from stage0.agent import Observation
        self.memory.apply_medication_change(action='add', name='合成药甲', ingredients=[],
                                            session_id='s', turn_id='a', source='caregiver',
                                            dose='5mg')
        self.inv.sync_authority(self.memory)
        question_id = self._declare_question()
        before = [(m['display_name'], m['dose'])
                  for m in self.memory.current_medications()]

        arguments = {'question_id': question_id, 'name': '合成药甲', 'field': 'dose',
                     'value': '10mg', 'quote': '我已经加到10mg了'}
        result = self.executor.execute(self.ctx, 'propose_medication_change', arguments,
                                       state=None)
        self.assertTrue(result.ok, result.error)
        observation = Observation('propose_medication_change', '记为待确认候选',
                                  arguments, result.value, True, gap_id=question_id)
        self.inv.observe(observation, self.evidence)

        candidates = self.inv.pending_change_candidates
        self.assertEqual(1, len(candidates), candidates)
        self.assertEqual('10mg', candidates[0]['value'])
        after = [(m['display_name'], m['dose'])
                 for m in self.memory.current_medications()]
        self.assertEqual(before, after, '模型提议的变更改了权威记录')

    def test_an_uncheckable_field_is_refused_at_the_proposal_layer(self):
        """不可核对的字段在**提案层**就被挡下，连观察都不会产生。"""
        from stage0 import investigation
        question_id = self._declare_question()
        errors = investigation.proposal_errors(self.inv, {
            'decision': 'tool', 'tool': 'propose_medication_change',
            'gap_id': question_id, 'expected_observation': '记为候选',
            'arguments': {'question_id': question_id, 'name': '合成药甲',
                          'field': '心情', 'value': '好多了'}})
        self.assertIn('change_field_not_checkable', errors)

    def test_proposing_requires_an_open_question(self):
        """没有在问的问题就提候选 = 凭空发起一次改药。"""
        from stage0 import investigation
        self.assertNotIn('propose_medication_change',
                         investigation.allowed_tools(self.inv))
        self._declare_question()
        self.assertIn('propose_medication_change',
                      investigation.allowed_tools(self.inv))


if __name__ == '__main__':
    unittest.main()
