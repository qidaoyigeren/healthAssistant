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

        Task 4 收敛了计数落点：兜底产出过动作就是降级，落 ``degraded_outcome``。
        此前它落在 ``report_quality_pass``——把"确定性兜底产出了动作"读成了一次
        报告质量通过。这条断言当时被刻意钉在**实际**取值上，所以口径一变它就响
        （见 git 历史），而不是悄悄跟着变。
        """
        outcome = scoring.score_outcome(
            _clean_task(), _clean_observed(attribution={'policy_fallback': 1}))
        self.assertFalse(outcome['autonomy'])
        self.assertFalse(outcome['complete'])
        self.assertNotEqual(outcome['bucket'], 'autonomous_without_degradation')
        self.assertEqual(outcome['bucket'], 'degraded_outcome')

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


class ConflictTest(unittest.TestCase):
    """第 3 节必须报告**真实观察到**的分歧，且具名双方。

    旧口径只做标题子串匹配，所以"这一节有没有写东西"从不影响结果——一个零
    结论、`unrecoverable_failure` 的报告照样五节齐全。本类逐条证伪新检查。
    """

    DIFF_LINE = '- 材料 case:1/item:2（changed）与当前记录 ref:med:7 存在差异\n'
    CONFLICT = [{'ref': 'case:1/item:2', 'kind': 'changed', 'counterparts': ['ref:med:7']}]

    def _task(self):
        return _task(must_report_conflict=True)

    def test_a_heading_with_a_placeholder_body_does_not_report_a_conflict(self):
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n')
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=report, material_conflicts=self.CONFLICT))
        self.assertIn('conflict_not_reported', quality['failures'])

    def test_a_body_that_denies_the_conflict_does_not_count(self):
        """写了内容、但内容与观察到的分歧相反，不是"报告了分歧"。"""
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 材料 case:1/item:2 与当前记录 ref:med:7 一致，没有差异。\n')
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=report, material_conflicts=self.CONFLICT))
        self.assertIn('conflict_not_reported', quality['failures'])

    def test_reporting_only_one_side_fails(self):
        report = '## 3. 不同材料之间的差异\n\n- 材料 case:1/item:2 的剂量不同\n'
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=report, material_conflicts=self.CONFLICT))
        self.assertIn('conflict_side_missing', quality['failures'])

    def test_both_sides_named_passes(self):
        report = '## 3. 不同材料之间的差异\n\n' + self.DIFF_LINE
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=report, material_conflicts=self.CONFLICT))
        self.assertEqual(quality['failures'], [])

    def test_a_conflict_that_was_never_actually_observed_is_not_required(self):
        """没有真实分歧时，占位句是**合法**内容——否则这条检查恒假。"""
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n')
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=report, material_conflicts=[]))
        self.assertEqual(quality['failures'], [])


class EmptySectionTest(unittest.TestCase):
    """标题缺失与"该有内容却只有占位句"必须都判失败；状态确实为空时占位句合法。

    这是 Task 1 引入的判据的双向证伪。只要求"必须有非占位条目"，会把
    `information_complete`（材料本就一致）这类任务变成**恒假**——与恒真一样
    不可证伪，而且正确答案永远拿不到分。
    """

    REPORT = ('## 3. 不同材料之间的差异\n\n'
              '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n')

    def _task(self):
        return _task(required_report_sections=['3. 不同材料之间的差异'])

    def test_a_missing_heading_is_a_failure(self):
        quality = scoring.score_report_quality(self._task(), _observed(report_markdown=''))
        self.assertEqual(quality['failures'], ['section_missing:3. 不同材料之间的差异'])

    def test_a_placeholder_that_contradicts_the_state_is_a_failure(self):
        """状态里有分歧，报告却只写占位句——这才是 A3 要修的那种缺陷。"""
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=self.REPORT,
                                    empty_state_sections=[]))
        self.assertEqual(quality['failures'], ['empty_or_missing_section:3. 不同材料之间的差异'])

    def test_a_placeholder_is_legitimate_when_the_state_really_is_empty(self):
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown=self.REPORT,
                                    empty_state_sections=['3. 不同材料之间的差异']))
        self.assertEqual(quality['failures'], [])

    def test_an_empty_state_never_excuses_a_missing_heading(self):
        """空态只解释"为什么只有占位句"，不能让整节消失。"""
        quality = scoring.score_report_quality(
            self._task(), _observed(report_markdown='',
                                    empty_state_sections=['3. 不同材料之间的差异']))
        self.assertEqual(quality['failures'], ['section_missing:3. 不同材料之间的差异'])

    def test_a_declared_name_matches_its_qualified_heading(self):
        """任务声明"4. 仍缺少依据的问题"，报告写作"…（待核实）"——是同一节。"""
        report = '## 4. 仍缺少依据的问题（待核实）\n\n- 核查合成药甲的标签证据\n'
        task = _task(required_report_sections=['4. 仍缺少依据的问题'])
        self.assertEqual(
            scoring.score_report_quality(task, _observed(report_markdown=report))['failures'], [])

    def test_a_names_mention_inside_a_body_is_not_a_section(self):
        """名字只出现在别处的正文里不算"这一节在"——否则这条检查又能被绕过。"""
        report = '## 3. 别的标题\n\n- 也被要求写 4. 仍缺少依据的问题\n'
        task = _task(required_report_sections=['4. 仍缺少依据的问题'])
        self.assertEqual(
            scoring.score_report_quality(task, _observed(report_markdown=report))['failures'],
            ['section_missing:4. 仍缺少依据的问题'])

    def test_an_empty_state_declared_by_its_rendered_title_still_excuses_the_section(self):
        """空态表由渲染端给出，用的是**渲染标题**（带后缀）；要求列表用的是
        声明名。同一个节的两个名字必须能对上，否则这里会出现假失败。"""
        report = '## 4. 仍缺少依据的问题（待核实）\n\n- 本契约内没有剩余缺口。\n'
        task = _task(required_report_sections=['4. 仍缺少依据的问题'])
        self.assertEqual(
            scoring.score_report_quality(
                task, _observed(report_markdown=report,
                                empty_state_sections=['4. 仍缺少依据的问题（待核实）']))['failures'],
            [])


class NegativeControlTest(unittest.TestCase):
    """每条对照都必须判失败——否则该检查仍不可证伪。"""

    def test_a_failed_task_with_a_complete_report_is_still_a_failure(self):
        task = _task(required_report_sections=['2. 有来源支持的事实'])
        report = '## 2. 有来源支持的事实\n\n- 有内容\n'
        observed = _observed(report_markdown=report, error='RuntimeError: boom')
        self.assertEqual(scoring.score_outcome(task, observed)['bucket'],
                         'execution_failed_or_not_sampled')

    def test_a_rule_takeover_is_not_autonomous(self):
        task = _task()
        observed = _observed(subquestion_source='code_default')
        outcome = scoring.score_outcome(task, observed)
        self.assertFalse(outcome['autonomy'])
        self.assertNotEqual(outcome['bucket'], 'autonomous_without_degradation')

    def test_a_policy_fallback_is_not_autonomous(self):
        outcome = scoring.score_outcome(_task(), _observed(
            subquestion_source='model',
            attribution={'policy_fallback': 2}))
        self.assertFalse(outcome['autonomy'])

    def test_a_perfect_report_with_a_policy_fallback_is_not_a_quality_pass(self):
        """报告完美，但**确定性兜底产出过动作**——这不是自主达成，也不是
        "报告质量通过"就该记的那一格。"""
        outcome = scoring.score_outcome(_task(), _observed(
            subquestion_source='model', attribution={'policy_fallback': 1},
            report_markdown='## 2. 有来源支持的事实\n\n- 有内容\n'))
        self.assertEqual(outcome['bucket'], 'degraded_outcome')

    def test_a_perfect_report_after_budget_exhaustion_is_not_complete(self):
        outcome = scoring.score_outcome(_task(), _observed(termination_reason='budget_insufficient'))
        self.assertFalse(outcome['complete'])


class CompletionTest(unittest.TestCase):
    """``complete`` 与计数 ``autonomous_without_degradation`` 判据不同，且
    这个差别有实际后果。"""

    def _task(self):
        return _task(allowed_terminal_reasons=['waiting_review', 'checks_completed'],
                     required_report_sections=['3. 不同材料之间的差异'])

    def _observed(self):
        return _observed(report_markdown='## 3. 不同材料之间的差异\n\n- 两处不一致\n',
                         termination_reason='waiting_review', subquestion_source='model',
                         attribution={'policy_fallback': 0})

    def test_handing_an_unresolved_conflict_to_a_human_is_a_complete_outcome(self):
        """停在该任务**声明允许**的 waiting_review 是正确产品行为，不是未完成。"""
        outcome = scoring.score_outcome(self._task(), self._observed())
        self.assertEqual(outcome['terminal_state'], 'waiting')
        self.assertTrue(outcome['complete'])

    def test_but_it_is_not_counted_as_autonomous_without_degradation(self):
        """那个计数明确要求分类为 completed——两者不可混为一谈。"""
        outcome = scoring.score_outcome(self._task(), self._observed())
        self.assertNotEqual(outcome['bucket'], 'autonomous_without_degradation')
        self.assertTrue(outcome['autonomy'])

    def test_a_terminal_reason_outside_the_declared_set_is_still_not_complete(self):
        task = _task(allowed_terminal_reasons=['checks_completed'])
        outcome = scoring.score_outcome(task, self._observed())
        self.assertFalse(outcome['complete'])

    def test_stopping_where_the_task_allows_is_not_an_execution_failure(self):
        """预算耗尽不在 completed/waiting 之列，但它可以是任务**声明允许**的
        出路：`first_search_empty` 族把检索压到 1 次，首搜无果就如实交付部分
        结果。把它记成"执行失败或未采样"是把一条正确路径读成故障。"""
        task = _task(allowed_terminal_reasons=['checks_completed', 'budget_insufficient'],
                     required_report_sections=['3. 不同材料之间的差异'])
        outcome = scoring.score_outcome(task, _observed(
            report_markdown='## 3. 不同材料之间的差异\n\n- 部分结果\n',
            termination_reason='budget_insufficient', subquestion_source='model',
            attribution={'policy_fallback': 0}))
        self.assertEqual(outcome['bucket'], 'terminal_expected')
        self.assertFalse(outcome['complete'], '它仍然不是一次完整完成')

    def test_stopping_somewhere_the_task_forbids_is_still_not_a_usable_sample(self):
        task = _task(allowed_terminal_reasons=['checks_completed'])
        outcome = scoring.score_outcome(task, _observed(
            report_markdown='## 3. 不同材料之间的差异\n\n- x\n',
            termination_reason='cancelled', subquestion_source='model',
            attribution={'policy_fallback': 0}))
        self.assertEqual(outcome['bucket'], 'execution_failed_or_not_sampled')


class RescoreTest(unittest.TestCase):
    def test_missing_fields_become_undetermined_not_fabricated(self):
        artifact = {'protocol': 'visitprep-eval@1', 'arm': 'fixed',
                    'tasks': [{'task_id': 't1', 'family_id': 'f',
                               'score': {'passed': True}, 'observed': {}}]}
        out = scoring.rescore(artifact)
        entry = out['tasks'][0]
        self.assertTrue(entry['undetermined'])
        self.assertEqual(entry['protocol'], 'visitprep-eval@2')
        self.assertEqual(out['original_protocol'], 'visitprep-eval@1')
        self.assertIn('not_comparable_to', out)
        self.assertIn('missing:termination_reason', entry['undetermined_reasons'])

    def test_a_complete_old_record_is_rescored_without_guessing(self):
        artifact = {'protocol': 'visitprep-eval@1', 'arm': 'fixed', 'tasks': [
            {'task_id': 't1', 'family_id': 'f', 'observed': {
                'report_markdown': '## 2. 有来源支持的事实\n\n- 有内容\n',
                'termination_reason': 'checks_completed',
                'subquestion_source': 'model', 'attribution': {}}}]}
        out = scoring.rescore(artifact)
        self.assertEqual(out['tasks'][0]['terminal_state'], 'completed')
        self.assertFalse(out['tasks'][0]['undetermined'])

    def test_the_old_score_is_kept_verbatim_and_the_new_one_added(self):
        """不覆盖旧结论：旧分数原样留档，新判定另立字段。"""
        artifact = {'protocol': 'visitprep-eval@1', 'arm': 'fixed', 'tasks': [
            {'task_id': 't1', 'family_id': 'f', 'score': {'passed': True},
             'observed': {'report_markdown': '', 'termination_reason': 'checks_completed',
                          'subquestion_source': 'model', 'attribution': {}}}]}
        entry = scoring.rescore(artifact)['tasks'][0]
        self.assertEqual(entry['original_score'], {'passed': True})
        self.assertIn('bucket', entry)

    def test_a_missing_axis_input_is_undetermined_and_does_not_manufacture_a_verdict(self):
        """`subquestion_source` 缺了，自主性那一轴就没有依据。

        照 ``None != 'model'`` 判，会把**每一条**旧记录读成降级——那是把"没记"
        当成"不是模型"，是补造判定。不可判定时不给 bucket，但在场的那部分仍
        以 ``partial`` 给出。
        """
        artifact = {'protocol': 'visitprep-eval@1', 'arm': 'model', 'tasks': [
            {'task_id': 't1', 'family_id': 'f', 'observed': {
                'report_markdown': '## 2. 有来源支持的事实\n\n- 有内容\n',
                'termination_reason': 'checks_completed', 'attribution': {}}}]}
        entry = scoring.rescore(artifact)['tasks'][0]
        self.assertTrue(entry['undetermined'])
        self.assertIn('missing:subquestion_source', entry['undetermined_reasons'])
        self.assertNotIn('bucket', entry)
        self.assertNotIn('autonomy', entry)
        self.assertEqual(entry['partial']['terminal_state'], 'completed')


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
