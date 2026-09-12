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


if __name__ == '__main__':
    unittest.main()
