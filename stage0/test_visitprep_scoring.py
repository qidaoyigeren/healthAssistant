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
    base = {'report_markdown': '', 'termination_reason': 'checks_completed',
            'degraded_reason': None, 'attribution': {}, 'invalid_calls': 0}
    base.update(over)
    return base


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

    def test_an_unnecessary_question_is_a_failure(self):
        task = _task(must_ask_fields=['dose'])
        quality = scoring.score_report_quality(task, _observed(asked_fields=['dose', 'shoe_size']))
        self.assertEqual(quality['failures'], ['unnecessary_question'])

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


class DelegationTest(unittest.TestCase):
    """``run_visitprep.evaluate`` 是薄委托，但旧口径的持久化键必须仍在。"""

    def test_evaluate_keeps_question_recall_and_unsupported_conclusions(self):
        from stage0.agent_evals import run_visitprep
        task = _task(must_ask_fields=['dose', 'schedule'])
        score = run_visitprep.evaluate(
            task, _observed(asked_fields=['dose'], unsupported_statements=['s1', 's2']))
        self.assertEqual(score['question_recall'], {'numerator': 1, 'denominator': 2})
        self.assertEqual(score['unsupported_conclusions'], 2)

    def test_evaluate_reports_the_protocol_and_delegated_failures(self):
        from stage0.agent_evals import run_visitprep
        task = _task(expected_diff_kind='dose_mismatch')
        score = run_visitprep.evaluate(task, _observed(diff_kinds_seen=[]))
        self.assertEqual(score['protocol'], scoring.PROTOCOL)
        self.assertFalse(score['passed'])
        self.assertIn('material_index_never_read', score['failures'])


if __name__ == '__main__':
    unittest.main()
