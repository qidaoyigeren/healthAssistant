"""claim-support@1 的负向对照：引用被回读过，不等于引用**支持**该断言。

本 scope **叠加**在 ``evidence_quality.conservative-lexical-v1`` 之上，不覆盖
它：后者决定一条 claim 有没有支持/反对，本 scope 决定该断言是否落在它所引用的
**具体片段**里。分开的理由是历史 assessments 不能因为口径升级集体失效。
"""
from __future__ import annotations

import unittest

from stage0.claim_support import SCOPE, assess_support


class ClaimSupportTest(unittest.TestCase):
    def test_a_statement_whose_entities_are_absent_from_the_quote_has_no_span(self):
        result = assess_support(statement='氨氯地平与克拉霉素存在相互作用',
                                quote='氨氯地平的常用起始剂量为每日一次5mg。',
                                entities=['氨氯地平', '克拉霉素'])
        self.assertEqual(result['status'], 'no_span')
        self.assertEqual(result['scope'], SCOPE)

    def test_a_numeric_fact_absent_from_the_quote_has_no_span(self):
        """引用真实存在且已回读，但数字对不上——不足以支持整个断言。"""
        result = assess_support(statement='氨氯地平剂量为10mg',
                                quote='氨氯地平常用起始剂量为每日一次5mg。',
                                entities=['氨氯地平'])
        self.assertEqual(result['status'], 'no_span')

    def test_numbers_present_in_the_quote_support_the_statement(self):
        result = assess_support(statement='氨氯地平剂量为5mg',
                                quote='氨氯地平常用起始剂量为每日一次5mg。',
                                entities=['氨氯地平'])
        self.assertEqual(result['status'], 'supported_by_span')

    def test_a_material_field_that_contradicts_the_statement_is_a_mismatch(self):
        result = assess_support(statement='材料记录的剂量与当前记录相同',
                                quote='氨氯地平,10,mg,每日一次,2026-01-05',
                                entities=['氨氯地平'],
                                material_item={'kind': 'changed',
                                               'fields': {'name': '氨氯地平', 'dose': '10',
                                                          'unit': 'mg'}})
        self.assertEqual(result['status'], 'field_mismatch')

    def test_no_quote_at_all_is_not_applicable_not_supported(self):
        self.assertEqual(assess_support(statement='x', quote='', entities=['x'])['status'],
                         'not_applicable')

    def test_a_material_that_really_is_changed_does_not_fail_a_difference_claim(self):
        """正对照：材料确实不同时，说"不同"必须能成立——否则这条判据只会
        惩罚正确结论。"""
        result = assess_support(statement='材料记录的剂量与当前记录不同',
                                quote='氨氯地平 10mg 每日一次',
                                entities=['氨氯地平'],
                                material_item={'kind': 'changed',
                                               'fields': {'name': '氨氯地平', 'dose': '10',
                                                          'unit': 'mg'}})
        self.assertEqual(result['status'], 'supported_by_span')


class PredicateTests(unittest.TestCase):
    """只共享实体名，不足以支持一句断言（O-1）。

    实体相同只说明"讲的是同一味药"，不说明"讲的是同一件事"。少了这一层，
    一段讲出血风险的文字会把"服药频次是什么"读成已有依据。
    """

    def test_a_shared_entity_name_does_not_support_an_unrelated_attribute(self):
        result = assess_support(statement='合成药甲目前的服药频次是什么？',
                                quote='合成药甲和合成药乙存在出血风险。',
                                entities=['合成药甲'], target_field='schedule')
        self.assertEqual(result['status'], 'no_span')
        self.assertIn('attribute_absent_from_span:schedule', result['reasons'])

    def test_a_span_that_actually_states_the_attribute_still_supports(self):
        """正对照：片段真的在讲这件事时必须能成立——否则这条判据只会惩罚
        正确结论，而"修好了"与"再也不判支持"就分不开。"""
        result = assess_support(statement='合成药甲目前的服药频次是什么？',
                                quote='合成药甲的服药频次为每日两次。',
                                entities=['合成药甲'], target_field='schedule')
        self.assertEqual(result['status'], 'supported_by_span')

    def test_without_a_known_field_it_falls_back_to_content_terms(self):
        unrelated = assess_support(statement='合成药甲的服药频次是什么？',
                                   quote='合成药甲和合成药乙存在出血风险。',
                                   entities=['合成药甲'], target_field=None)
        self.assertEqual(unrelated['status'], 'no_span')
        self.assertIn('content_absent_from_span', unrelated['reasons'])

    def test_the_content_fallback_still_accepts_a_matching_span(self):
        result = assess_support(statement='合成药甲的服药频次是什么？',
                                quote='合成药甲的服药频次为每日两次。',
                                entities=['合成药甲'], target_field=None)
        self.assertEqual(result['status'], 'supported_by_span')

    def test_a_material_item_claim_is_judged_by_the_material_kind_not_by_words(self):
        """材料路径不受新判据影响：那里判的是"材料与记录的关系"，
        不是"这句话是不是落在片段里"——一句关于差异的元陈述本来就不会
        逐字出现在片段中。"""
        result = assess_support(statement='材料记录的剂量与当前记录相同',
                                quote='氨氯地平 10mg 每日一次',
                                entities=['氨氯地平'],
                                material_item={'kind': 'same',
                                               'fields': {'name': '氨氯地平', 'dose': '10'}})
        self.assertEqual(result['status'], 'supported_by_span')


if __name__ == '__main__':
    unittest.main()
