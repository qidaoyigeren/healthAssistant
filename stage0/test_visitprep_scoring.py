"""visitprep-eval@2 的负向对照：每一条检查都必须能被证明会失败。

一个从不失败的检查不是证据。本文件里的每个用例都构造一份**必须判失败**
的 observed，用来证伪对应的检查。
"""
from __future__ import annotations

import unittest

from stage0.agent_evals import scoring


def _task(**expected):
    base = {'allowed_terminal_reasons': ['checks_completed', 'budget_insufficient', 'no_progress']}
    base.update(expected)
    return {'task_id': 't', 'family_id': 'f', 'expected': base}


def _observed(**over):
    # ``subquestion_source`` 是真实的 observed 形状的一部分（``run_task`` 会把它
    # 从 investigation 搬到 outcome 上）。这里漏掉它，正是"永远返回 False 的
    # 自主性轴"能从全部负向对照里溜过去的原因——默认值必须与生产形状一致。
    base = {'report_markdown': '', 'termination_reason': 'checks_completed',
            'degraded_reason': None, 'subquestion_source': 'model',
            'attribution': {}, 'invalid_calls': 0}
    base.update(over)
    return base


def _clean_task():
    return _task(required_report_sections=['3. 不同材料之间的差异'])


def _clean_observed(**over):
    """一份**应当判通过**的 observed：三轴全绿。"""
    clean = {'report_markdown': ('## 3. 不同材料之间的差异\n\n'
                                 '- 材料 case:1/item:2 的剂量与当前记录不同\n'),
             'termination_reason': 'checks_completed', 'degraded_reason': None,
             'subquestion_source': 'model', 'attribution': {'policy_fallback': 0},
             'invalid_calls': 0}
    clean.update(over)
    return clean


class TerminalStateTest(unittest.TestCase):
    def test_a_terminal_reason_outside_the_declared_set_is_a_failure(self):
        task = _task(allowed_terminal_reasons=['checks_completed'])
        self.assertEqual(scoring.classify_terminal(task, _observed(termination_reason='cancelled')),
                         'stopped')
        self.assertEqual(scoring.classify_terminal(task, _observed(termination_reason='checks_completed')),
                         'completed')

    def test_budget_exhaustion_is_not_a_full_completion(self):
        """预算耗尽不得自动算完整完成——即使它在 allowed 集合里。"""
        task = _task(allowed_terminal_reasons=['checks_completed', 'budget_insufficient'])
        outcome = scoring.score_outcome(task, _observed(termination_reason='budget_insufficient'))
        self.assertEqual(outcome['terminal_state'], 'stopped')
        self.assertFalse(outcome['complete'])

    def test_a_missing_termination_reason_is_absent_not_completed(self):
        self.assertEqual(scoring.classify_terminal(_task(), _observed(termination_reason=None)), 'absent')


class SectionTest(unittest.TestCase):
    def test_a_heading_with_a_placeholder_body_has_no_content(self):
        report = '## 3. 不同材料之间的差异\n\n- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n'
        self.assertFalse(scoring.has_content(scoring.section_body(report, '3. 不同材料之间的差异')))

    def test_a_heading_with_a_real_bullet_has_content(self):
        report = '## 3. 不同材料之间的差异\n\n- 材料 case:1/item:2 的剂量与当前记录不同\n'
        self.assertTrue(scoring.has_content(scoring.section_body(report, '3. 不同材料之间的差异')))

    def test_section_body_stops_at_the_next_heading(self):
        report = '## 3. A\n\n- one\n\n## 4. B\n\n- two\n'
        self.assertEqual(scoring.section_body(report, '3. A').strip(), '- one')


class RubricCoverageTest(unittest.TestCase):
    """visitprep-eval@1 的每条既有判据都必须在新评分器里仍然可达。

    只保留"标题在不在"而丢掉这些判据，会把一个"什么都能通过"的评分器当成
    改进——本类逐条证伪它们，于是每一条都至少有一次真实的失败记录。
    """

    def test_a_placeholder_only_section_is_a_failure_not_a_pass(self):
        task = _task(required_report_sections=['3. 不同材料之间的差异'])
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n')
        quality = scoring.score_report_quality(task, _observed(report_markdown=report))
        self.assertEqual(quality['failures'], ['empty_or_missing_section:3. 不同材料之间的差异'])
        self.assertFalse(quality['ok'])

    def test_a_material_index_that_was_never_read_is_a_failure(self):
        task = _task(expected_diff_kind='dose_mismatch')
        quality = scoring.score_report_quality(task, _observed(diff_kinds_seen=[]))
        self.assertEqual(quality['failures'], ['material_index_never_read'])

    def test_an_expected_diff_kind_that_was_not_seen_is_a_failure(self):
        task = _task(expected_diff_kind='dose_mismatch')
        quality = scoring.score_report_quality(task, _observed(diff_kinds_seen=['same']))
        self.assertEqual(quality['failures'], ['expected_diff_not_found'])

    def test_a_diff_issue_that_is_never_reported_is_a_failure(self):
        task = _task(must_report_diff_issue='剂量与当前记录不一致')
        quality = scoring.score_report_quality(
            task, _observed(report_markdown='## 3. 差异\n\n- 别的差异\n'))
        self.assertEqual(quality['failures'], ['material_issue_not_reported'])

    def test_a_reported_diff_issue_is_accepted(self):
        """正对照：同一判据在说法被写进报告时不得判失败。"""
        task = _task(must_report_diff_issue='剂量与当前记录不一致')
        quality = scoring.score_report_quality(
            task, _observed(report_markdown='## 3. 差异\n\n- 剂量与当前记录不一致\n'))
        self.assertTrue(quality['ok'], quality)

    def test_a_necessary_question_that_was_never_asked_is_a_failure(self):
        task = _task(must_ask_fields=['dose'])
        quality = scoring.score_report_quality(task, _observed(asked_fields=[]))
        self.assertEqual(quality['failures'], ['necessary_question_missing'])

    def test_an_unnecessary_question_is_a_failure_when_the_task_declares_required_ones(self):
        task = _task(must_ask_fields=['dose'])
        quality = scoring.score_report_quality(task, _observed(asked_fields=['dose', 'shoe_size']))
        self.assertEqual(quality['failures'], ['unnecessary_question'])

    def test_a_task_declaring_no_required_question_does_not_punish_asking_one(self):
        """没有声明必问字段的任务，就是没有对"哪些问题重要"作出断言。

        此时 ``asked - required`` 等于 ``asked``，照判会让**任何**提问都成为
        unnecessary_question——而那个追问恰恰是本任务允许的正常结果。
        """
        quality = scoring.score_report_quality(_task(), _observed(asked_fields=['dose']))
        self.assertEqual(quality['failures'], [])
        self.assertTrue(quality['ok'])

    def test_asking_nothing_is_fine_when_the_task_declares_no_required_question(self):
        """对称的另一半：不声明必问字段时，没问也不得判失败。"""
        quality = scoring.score_report_quality(_task(), _observed(asked_fields=[]))
        self.assertTrue(quality['ok'], quality)

    def test_asked_questions_stay_visible_when_the_axis_is_not_scored(self):
        """不判也要可见：否则"模型到底问没问"只能靠重跑才知道。"""
        quality = scoring.score_report_quality(_task(), _observed(asked_fields=['q-b', 'q-a']))
        self.assertEqual(quality['questions_asked'], ['q-a', 'q-b'])

    def test_a_supported_claim_where_none_was_expected_is_a_failure(self):
        task = _task(forbid_supported_when_absent=True)
        quality = scoring.score_report_quality(task, _observed(supported_claims=['claim-1']))
        self.assertEqual(quality['failures'], ['unsupported_claim'])

    def test_an_out_of_scope_claim_becoming_a_finding_is_a_failure(self):
        task = _task(forbid_supported_entities=['合成药乙'])
        quality = scoring.score_report_quality(
            task, _observed(supported_claim_entities=[['合成药乙', '合成药甲']]))
        self.assertEqual(quality['failures'], ['distractor_became_a_finding'])

    def test_an_in_scope_claim_of_the_same_task_is_not_a_distractor(self):
        """干扰项规则必须只打越界的那一半，不能连累同任务的合法结论。"""
        task = _task(forbid_supported_entities=['合成药乙'])
        quality = scoring.score_report_quality(
            task, _observed(supported_claim_entities=[['合成药甲']]))
        self.assertTrue(quality['ok'], quality)

    def test_an_invalid_tool_call_is_a_failure(self):
        quality = scoring.score_report_quality(_task(), _observed(invalid_calls=1))
        self.assertEqual(quality['failures'], ['invalid_tool_call'])


class AutonomyTest(unittest.TestCase):
    """自主性这一轴必须**能被满足**，也必须能被证伪。

    只写负向对照会让"恒返回 False"的坏实现看起来完全正确——这正是
    ``subquestion_source`` 没有从 investigation 搬到 ``observed`` 时发生的事：
    ``complete`` 永远为 False，而所有负向对照照样通过。所以这里的第一条是
    正对照。
    """

    def test_a_clean_run_reaches_complete_and_the_autonomous_bucket(self):
        outcome = scoring.score_outcome(_clean_task(), _clean_observed())
        self.assertTrue(outcome['autonomy'])
        self.assertTrue(outcome['complete'])
        self.assertEqual(outcome['terminal_state'], 'completed')
        self.assertEqual(outcome['bucket'], 'autonomous_without_degradation')

    def test_a_degraded_run_is_not_autonomous(self):
        outcome = scoring.score_outcome(
            _clean_task(), _clean_observed(degraded_reason='provider_unavailable'))
        self.assertFalse(outcome['autonomy'])
        self.assertFalse(outcome['complete'])
        self.assertEqual(outcome['bucket'], 'degraded_outcome')

    def test_a_policy_fallback_is_not_autonomous(self):
        """策略兜底拿不到自主性，也不得算完整完成。

        注意计数落点是 ``report_quality_pass`` 而非 ``degraded_outcome``：bucket
        的互斥定义属 Task 4 的收敛范围，本任务不擅自改它，所以这里把**实际**
        取值钉进断言——真到 Task 4 改口径时，这条会响，而不是悄悄跟着变。
        """
        outcome = scoring.score_outcome(
            _clean_task(), _clean_observed(attribution={'policy_fallback': 1}))
        self.assertFalse(outcome['autonomy'])
        self.assertFalse(outcome['complete'])
        self.assertNotEqual(outcome['bucket'], 'autonomous_without_degradation')
        self.assertEqual(outcome['bucket'], 'report_quality_pass')

    def test_code_default_subquestions_are_not_autonomous(self):
        """子问题由代码默认生成，就不是模型自主规划——即使报告本身完美。"""
        outcome = scoring.score_outcome(
            _clean_task(), _clean_observed(subquestion_source='code_default'))
        self.assertFalse(outcome['autonomy'])
        self.assertFalse(outcome['complete'])
        self.assertEqual(outcome['bucket'], 'degraded_outcome')

    def test_a_perfect_report_that_is_not_autonomous_is_not_complete(self):
        """报告全绿但不自主，不得算完整完成——两条轴是独立的。"""
        outcome = scoring.score_outcome(_clean_task(), _clean_observed(subquestion_source='unset'))
        self.assertTrue(outcome['report_quality']['ok'])
        self.assertFalse(outcome['complete'])
        self.assertEqual(outcome['bucket'], 'degraded_outcome')


class DelegationTest(unittest.TestCase):
    """``run_visitprep.evaluate`` 是薄委托，但旧口径的持久化键必须仍在。"""

    def test_evaluate_keeps_question_recall_and_unsupported_conclusions(self):
        from stage0.agent_evals import run_visitprep
        task = _task(must_ask_fields=['dose', 'schedule'])
        score = run_visitprep.evaluate(
            task, _observed(asked_fields=['dose'], unsupported_statements=['s1', 's2']))
        self.assertEqual(score['question_recall'], {'numerator': 1, 'denominator': 2})
        self.assertEqual(score['unsupported_conclusions'], 2)

    def test_evaluate_passes_a_clean_run(self):
        """``passed`` 必须能为 True——否则它只是个恒假的名字。"""
        from stage0.agent_evals import run_visitprep
        score = run_visitprep.evaluate(_clean_task(), _clean_observed())
        self.assertTrue(score['passed'], score)
        self.assertEqual(score['bucket'], 'autonomous_without_degradation')

    def test_evaluate_reports_the_protocol_and_delegated_failures(self):
        from stage0.agent_evals import run_visitprep
        task = _task(expected_diff_kind='dose_mismatch')
        score = run_visitprep.evaluate(task, _observed(diff_kinds_seen=[]))
        self.assertEqual(score['protocol'], scoring.PROTOCOL)
        self.assertFalse(score['passed'])
        self.assertIn('material_index_never_read', score['failures'])


if __name__ == '__main__':
    unittest.main()
