"""计划采纳—行动执行—反馈修订：契约层验证。

锁的是这一轮修的断点，不是实现细节：

* 一次采纳必须是**完整的状态迁移**（缺口关闭、工具列表收敛、模型看到真实结果）；
* 问题的**身份**与**取证来源**分开，换来源不换问题、不删未决项；
* 执行结果、信息状态、事项状态三者不互相冒充。
"""
import unittest

from stage0.investigation import (
    digest, InvestigationState, allowed_tools, gap_closing_tools, proposal_errors,
    question_id_for, policy_of, strategy_can_serve, is_question_answered,
    question_blocks,
    TARGET_PATIENT_STATE, TARGET_GENERAL_REFERENCE, TARGET_PROFESSIONAL,
    STRATEGY_ASK_USER, STRATEGY_GENERAL_REFERENCE, STRATEGY_PATIENT_MATERIAL,
    STRATEGY_PROFESSIONAL_REVIEW,
    INFO_NOT_ATTEMPTED, INFO_SOURCE_LIMITED, INFO_AVAILABLE,
    INFO_ATTEMPTED_NO_RESULT,
    INFO_RECEIVED_UNCONFIRMED, STRATEGY_PATIENT_RECORD,
    QUESTION_STATUS_OPEN, QUESTION_STATUS_ANSWERED, QUESTION_STATUS_UNAVAILABLE,
    GAP_PLAN,
)


class _Memory:
    """最小解析器替身：只承认它真的有的对象。"""

    def __init__(self, known=()):
        self.known = set(known)

    def resolve_ref(self, ref):
        if ref not in self.known:
            raise ValueError(f'unknown memory item for ref: {ref}')
        return {'layer': 'conclusion', 'row': {'id': 1}}


def case_state(*, meds=('合成药甲', '合成药乙'), policy='safety_case', authority=True):
    inv = InvestigationState('跟进一件药物相互作用风险安全事项。', 'local-demo')
    inv.policy = policy
    inv.authority_read = authority
    inv.facts = {'medications': [{'display_name': name} for name in meds]}
    inv.memory = _Memory()
    return inv


def as_declaration(statement, target, subject_refs, strategy=None, **extra):
    payload = {'statement': statement, 'information_target': target,
               'subject_refs': list(subject_refs)}
    if strategy:
        payload['strategy'] = strategy
    payload.update(extra)
    return payload


def declare(inv, **question):
    """声明一条问题——连同已声明的一起提交（声明是一次修订）。"""
    payload = as_declaration(question.pop('statement'), question.pop('target'),
                             question.pop('subject_refs'), **question)
    existing = [as_declaration(q['statement'], q.get('information_target'),
                               q.get('subject_refs'), q.get('strategy'),
                               target_field=q.get('target_field'))
                for q in inv.questions]
    return inv.accept_questions([*existing, payload])


class PlanAdoptionTests(unittest.TestCase):
    def test_one_adoption_is_a_complete_state_transition(self):
        """采纳之后：问题集定了、首次规划缺口关了、工具列表收敛了。"""
        inv = case_state()
        self.assertEqual('first_plan', inv.revision_trigger(),
                         '采纳之前确实处在"首次规划"阶段')
        self.assertIn('plan_questions', allowed_tools(inv))

        self.assertEqual([], declare(inv, statement='合成药乙的服用频次是什么？',
                                     target=TARGET_PATIENT_STATE,
                                     subject_refs=['合成药乙'], target_field='schedule'))
        self.assertTrue(inv.questions_settled)
        self.assertIsNone(inv.revision_trigger(),
                          '首次规划阶段必须真正结束，否则工具列表一直鼓励重复规划')
        self.assertNotIn('plan_questions', allowed_tools(inv),
                         '已经采纳过的计划不该继续被邀请重交一遍')
        # 首次规划缺口必须真的**关掉**（而不是只把布尔字段置真，缺口还开着）。
        open_plan_gaps = [g for g in inv.gaps
                          if g['status'] == 'open' and g['kind'] == 'plan_missing']
        self.assertEqual([], open_plan_gaps, open_plan_gaps)

    def test_the_model_sees_the_real_adoption_outcome_and_ids(self):
        """工具结果必须是真的：采纳了哪些、question_id 是什么。"""
        inv = case_state()
        outcome = inv.adopt_questions([{
            'statement': '合成药乙的服用频次是什么？',
            'information_target': TARGET_PATIENT_STATE, 'strategy': STRATEGY_ASK_USER,
            'subject_refs': ['合成药乙'], 'target_field': 'schedule',
            'why': '用法影响判断', 'basis_refs': [],
        }])
        self.assertEqual(1, len(outcome['added']))
        question_id = outcome['added'][0]
        self.assertTrue(question_id.startswith('q:'))
        self.assertEqual(question_id, outcome['questions'][0]['question_id'])
        self.assertEqual(STRATEGY_ASK_USER, outcome['questions'][0]['strategy'])
        self.assertEqual(INFO_NOT_ATTEMPTED, outcome['questions'][0]['information_state'])
        self.assertTrue(outcome['questions'][0]['blocking'])

    def test_a_rejected_plan_is_not_reported_as_adopted(self):
        inv = case_state()
        errors = declare(inv, statement='随便问问', target='made_up',
                         subject_refs=['合成药甲'])
        self.assertEqual(['unknown_information_target'], errors)
        self.assertEqual([], inv.questions, '被拒的计划不留下任何问题')

    def test_the_same_plan_cannot_be_replayed_without_a_reason(self):
        """没有新信息时重复提交同一份计划：不重复创建、不计为进展。"""
        inv = case_state()
        declare(inv, statement='频次？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule')
        before = [q['question_id'] for q in inv.questions]
        self.assertIsNone(inv.revision_trigger())
        self.assertEqual([], [g for g in inv.gaps if g['status'] == 'open'
                              and g['kind'] == 'plan_missing'])
        outcome = inv.adopt_questions([{
            'statement': '频次？', 'information_target': TARGET_PATIENT_STATE,
            'strategy': STRATEGY_ASK_USER, 'subject_refs': ['合成药乙'],
            'target_field': 'schedule', 'why': None, 'basis_refs': [],
        }])
        self.assertEqual([], outcome['added'], '重复提交不新增问题')
        self.assertEqual(before, [q['question_id'] for q in inv.questions])


class SourceCapabilityTests(unittest.TestCase):
    def test_general_reference_cannot_prove_what_this_patient_actually_takes(self):
        """来源能力：一般药品资料说不了"这位用户实际怎么吃"。"""
        self.assertFalse(strategy_can_serve(TARGET_PATIENT_STATE,
                                            STRATEGY_GENERAL_REFERENCE))
        self.assertTrue(strategy_can_serve(TARGET_PATIENT_STATE, STRATEGY_ASK_USER))
        self.assertTrue(strategy_can_serve(TARGET_GENERAL_REFERENCE,
                                           STRATEGY_GENERAL_REFERENCE))
        inv = case_state()
        errors = declare(inv, statement='这位患者的服用频次是什么？',
                         target=TARGET_PATIENT_STATE,
                         subject_refs=['合成药乙'], target_field='schedule',
                         strategy=STRATEGY_GENERAL_REFERENCE)
        self.assertEqual(['strategy_cannot_serve_target'], errors)
        self.assertEqual([], inv.questions)

    def test_the_two_targets_stay_two_questions(self):
        """"这位用户实际怎样服用"与"资料记载的一般用法"是不同目标。"""
        patient = question_id_for(TARGET_PATIENT_STATE, ['合成药乙'], 'schedule')
        reference = question_id_for(TARGET_GENERAL_REFERENCE, ['合成药乙'], 'schedule')
        self.assertNotEqual(patient, reference)

    def test_a_reference_must_really_exist_in_scope(self):
        """不能靠 ``memory:`` 前缀冒充合法引用。"""
        inv = case_state()
        errors = declare(inv, statement='这条结论还成立吗？',
                         target=TARGET_GENERAL_REFERENCE,
                         subject_refs=['memory:conclusion:999@v1'])
        self.assertEqual(['subquestion_entity_not_in_scope'], errors,
                         '不存在的引用必须被拒，而不是看到前缀就放行')


class StrategyChangeTests(unittest.TestCase):
    def test_changing_the_source_keeps_the_same_question_and_its_history(self):
        """同一问题从查材料转向问用户：ID 不变，历史留痕。"""
        inv = case_state()
        declare(inv, statement='合成药乙实际怎么服用？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule',
                strategy=STRATEGY_PATIENT_MATERIAL)
        original = inv.questions[0]['question_id']
        inv.questions[0]['information_state'] = INFO_SOURCE_LIMITED
        inv.questions[0]['attempts'].append({'tool': 'read_material_item',
                                             'information_state': INFO_SOURCE_LIMITED})
        errors = declare(inv, statement='合成药乙实际怎么服用？', target=TARGET_PATIENT_STATE,
                         subject_refs=['合成药乙'], target_field='schedule',
                         strategy=STRATEGY_ASK_USER, why='材料里没有记录')
        self.assertEqual([], errors, '换来源不能被判成"删掉了一个未决问题"')
        self.assertEqual(1, len(inv.questions))
        self.assertEqual(original, inv.questions[0]['question_id'])
        self.assertEqual(STRATEGY_ASK_USER, inv.questions[0]['strategy'])
        history = inv.questions[0]['strategy_history']
        self.assertEqual(1, len(history))
        self.assertEqual(STRATEGY_PATIENT_MATERIAL, history[0]['from'])
        self.assertEqual(STRATEGY_ASK_USER, history[0]['to'])
        self.assertEqual(1, len(inv.questions[0]['attempts']),
                         '旧尝试仍在，换来源没把记录抹掉')
        self.assertEqual(INFO_NOT_ATTEMPTED, inv.questions[0]['information_state'],
                         '换了来源，"这条来源取不到"的结论不再适用')

    def test_a_source_change_is_a_legal_revision_trigger(self):
        inv = case_state()
        declare(inv, statement='实际怎么服用？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule',
                strategy=STRATEGY_PATIENT_MATERIAL)
        self.assertIsNone(inv.revision_trigger(), '没有理由时不能重写计划')
        inv.questions[0]['information_state'] = INFO_SOURCE_LIMITED
        self.assertEqual('strategy_needs_change', inv.revision_trigger())

    def test_a_professional_question_has_no_closing_tool_and_waits_for_review(self):
        inv = case_state()
        declare(inv, statement='这个剂量是否需要调整？', target=TARGET_PROFESSIONAL,
                subject_refs=['合成药甲'], strategy=STRATEGY_PROFESSIONAL_REVIEW)
        gap = next(g for g in inv.gaps if g['kind'] == 'question_open')
        self.assertEqual([], gap_closing_tools(inv, gap),
                         '没有工具能关它——空列表是真答案')
        self.assertEqual('waiting_review', inv._forced_stop_typed())

    def test_a_user_question_makes_clarification_available(self):
        inv = case_state()
        self.assertNotIn('ask_clarification', allowed_tools(inv))
        declare(inv, statement='频次？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule',
                strategy=STRATEGY_ASK_USER)
        self.assertIn('ask_clarification', allowed_tools(inv))

    def test_the_model_may_ask_in_its_own_words(self):
        inv = case_state()
        declare(inv, statement='合成药乙是什么时候开始的？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='start_date')
        qid = inv.questions[0]['question_id']
        proposal = {'decision': 'tool', 'tool': 'ask_clarification', 'gap_id': qid,
                    'expected_observation': '用户给出开始时间',
                    'arguments': {'question_id': qid,
                                  'question': '麻烦问一下，这个药大概是从什么时候开始吃的？'}}
        self.assertEqual([], proposal_errors(inv, proposal))

    def test_asking_about_a_question_whose_source_is_not_the_user_is_refused(self):
        inv = case_state()
        declare(inv, statement='资料怎么说？', target=TARGET_GENERAL_REFERENCE,
                subject_refs=['合成药甲'], strategy=STRATEGY_GENERAL_REFERENCE)
        qid = inv.questions[0]['question_id']
        proposal = {'decision': 'tool', 'tool': 'ask_clarification', 'gap_id': qid,
                    'expected_observation': 'x',
                    'arguments': {'question_id': qid, 'question': '资料怎么说？'}}
        self.assertIn('strategy_is_not_ask_user', proposal_errors(inv, proposal))

    def test_a_prescribing_question_is_still_refused(self):
        inv = case_state()
        declare(inv, statement='开始时间？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药甲'], target_field='start_date')
        qid = inv.questions[0]['question_id']
        proposal = {'decision': 'tool', 'tool': 'ask_clarification', 'gap_id': qid,
                    'expected_observation': 'x',
                    'arguments': {'question_id': qid, 'question': '请把剂量调整为 10mg。'}}
        self.assertIn('question_prescribes', proposal_errors(inv, proposal))


class AnswerAdoptionTests(unittest.TestCase):
    """答案采纳：把**取得的信息**变成某条问题的答案。"""

    def _read_question(self, inv, *, target=TARGET_PATIENT_STATE, field='dose',
                       strategy=STRATEGY_PATIENT_RECORD, subjects=('memory:medication:1@v1',)):
        inv.facts = {'medications': [{'display_name': '合成药甲'}]}
        inv.memory = _Memory({'memory:medication:1@v1'})
        inv.memory.connection = _MedicationRows({'dose': '5mg'})
        errors = declare(inv, statement='合成药甲现在的剂量是多少？', target=target,
                         subject_refs=list(subjects), target_field=field, strategy=strategy)
        assert errors == [], errors
        return inv.questions[0]['question_id']

    def test_an_authoritative_record_field_is_reused_without_re_approval(self):
        """当前权威记录里的精确字段由**程序**直接复用，不需要模型再批准一次。"""
        inv = case_state()
        qid = self._read_question(inv)
        outcome = inv.answer_question(qid, source='patient_record', value='5mg',
                                      field='dose', source_ref='memory:medication:1@v1')
        self.assertTrue(outcome['accepted'], outcome)
        self.assertFalse(outcome['partial'])
        self.assertEqual('authoritative_record', outcome['provenance'])
        question = inv.question(qid)
        self.assertTrue(is_question_answered(question))
        self.assertFalse(question_blocks(question))

    def test_a_source_that_exists_but_does_not_support_the_answer_is_refused(self):
        """引用存在 != 来源支持答案。"""
        inv = case_state()
        qid = self._read_question(inv)
        outcome = inv.answer_question(qid, source='patient_record', value='20mg',
                                      field='dose', source_ref='memory:medication:1@v1')
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('record_differs', outcome['errors'])
        self.assertFalse(is_question_answered(inv.question(qid)))
        self.assertTrue(question_blocks(inv.question(qid)),
                        '对不上的答案必须继续保持未决')

    def test_an_evidence_answer_needs_a_quote_from_what_was_actually_read(self):
        inv = case_state()
        inv.read_refs = ['ev-1']
        inv.evidence_store = _Evidence({'ev-1': '合成药甲的常用起始剂量为 5mg。'})
        qid = self._read_question(inv, target=TARGET_GENERAL_REFERENCE, field='dose',
                                  strategy=STRATEGY_GENERAL_REFERENCE, subjects=('合成药甲',))
        refused = inv.answer_question(qid, source='evidence', value='5mg', field='dose',
                                      source_ref='ev-1')
        self.assertFalse(refused['accepted'])
        self.assertIn('no_quote', refused['errors'])
        refused = inv.answer_question(qid, source='evidence', value='5mg', field='dose',
                                      source_ref='ev-1', quote='说明书上说可以随便加量')
        self.assertIn('quote_not_read_back', refused['errors'])
        accepted = inv.answer_question(qid, source='evidence', value='5mg', field='dose',
                                       source_ref='ev-1', quote='常用起始剂量为 5mg')
        self.assertTrue(accepted['accepted'], accepted)
        self.assertEqual('reference_evidence', accepted['provenance'])

    def test_a_user_report_is_recorded_with_its_own_provenance(self):
        """用户报告不等于已核实事实——来源属性如实保留。"""
        inv = case_state()
        qid = self._read_question(inv, field='schedule')
        outcome = inv.answer_question(qid, source='user_answer', value='每日一次',
                                      field='schedule')
        self.assertTrue(outcome['accepted'])
        self.assertEqual('user_reported', outcome['provenance'])
        answer = inv.question(qid)['answers'][-1]
        self.assertEqual('user_reported', answer['provenance'])

    def test_a_partial_answer_keeps_the_question_open_and_says_what_is_missing(self):
        """只答上一部分：保留已知部分，明确剩余缺口。"""
        inv = case_state()
        qid = self._read_question(inv)
        outcome = inv.answer_question(qid, source='user_answer', value='每日一次',
                                      field='schedule')
        self.assertTrue(outcome['accepted'])
        self.assertTrue(outcome['partial'])
        self.assertEqual(['dose'], outcome['still_open'])
        question = inv.question(qid)
        self.assertFalse(is_question_answered(question))
        self.assertTrue(question_blocks(question))
        self.assertTrue(any(g.get('missing_field') == 'dose' for g in inv.gaps),
                        '剩余缺口必须写清楚')

    def test_an_unrelated_non_empty_tool_result_cannot_answer_a_question(self):
        """非空但无关的结果不能回答问题。"""
        inv = case_state()
        qid = self._read_question(inv)
        outcome = inv.answer_question(qid, source='patient_record', value='红色',
                                      field='dose', source_ref='memory:medication:1@v1')
        self.assertFalse(outcome['accepted'])
        self.assertFalse(is_question_answered(inv.question(qid)))

    def test_repeating_the_same_answer_does_not_adopt_twice(self):
        inv = case_state()
        qid = self._read_question(inv)
        first = inv.answer_question(qid, source='patient_record', value='5mg',
                                    field='dose', source_ref='memory:medication:1@v1')
        self.assertTrue(first['accepted'])
        again = inv.answer_question(qid, source='patient_record', value='5mg',
                                    field='dose', source_ref='memory:medication:1@v1')
        self.assertFalse(again['accepted'])
        self.assertIn('question_already_answered', again['errors'])
        self.assertEqual(1, len(inv.question(qid)['answers']))

    def test_an_answer_cannot_be_submitted_for_a_question_that_does_not_exist(self):
        inv = case_state()
        outcome = inv.answer_question('q:nope', source='patient_record', value='5mg')
        self.assertFalse(outcome['accepted'])
        self.assertIn('unknown_question_id', outcome['errors'])

    def test_a_supported_claim_updates_its_question(self):
        """证据 claim 得到支持时，**同一条问题**同步得到依据。"""
        inv = case_state()
        qid = self._read_question(inv, target=TARGET_GENERAL_REFERENCE, field='dose',
                                  strategy=STRATEGY_GENERAL_REFERENCE, subjects=('合成药甲',))
        claim_id = 'claim:' + digest([qid])[:12]
        self.assertIn(claim_id, [c['claim_id'] for c in inv.claims],
                      '一般参考知识类问题建立时就带着它的 claim')
        inv.assessments[claim_id] = {'ev-9': {'status': 'supported',
                                              'source_status': 'current',
                                              'support_status': 'supported_by_span'}}
        inv._assess()
        question = inv.question(qid)
        self.assertTrue(is_question_answered(question),
                        '有依据的支持必须回到问题上，否则问题永远停在未决')
        self.assertEqual(INFO_AVAILABLE, question['information_state'])

    def test_a_claim_that_was_read_but_not_supported_leaves_the_question_open(self):
        inv = case_state()
        qid = self._read_question(inv, target=TARGET_GENERAL_REFERENCE, field='dose',
                                  strategy=STRATEGY_GENERAL_REFERENCE, subjects=('合成药甲',))
        claim_id = 'claim:' + digest([qid])[:12]
        inv.assessments[claim_id] = {'ev-9': {'status': 'insufficient',
                                              'source_status': 'current',
                                              'support_status': 'no_supporting_span'}}
        inv._assess()
        question = inv.question(qid)
        self.assertFalse(is_question_answered(question))
        self.assertTrue(question_blocks(question))
        # 读了、但没形成支持关系：如实记为"试过没结果"，**不是**已回答。
        self.assertEqual(INFO_ATTEMPTED_NO_RESULT, question['information_state'])

    def test_classify_reading_separates_the_outcomes(self):
        inv = case_state()
        qid = self._read_question(inv)
        self.assertEqual('not_attempted', inv.classify_reading(inv.question(qid)))
        inv.record_question_attempt(qid, {'tool': 'memory_read', 'found_information': True})
        self.assertEqual('content_pending', inv.classify_reading(inv.question(qid)))
        inv.record_question_attempt(qid, {'tool': 'rag_search', 'found_information': False,
                                          'information_state': INFO_SOURCE_LIMITED})
        self.assertEqual('insufficient', inv.classify_reading(inv.question(qid)))


class _MedicationRows:
    """`_recorded_value` 的最小替身：只回它真的有的字段。"""

    def __init__(self, values):
        self.values = values

    def execute(self, _sql, _params=()):
        return _Row(self.values)


class _Row:
    def __init__(self, values):
        self._values = values

    def fetchone(self):
        row = dict(self._values)
        row.setdefault('display_name', '合成药甲')
        row.setdefault('schedule', None)
        row.setdefault('start_at', None)
        return row

    def __getitem__(self, key):
        return self._values.get(key)


class _Evidence:
    def __init__(self, contents):
        self.contents = contents

    def read(self, evidence_id, *, scope_id=None, offset=0, limit=2000):
        return {'content': self.contents.get(evidence_id, '')}

    def get_meta(self, evidence_id):
        return {'evidence_id': evidence_id} if evidence_id in self.contents else None


class ExecutionResultVsInformationStateTests(unittest.TestCase):
    def test_an_unavailable_source_is_not_an_answer(self):
        """暂时取不到资料 ≠ 已经有答案。"""
        inv = case_state()
        declare(inv, statement='资料怎么说？', target=TARGET_GENERAL_REFERENCE,
                subject_refs=['合成药甲'], strategy=STRATEGY_GENERAL_REFERENCE)
        question = inv.questions[0]
        question['status'] = QUESTION_STATUS_UNAVAILABLE
        question['information_state'] = INFO_SOURCE_LIMITED
        self.assertFalse(is_question_answered(question))
        self.assertTrue(question_blocks(question), '它必须继续阻塞、继续出现在关闭校验里')
        self.assertIn(question['question_id'],
                      [q['question_id'] for q in inv.blocking_questions()])

    def test_an_answered_question_with_available_information_is_settled(self):
        inv = case_state()
        declare(inv, statement='频次？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule')
        inv.settle_question(inv.questions[0]['question_id'], QUESTION_STATUS_ANSWERED,
                            information_state=INFO_AVAILABLE)
        self.assertTrue(is_question_answered(inv.questions[0]))
        self.assertFalse(question_blocks(inv.questions[0]))

    def test_no_search_means_no_evidence_unavailable_verdict(self):
        """没有执行检索，就不能报告"检索后资料不可得"。"""
        inv = case_state()
        declare(inv, statement='资料怎么说？', target=TARGET_GENERAL_REFERENCE,
                subject_refs=['合成药甲'], strategy=STRATEGY_GENERAL_REFERENCE)
        inv.finish('no_progress:repeated_proposal')
        self.assertEqual('no_progress', inv.termination_reason,
                         '重复规划不等于证据不足')
        self.assertEqual(INFO_NOT_ATTEMPTED, inv.questions[0]['information_state'])

    def test_a_search_that_found_nothing_is_reported_as_source_limited(self):
        inv = case_state()
        declare(inv, statement='资料怎么说？', target=TARGET_GENERAL_REFERENCE,
                subject_refs=['合成药甲'], strategy=STRATEGY_GENERAL_REFERENCE)
        qid = inv.questions[0]['question_id']
        inv.queries = ['a', 'b', 'c']
        inv.record_question_attempt(qid, {'tool': 'rag_search', 'found_information': False})
        inv.finish('budget_exhausted:search')
        self.assertEqual('evidence_unavailable', inv.termination_reason)
        self.assertEqual(INFO_SOURCE_LIMITED, inv.questions[0]['information_state'])
        self.assertTrue(question_blocks(inv.questions[0]),
                        '取不到仍然阻塞——它没有被当成已回答')

    def test_a_record_read_that_did_not_answer_is_not_evidence_unavailable(self):
        """成功读到了记录、但记录里没有答案 ≠ 资料不可得。

        真实跑批暴露的：模型读了患者记录（成功），问题仍未解决，却被写成
        `evidence_unavailable`——那句话的意思是"资料取不到"，与事实不符。
        """
        inv = case_state()
        declare(inv, statement='这两条药现在还在吃吗？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药甲'], target_field='interaction_status',
                strategy='patient_record')
        qid = inv.questions[0]['question_id']
        inv.record_question_attempt(qid, {'tool': 'memory_read', 'ok': True,
                                          'found_information': True,
                                          'information_state': INFO_RECEIVED_UNCONFIRMED})
        # 读到内容 ≠ 资料不可得（不该报 evidence_unavailable）……
        self.assertIsNone(inv._forced_stop_typed())
        # ……也 ≠ "必须换个来源"。内容是**待处理**的：先采纳，别急着换地方查。
        self.assertEqual('content_pending', inv.classify_reading(inv.questions[0]))
        self.assertIsNone(inv.revision_trigger())
        self.assertNotEqual('strategy_needs_change', inv.revision_trigger())

    def test_waiting_is_not_reported_as_making_no_progress(self):
        inv = case_state()
        declare(inv, statement='频次？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule')
        inv.finish('no_progress:repeated_reads')
        self.assertEqual('waiting_input', inv.termination_reason)

    def test_completion_needs_no_blocking_question_not_the_old_three_checks(self):
        inv = case_state()
        inv.settle_questions_by_default()
        self.assertNotEqual(('checked', 'checked', 'checked'), tuple(inv.checks.values()))
        self.assertEqual('checks_completed', inv._forced_stop_typed())

    def test_the_run_does_not_finish_before_the_model_can_ask(self):
        inv = case_state()
        self.assertFalse(inv.questions_settled)
        self.assertIsNone(inv._forced_stop_typed())

    def test_the_wrap_up_does_not_invent_a_default_plan(self):
        inv = case_state()
        declare(inv, statement='频次？', target=TARGET_PATIENT_STATE,
                subject_refs=['合成药乙'], target_field='schedule')
        inv.finish('no_progress:repeated_reads')
        self.assertEqual([], inv.claims)
        self.assertEqual(1, len(inv.questions))


class LegacyContractUnchangedTests(unittest.TestCase):
    def test_evidence_review_still_requires_whole_list_coverage(self):
        inv = case_state(meds=('合成药甲', '合成药乙'), policy='evidence_review')
        errors = inv.accept_questions([{'statement': '只看一条药', 'entities': ['合成药甲']}])
        self.assertEqual(['subquestion_coverage_incomplete'], errors)

    def test_evidence_review_still_has_its_three_checks(self):
        self.assertEqual(('authority', 'interaction_evidence', 'applicability'),
                         policy_of('evidence_review')['required_checks'])
        self.assertTrue(policy_of('evidence_review')['require_full_coverage'])
        self.assertFalse(policy_of('evidence_review')['typed_questions'])

    def test_evidence_review_still_requires_the_verbatim_question(self):
        inv = case_state(policy='evidence_review')
        inv.gap('fact:dose_unit:合成药甲', 'patient_fact_missing',
                '请补充合成药甲记录剂量的单位。', field='dose_unit:合成药甲')
        proposal = {'decision': 'tool', 'tool': 'ask_clarification',
                    'gap_id': 'fact:dose_unit:合成药甲', 'expected_observation': 'x',
                    'arguments': {'question': '单位是什么？'}}
        self.assertIn('question_does_not_match_missing_fact', proposal_errors(inv, proposal))


if __name__ == '__main__':
    unittest.main()
