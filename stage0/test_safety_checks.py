"""必要安全检查：由程序执行，不经过模型。

锁的不变量：
* 药单一变就登记一次检查——**在同一个事务里**，不是"看模型心情"；
* 检查在没有 agent、没有 planner、没有 provider 的情况下照样执行；
* 第一次出现的药物组合正是由这条路径第一次查到的；
* 同一世界状态下重复触发不重复检查，但世界变了必须重新检查（去重不能吞掉新提示）；
* 检查失败保持可见，绝不静默变成"检查过了"。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stage0.memory import MemoryStore
from stage0.product import ProductStore
from stage0 import safety_cases as sc
from stage0 import safety_checks as checks


SYNTHETIC_TEST_WARNING = {
    "drug_a": "氨氯地平", "drug_b": "克拉霉素", "severity": "moderate",
    "effect": "降压作用增强", "source_text": "合成文本",
    "source_url": "https://example.test/label", "confidence": "medium",
}


def fake_detect(medications):
    """与 test_memory_p1 同源的最小检测器：只认识两组药物对。"""
    warnings = []
    if {"氨氯地平", "克拉霉素"}.issubset(medications):
        warnings.append({
            "drug_a": "氨氯地平", "drug_b": "克拉霉素", "severity": "moderate",
            "mechanism": "CYP3A4", "effect": "降压作用增强", "management": None,
            "source_text": "与CYP3A4抑制剂克拉霉素合用时，氨氯地平暴露量增加",
            "source_url": "https://example.test/label", "confidence": "medium",
            "detection_path": "test_detector",
        })
    if {"氨氯地平", "辛伐他汀"}.issubset(medications):
        warnings.append({
            "drug_a": "辛伐他汀", "drug_b": "氨氯地平", "severity": "major",
            "mechanism": "CYP3A4", "effect": "肌病风险增加", "management": None,
            "source_text": "辛伐他汀与氨氯地平合用日剂量限制",
            "source_url": "https://example.test/label2", "confidence": "high",
            "detection_path": "test_detector",
        })
    return {"warnings": warnings, "medications": list(medications)}


class NecessaryCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.product = ProductStore(self.memory)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def add(self, name, key=None):
        return self.memory.apply_medication_change(
            action='add', name=name, ingredients=[], session_id='s',
            turn_id=key or name, source='caregiver')

    def queued(self):
        return [dict(row) for row in self.memory.connection.execute(
            'SELECT * FROM necessary_checks ORDER BY id')]

    def run_checks(self, **kwargs):
        return checks.run_necessary_checks(self.memory, detector=fake_detect,
                                           product=self.product, **kwargs)

    # ---- 触发与持久化 ------------------------------------------------------
    def test_a_medication_change_enqueues_a_check_in_the_same_transaction(self):
        self.add('氨氯地平')
        rows = self.queued()
        self.assertEqual(1, len(rows))
        self.assertEqual(checks.TRIGGER_MEDICATION_SET, rows[0]['trigger_kind'])
        self.assertEqual('open', rows[0]['status'])

    def test_the_check_runs_without_any_agent_or_provider(self):
        """必要检查不需要 agent：这里全程没有构造过 MedicationCoordinatorAgent。"""
        self.add('氨氯地平')
        self.add('克拉霉素')
        report = self.run_checks()
        self.assertEqual('ok', report['status'])
        self.assertTrue(report['completed'])
        self.assertEqual(0, report['pending']['unfinished'])

    def test_a_first_time_pair_is_found_by_the_deterministic_path(self):
        """第一次出现的组合：没有任何旧结论可失效，只能由这条路径查到。"""
        self.add('氨氯地平')
        self.assertEqual([], self.memory.current_conclusions())
        self.add('克拉霉素')
        self.run_checks()
        texts = [c['text'] for c in self.memory.current_conclusions()]
        self.assertTrue(any('氨氯地平×克拉霉素' in t for t in texts), texts)

    def test_the_finding_creates_a_safety_case_that_can_be_reopened(self):
        self.add('氨氯地平')
        self.add('克拉霉素')
        self.run_checks()
        case = sc.SafetyCaseStore(self.product).objects()[0]
        self.assertEqual(sc.CASE_INTERACTION_RISK, case['case_type'])
        # 对象键按归一化后排序拼接，与药名在句子里出现的先后无关。
        self.assertEqual(['pair:克拉霉素|氨氯地平'], case['subject_keys'])
        self.assertTrue(case['linked_conclusion_refs'])
        self.assertIn(sc.STATUS_OPEN, (case['current_status'], sc.STATUS_AWAITING_USER))

    # ---- 去重与失效 --------------------------------------------------------
    def test_the_same_world_state_is_not_checked_twice(self):
        self.add('氨氯地平')
        self.add('克拉霉素')
        self.run_checks()
        first = len(self.memory.current_conclusions())
        # 第二次运行：队列已空，不重复记录同一药物对。
        self.assertEqual([], self.run_checks()['completed'])
        self.assertEqual(first, len(self.memory.current_conclusions()))

    def test_a_changed_medication_set_is_not_swallowed_by_dedup(self):
        """去重键含用药集合哈希——世界变了就一定会再查一次。"""
        self.add('氨氯地平')
        self.add('克拉霉素')
        self.run_checks()
        self.add('辛伐他汀', key='simva')
        open_rows = [r for r in self.queued() if r['status'] == 'open']
        self.assertEqual(1, len(open_rows), '新药单必须再排一次检查')
        self.run_checks()
        texts = [c['text'] for c in self.memory.current_conclusions()]
        self.assertTrue(any('辛伐他汀×氨氯地平' in t for t in texts), texts)

    def test_returning_to_a_previously_checked_state_is_checked_again(self):
        """{甲,乙} → {甲} → 回到 {甲,乙}：这一轮的检查**不能**被历史去重吞掉。

        集合哈希和第一次相同。只按集合哈希去重，就会把这一轮当成重复而跳过——
        中间那次变化产生的任何结论都不会被重新核对。
        """
        self.add('氨氯地平')
        self.add('克拉霉素')
        self.run_checks()
        done_before = [r for r in self.queued() if r['status'] == 'done']
        self.assertEqual(2, len(done_before))

        self.memory.apply_medication_change(action='remove', name='克拉霉素', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        self.run_checks()
        self.add('克拉霉素', key='restart')
        self.assertTrue([r for r in self.queued() if r['status'] == 'open'],
                        '回到曾经检查过的状态必须再排一次检查')
        self.run_checks()
        self.assertEqual([], [r for r in self.queued() if r['status'] == 'open'])

    def test_a_failed_check_stays_visible_instead_of_reading_as_checked(self):
        def exploding(_medications):
            raise RuntimeError('detector unavailable')

        self.add('氨氯地平')
        self.add('克拉霉素')
        report = checks.run_necessary_checks(self.memory, detector=exploding,
                                             product=self.product)
        self.assertTrue(all(item['status'] == 'error' for item in report['completed']))
        # 检查失败**保持可见**：既没有变成 done，也没有产出任何结论。
        self.assertEqual(len(self.queued()), report['pending']['unfinished'])
        self.assertEqual([], self.memory.current_conclusions())

    def test_a_single_drug_list_is_a_no_op_not_a_false_all_clear(self):
        self.add('氨氯地平')
        self.run_checks()
        self.assertEqual([], self.memory.current_conclusions())
        self.assertEqual(0, self.queued()[0]['attempts'])

    # ---- 患者事实触发 ------------------------------------------------------
    def test_a_safety_critical_fact_change_enqueues_a_condition_check(self):
        from stage0.memory import SemanticFact
        self.memory.write_semantic_fact(
            SemanticFact('allergy', '青霉素', {'allergen': '青霉素'}), source='caregiver')
        kinds = {row['trigger_kind'] for row in self.queued()}
        self.assertIn(checks.TRIGGER_CONDITION_FACTS, kinds)

    def test_a_condition_check_without_retrieval_records_no_finding(self):
        """没有检索工具时**不推断**：不跑就是没跑过，不产出"没发现问题"。"""
        from stage0.memory import SemanticFact
        self.add('氨氯地平')
        self.memory.write_semantic_fact(
            SemanticFact('allergy', '青霉素', {'allergen': '青霉素'}), source='caregiver')
        self.run_checks(rag_tool=None)
        self.assertEqual([c for c in self.memory.current_conclusions()
                          if c['kind'] == 'condition_warning'], [])

    # ---- 确定性重查（无 agent） ---------------------------------------------
    def test_deterministic_recheck_never_phrases_absence_as_risk_removed(self):
        """没有 agent 的 hook 时，重查仍然执行，且"检不出"绝不写成"风险解除"。"""
        # agent 从未构造：检测器由 store 直接提供，证明这条路径不依赖 agent。
        self.memory.recheck_detector = fake_detect
        medication = self.add('氨氯地平')['medication']
        conclusion = self.memory.record_conclusion(
            session_id='s', turn_id='t', kind='warning',
            text='氨氯地平×克拉霉素：降压作用增强（moderate / medium）',
            memory_refs=[medication['ref']],
            source_refs=[{'uri': 'https://example.test/label', 'text': 'x'}])
        self.add('辛伐他汀', key='simva')  # 药单变化 → 旧结论失效并生成重查任务
        self.assertEqual(conclusion['id'], self.memory.stale_conclusions()[0]['id'])

        receipt = self.memory.recheck_pending()
        self.assertEqual('ok', receipt['status'])
        self.assertEqual('done', receipt['completed'][0]['status'])
        successor = next(c for c in self.memory.current_conclusions()
                         if c['predecessor_id'] == conclusion['id'])
        self.assertIn('未检出', successor['text'])
        for forbidden in ('风险已解除', '无风险', '可以放心'):
            self.assertNotIn(forbidden, successor['text'])

    # ---- 检测器结果口径 ----------------------------------------------------
    def test_a_bare_list_detector_is_understood_not_silently_empty(self):
        """返回列表的检测器与返回 {'warnings': [...]} 的检测器结果必须一致。

        只认字典会让一个返回列表的检测器被静默当成"没有检出"——把"没检查"
        读成"没问题"，正是最危险的失败方向。
        """
        warnings = [dict(SYNTHETIC_TEST_WARNING)]
        self.assertEqual(checks.findings_of(warnings),
                         checks.findings_of({"warnings": list(warnings)}))
        # 认不出的形态按异常处理，绝不降级为空结果。
        with self.assertRaises(TypeError):
            checks.findings_of("unexpected")

    def test_a_bare_list_detector_still_creates_a_case(self):
        self.add('氨氯地平')
        self.add('克拉霉素')
        report = checks.run_necessary_checks(self.memory, detector=fake_detect,
                                             product=self.product)
        self.assertTrue(report['completed'])
        self.assertEqual(1, len(self.memory.current_conclusions()))
        self.assertEqual(1, len(sc.SafetyCaseStore(self.product).objects()))

    # ---- 文本口径 ----------------------------------------------------------
    def test_finding_text_keeps_the_pair_parseable_for_dependency_rows(self):
        """结论文本必须保持 `a×b：…` 口径——依赖索引靠它解析药物对。"""
        text = checks.warning_text({'drug_a': 'A药', 'drug_b': 'B药',
                                    'effect': '叠加', 'severity': 'major',
                                    'confidence': 'high'})
        self.assertTrue(text.startswith('A药×B药：'))
        self.assertEqual('A药×B药', text.split('：', 1)[0])


if __name__ == '__main__':
    unittest.main()
