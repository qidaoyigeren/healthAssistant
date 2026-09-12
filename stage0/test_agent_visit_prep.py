"""就诊准备闭环（协议 v4）：归因、纠错上下文、职责四分、子问题、材料、报告证据支持。

全部离线：不出网、不使用真实端点。真实模型只在显式 --live 入口下使用。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent, RAGTool
from stage0.memory import MemoryStore


@contextmanager
def _env():
    """A temp patient store that is CLOSED before the directory is removed.

    Windows keeps the SQLite handle locked, so the store must be closed inside
    the ``with`` block — a cleanup callback registered with ``addCleanup`` runs
    after TemporaryDirectory's own cleanup and fails with WinError 32.
    """
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
        try:
            yield store
        finally:
            store.close()


def _rag(chunks=None, warnings=None):
    """Offline exact retrieval double: never touches embeddings or the network."""
    class Fixed(RAGTool):
        def __call__(self, query, **kwargs):
            return {'query': query, 'mode': 'fixed', 'corpus_version': 'test-v1',
                    'results': list(chunks or []), 'warnings': list(warnings or [])}

        def _get_retriever(self):
            raise RuntimeError('offline exact retrieval; embeddings disabled')
    return Fixed()


def _planner_script(steps):
    """A scripted planner that binds each step to a gap the code ACTUALLY
    reports in the payload — never to a hard-coded id, so the test does not
    depend on how gaps happen to be named."""
    pending = list(steps)

    def provider(payload):
        inv = payload.get('investigation') or {}
        open_gaps = inv.get('open_gaps') or []
        if not pending:
            return {'decision': 'respond'}
        tool = pending.pop(0)
        if tool == 'respond':
            return {'decision': 'respond'}
        if tool == 'memory_read':
            return {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
                    'expected_observation': '获得完整当前事实及版本',
                    'arguments': {'query': 'snapshot'}}
        gap = next((g for g in open_gaps if g['kind'] == 'evidence_missing'), None) \
            or (open_gaps[0] if open_gaps else {'gap_id': 'authority'})
        return {'decision': 'tool', 'tool': tool, 'gap_id': gap['gap_id'],
                'expected_observation': f'{tool} 的结果', 'arguments': {}}

    return provider


PLANNER_SOURCES = ('llm', 'llm_post_correction', 'system_forced', 'fallback',
                   'deterministic', 'rejected')


def _run(ddi, steps, medication='华法林'):
    """Drive one turn through the real loop with a scripted planner."""
    with _env() as store:
        store.apply_medication_change(action='add', name=medication, ingredients=[],
            session_id='s', turn_id='t0', source='test', dose='3mg')
        agent = MedicationCoordinatorAgent(
            store, ddi_tool=ddi, rag_tool=_rag(), max_cycles=6,
            llm_planner_enabled=True, proposal_provider=_planner_script(steps))
        with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'}):
            response = agent.handle(CareEvent('user_message', '看看我的用药风险'),
                                    session_id='s', turn_id='t1')
        return [e['planner'] for e in response.tool_trace
                if e.get('phase') == 'plan' and e.get('planner')]


class AttributionTest(unittest.TestCase):
    """代码替模型做的每个决定都必须与"模型选择"分开计数。"""

    def test_planner_sources_are_the_declared_five(self):
        traces = _run(DDITool(lambda meds: [WARFARIN_WARNING]),
                      ['memory_read', 'ddi_check', 'respond'])
        sources = {t['source'] for t in traces}
        self.assertTrue(sources, 'trace 里没有任何 planner 归因')
        self.assertTrue(sources <= set(PLANNER_SOURCES),
                        f'出现了未声明的归因类别：{sources - set(PLANNER_SOURCES)}')

    def test_code_forced_warning_write_is_labelled_system_forced(self):
        """代码为满足安全不变量自己构造并执行的动作，不得记成模型选择。"""
        traces = _run(DDITool(lambda meds: [WARFARIN_WARNING]),
                      ['memory_read', 'ddi_check', 'respond'])
        sources = [t['source'] for t in traces]
        self.assertIn('system_forced', sources,
                      f'强制写入未单独标记；实际 planner sources = {sources}')

    def test_hydrated_arguments_are_marked_on_the_trace(self):
        """代码替模型补了决定性参数时，这条 trace 必须单独可见。"""
        # ddi_check without medications: the schema requires it, and the guard
        # owns the authoritative value, so it is hydrated rather than rejected.
        traces = _run(DDITool(lambda meds: []), ['memory_read', 'ddi_check', 'respond'])
        hydrated = [t for t in traces if t.get('hydrated_arguments')]
        self.assertTrue(hydrated, f'参数代填未被标记；traces = '
                                  f'{[(t["source"], t.get("argument_corrections")) for t in traces]}')
        self.assertTrue(all(t['argument_corrections'] for t in hydrated),
                        'hydrated_arguments 为真但 argument_corrections 为空')


class CorrectionContextTest(unittest.TestCase):
    """纠错提示只给约束：一旦它携带代码算好的动作，模型照抄即通过，
    却会被计成独立自主规划。"""

    def _planner(self):
        from stage0.agent import HybridPlanner
        return HybridPlanner(
            tool_schemas={'rag_search': {'type': 'object',
                                         'properties': {'query': {'type': 'string'}},
                                         'required': ['query']}},
            proposal_provider=lambda payload: {'decision': 'respond'})

    def test_correction_task_never_leaks_the_code_computed_next_action(self):
        from stage0.agent import AgentState
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对相互作用', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': 'x', 'entities': ['氨氯地平'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': []}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                     'description': 'd', 'claim_id': 'claim:a'}]
        scripted = inv.next_action()   # the code-computed action, with its fixed wording
        self.assertIsNotNone(scripted, '前置条件不成立：脚本动作没有被计算出来')
        planner = self._planner()
        state = AgentState(session_id='s', turn_id='t',
                           event=CareEvent('user_message', 'x'), investigation=inv)
        planner.last_rejection = {
            'proposal': {'decision': 'tool', 'tool': 'rag_search',
                         'arguments': {'query': '模型自己写的查询'}},
            'errors': [{'code': 'invalid_gap_link', 'category': 'safety', 'message': 'gap 无效'}],
            'reason': 'safety_rejection'}
        correction = planner.correction_for(state)
        self.assertIsNotNone(correction)
        self.assertNotIn('next_expected_action_hint', correction)
        blob = json.dumps(correction, ensure_ascii=False)
        self.assertNotIn(scripted.arguments['query'], blob,
                         '纠错上下文把代码算好的搜索词交给了模型')

    def test_correction_task_still_carries_actionable_constraints(self):
        """去掉泄露不等于去掉反馈：约束必须还在，否则模型只能瞎猜。"""
        from stage0.agent import AgentState
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对相互作用', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': 'x', 'entities': ['氨氯地平'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': []}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                     'description': 'd', 'claim_id': 'claim:a'}]
        planner = self._planner()
        state = AgentState(session_id='s', turn_id='t',
                           event=CareEvent('user_message', 'x'), investigation=inv)
        planner.last_rejection = {'proposal': {'decision': 'tool', 'tool': 'rag_search',
                                               'arguments': {'query': 'q'}},
                                  'errors': [{'code': 'invalid_gap_link', 'category': 'safety',
                                              'message': 'gap 无效'}],
                                  'reason': 'safety_rejection'}
        correction = planner.correction_for(state)
        self.assertIn('claim:a', correction['open_gap_ids'])
        self.assertFalse(correction['termination_ready'])
        self.assertIn('rag_search', correction['allowed_tools_now'])
        self.assertTrue(correction['instruction'])

    def test_post_correction_acceptance_is_labelled(self):
        """被拒后修正通过的提案，必须与"一次就对的独立规划"分开计数。"""
        traces = _run(DDITool(lambda meds: []), ['memory_read', 'respond', 'ddi_check'])
        sources = [t['source'] for t in traces]
        self.assertIn('llm_post_correction', sources, f'sources={sources}')


def _open_investigation(claim_status='insufficient', checks='uncovered'):
    """A minimal investigation with one open evidence gap."""
    from stage0.investigation import InvestigationState
    inv = InvestigationState('核对用药相互作用', 'local-demo')
    inv.authority_read = True
    inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
    inv.claims = [{'claim_id': 'claim:a', 'statement': '氨氯地平的标签证据', 'entities': ['氨氯地平'],
                   'status': claim_status, 'supporting_evidence': [], 'opposing_evidence': [],
                   'source_status': 'unknown', 'condition_status': 'unknown'}]
    inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                 'description': '核查氨氯地平的支持和反对证据', 'claim_id': 'claim:a'}]
    if checks == 'checked':
        inv.checks = {key: 'checked' for key in inv.checks}
        inv.gaps[0]['status'] = 'resolved'
        inv.gaps.append({'gap_id': 'subquestions', 'kind': 'plan_missing',
                         'status': 'resolved', 'description': 'plan'})
    return inv


class ResponsibilitySplitTest(unittest.TestCase):
    """强制停止 / 模型策略 / 降级策略 / 状态同步 必须可分辨。"""

    def test_forced_stop_produces_no_action_and_no_prefilled_candidate(self):
        inv = _open_investigation()
        self.assertIsNone(inv.forced_stop())
        self.assertEqual(inv.candidates, [],
                         '正常路径不得预填候选动作：那会把代码的策略塞回给模型')

    def test_degraded_path_still_produces_the_scripted_action(self):
        inv = _open_investigation()
        action = inv.degraded_next_action()
        self.assertIsNotNone(action)
        self.assertEqual(action.tool, 'rag_search')
        self.assertIn('氨氯地平', action.arguments['query'])
        self.assertEqual([c['tool'] for c in inv.candidates], ['rag_search'])

    def test_forced_stop_covers_the_non_negotiable_conditions(self):
        inv = _open_investigation(claim_status='supported', checks='checked')
        self.assertEqual(inv.forced_stop(), 'checks_completed')
        self.assertEqual(inv.termination_reason, 'checks_completed')

    def test_open_evidence_conflict_waits_for_review_and_never_picks_a_side(self):
        inv = _open_investigation(claim_status='supported', checks='checked')
        inv.gaps.append({'gap_id': 'conflict:claim:a', 'kind': 'evidence_conflict',
                         'status': 'open', 'description': '支持与反对证据并存'})
        self.assertEqual(inv.forced_stop(), 'waiting_review')

    def test_search_budget_exhaustion_stops_without_producing_an_action(self):
        inv = _open_investigation()
        from stage0.investigation import MAX_SEARCHES
        inv.queries = [f'q{i}' for i in range(MAX_SEARCHES)]
        self.assertEqual(inv.forced_stop(), 'budget_insufficient')
        self.assertEqual(inv.candidates, [])

    def test_an_already_set_termination_is_returned_verbatim(self):
        inv = _open_investigation()
        inv.termination_reason = 'waiting_input'
        self.assertEqual(inv.forced_stop(), 'waiting_input')

    def test_next_action_is_the_degraded_policy(self):
        """兼容层：既有调用方（finish/report 路径）语义不变。"""
        inv = _open_investigation()
        action = inv.next_action()
        self.assertEqual(action.tool, 'rag_search')


class _StubMemory:
    """The two MemoryStore calls sync_authority actually makes."""

    def __init__(self, medications=None):
        self._meds = medications if medications is not None else [
            {'display_name': '氨氯地平', 'ref': 'm1', 'dose': '5mg'},
            {'display_name': '克拉霉素', 'ref': 'm2', 'dose': '250mg'}]

    def snapshot(self):
        return {'medications': self._meds, 'semantic': [], 'open_conflicts': []}

    def scope_revision(self, name):
        return 1


class SubquestionsTest(unittest.TestCase):
    """子问题拆解是调查策略（C 类），必须归模型。"""

    def _inv(self, mode='llm'):
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对氨氯地平与克拉霉素能否同服', 'local-demo')
        inv.mode = mode
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}, {'display_name': '克拉霉素'}],
                     'semantic': [], 'open_conflicts': []}
        inv.sync_authority(_StubMemory())
        return inv

    def test_authority_read_leaves_a_subquestions_gap_instead_of_code_claims(self):
        inv = self._inv()
        self.assertEqual(inv.claims, [], '代码不得再自动穷举药对子问题')
        ids = [g['gap_id'] for g in inv.gaps if g['status'] == 'open']
        self.assertIn('subquestions', ids)
        self.assertEqual(inv.subquestion_source, 'unset')

    def test_deterministic_mode_keeps_the_existing_code_default(self):
        """没有可规划子问题的规划器时，行为必须与改造前一致，且如实标记来源。"""
        inv = self._inv(mode='deterministic')
        self.assertTrue(inv.claims, '确定性路径必须仍然产生可核查的子问题')
        self.assertEqual(inv.subquestion_source, 'code_default')

    def test_model_questions_become_claims_and_close_the_gap(self):
        inv = self._inv()
        errors = inv.accept_questions([
            {'statement': '氨氯地平与克拉霉素的相互作用', 'entities': ['氨氯地平', '克拉霉素']}])
        self.assertEqual(errors, [])
        self.assertEqual(inv.subquestion_source, 'model')
        self.assertEqual([c['source'] for c in inv.claims], ['model'])
        self.assertEqual([g['status'] for g in inv.gaps if g['gap_id'] == 'subquestions'],
                         ['resolved'])

    def test_invented_drug_names_are_refused(self):
        inv = self._inv()
        errors = inv.accept_questions([{'statement': 'x', 'entities': ['氨氯地平', '阿司匹林']}])
        self.assertEqual(errors, ['subquestion_entity_not_in_scope'])
        self.assertEqual(inv.claims, [])

    def test_questions_must_cover_every_authoritative_medication(self):
        """覆盖要求堵住"靠漏掉子问题让完成变便宜"。"""
        inv = self._inv()
        errors = inv.accept_questions([{'statement': 'x', 'entities': ['氨氯地平']}])
        self.assertEqual(errors, ['subquestion_coverage_incomplete'])
        self.assertEqual(inv.claims, [])

    def test_empty_or_malformed_question_sets_are_refused(self):
        inv = self._inv()
        self.assertEqual(inv.accept_questions([]), ['invalid_subquestion_count'])
        self.assertEqual(inv.accept_questions([{'statement': '', 'entities': ['氨氯地平']}]),
                         ['invalid_subquestion_statement'])
        self.assertEqual(inv.accept_questions([{'statement': 'x'}]),
                         ['invalid_subquestion_entities'])

    def test_material_candidate_names_count_as_allowed_entities(self):
        """材料候选药名也允许——否则模型无法就材料里的差异提问。

        走**真实数据流**：导入 CSV → `MaterialIndex.index()` → `observe()`
        记录 → `allowed_entities()`。改造前这里手工拼了一个候选字典，于是
        写入端（字符串）与读取端（字典）的形状不符被掩盖过去，该分支在生产
        中根本不可达——手工拼装的测试恰好绕开了它要验证的那段代码。
        """
        from stage0.agent import Observation
        from stage0.product import MaterialIndex, ProductStore

        with _env() as store:
            product = ProductStore(store)
            product.import_csv('chain', 'name,dose,unit,schedule,date,subject\n'
                                        '维生素D,400,IU,每日一次,2026-01-05,local-demo\n')
            inv = self._inv()
            inv.observe(Observation(tool='list_materials', purpose='枚举材料', arguments={},
                                    result=MaterialIndex(product).index(), ok=True), store)
            self.assertIn('维生素D', inv.allowed_entities())
            # 反面对照：没看到过的药名仍然不许出现，否则这条许可等于放开了
            # 全部药名，"不许编造药物"就不成立了。
            self.assertNotIn('布洛芬', inv.allowed_entities())

    def test_a_successful_revision_closes_the_leftover_error_gaps(self):
        """被拒过的声明在后来成功时，其错误缺口必须关闭。

        改造前 ``plan:*`` 缺口一经创建永不解析，而 ``forced_stop()`` 的
        ``checks_completed`` 要求"无任何开放缺口"——一次被拒之后成功的规划，
        会让整轮只能以 ``budget_insufficient``/``no_progress`` 收尾：**已经
        修正的错误永久挡住了完成**。
        """
        inv = self._inv()
        inv.gap('plan:subquestion_coverage_incomplete', 'plan_missing', '旧错误')
        self.assertEqual(inv.accept_questions(
            [{'statement': '核查', 'entities': ['氨氯地平', '克拉霉素']}]), [])
        leftover = [g for g in inv.gaps if g['gap_id'].startswith('plan:') and g['status'] == 'open']
        self.assertEqual(leftover, [])

    def test_repeated_identical_rejections_do_count_towards_the_limit(self):
        """重复同样的错误也必须计入——改造前数的是 distinct gap id。

        ``gap()`` 按 id 去重，而 plan 缺口的 id 是错误签名的拼接，所以同样的
        错误重复多少次都只有一个缺口，上限永远够不到；反过来，三种不同的单次
        错误又会误触发上限。计数的应当是**连续被拒次数**。
        """
        from stage0.agent import Observation
        inv = self._inv()
        for _ in range(3):
            inv.observe(Observation(tool='plan_questions', purpose='声明子问题', ok=True,
                                    arguments={'questions': [{'statement': 'x',
                                                              'entities': ['不在药单里的药']}]},
                                    result={}), None)
        self.assertEqual(inv.termination_reason, 'no_progress')

    def test_a_successful_declaration_resets_the_rejection_count(self):
        """一次成功即归零：界是"连续"被拒次数，不是累计。"""
        from stage0.agent import Observation
        inv = self._inv()
        reject = Observation(tool='plan_questions', purpose='声明子问题', ok=True,
                             arguments={'questions': [{'statement': 'x',
                                                       'entities': ['不在药单里的药']}]},
                             result={})
        inv.observe(reject, None)
        inv.observe(reject, None)
        self.assertEqual(inv.plan_attempts, 2)
        self.assertIsNone(inv.termination_reason)
        self.assertEqual(inv.accept_questions(
            [{'statement': '核查', 'entities': ['氨氯地平', '克拉霉素']}]), [])
        self.assertEqual(inv.plan_attempts, 0)

    def test_new_evidence_reopens_planning_so_the_model_can_revise(self):
        """接受之后出现新证据（材料差异）时，plan_questions 必须再次可用。

        改造前的闸门是"`subquestions` 缺口是否打开"，而接受声明正好把它解析掉
        ——于是接受之后**不可能**再修订，与工具自身描述"证据变化时可再次调用
        以修订"直接矛盾。
        """
        from stage0.investigation import allowed_tools
        inv = self._inv()
        self.assertEqual(inv.accept_questions(
            [{'statement': '核查', 'entities': ['氨氯地平', '克拉霉素']}]), [])
        self.assertNotIn('plan_questions', allowed_tools(inv))
        inv.gap('material:case:1/item:2', 'material_conflict', '材料差异',
                material_ref='case:1/item:2')
        self.assertIn('plan_questions', allowed_tools(inv))
        self.assertEqual(inv.revision_trigger(), 'new_material_evidence')

    def test_a_revision_is_recorded_with_its_history_and_retained_evidence(self):
        inv = self._inv()
        inv.accept_questions([{'statement': '核查', 'entities': ['氨氯地平', '克拉霉素']}])
        inv.gap('material:case:1/item:2', 'material_conflict', '材料差异',
                material_ref='case:1/item:2')
        self.assertEqual(inv.accept_questions(
            [{'statement': '核查材料差异', 'entities': ['氨氯地平', '克拉霉素']}]), [])
        self.assertEqual(len(inv.plan_revisions), 1)
        revision = inv.plan_revisions[0]
        self.assertEqual(revision['trigger'], 'new_material_evidence')
        self.assertEqual(revision['revision'], 1)
        # 实体集未变的 claim 拿到同一个 claim_id，其 assessments 因此继续有效；
        # 这一条必须**显式记录**，不能靠 id 恰好撞上。
        self.assertEqual(revision['retained'], revision['after'])

    def test_revisions_are_bounded_and_say_so_when_capped(self):
        """修订有界，且到界时留下**可读、非失败**的记录。"""
        from stage0.investigation import MAX_PLAN_REVISIONS, allowed_tools
        inv = self._inv()
        inv.accept_questions([{'statement': '核查', 'entities': ['氨氯地平', '克拉霉素']}])
        for index in range(MAX_PLAN_REVISIONS):
            inv.gap(f'material:case:1/item:{index}', 'material_conflict', '材料差异',
                    material_ref=f'case:1/item:{index}')
            self.assertIsNotNone(inv.revision_trigger(),
                                 f'第 {index} 次修订前触发条件应当成立')
            inv.accept_questions([{'statement': f'核查第{index}次',
                                   'entities': ['氨氯地平', '克拉霉素']}])
        inv.gap('material:case:1/item:99', 'material_conflict', '又一处材料差异',
                material_ref='case:1/item:99')
        self.assertIsNone(inv.revision_trigger(), '修订额度必须是有界的')
        self.assertNotIn('plan_questions', allowed_tools(inv))

    def test_retention_is_by_claim_id_and_the_id_is_order_sensitive(self):
        """保留的判据是 claim_id，而它是 ``digest(list(entities))``——**对顺序
        敏感**，这是本仓冻结的既有约定（持久化 claim id 的稳定性靠它）。

        所以钉住两件事：同一实体集、只改措辞 → 同一个 id → 证据保留；实体集
        相同但顺序变了 → 新 id → **不**保留。后者不是缺陷而是边界，写清楚比
        让它留在"看起来应该保留"的错觉里好。
        """
        inv = self._inv()
        inv.accept_questions([{'statement': '核查', 'entities': ['氨氯地平', '克拉霉素']}])
        inv.gap('material:case:1/item:2', 'material_conflict', '材料差异',
                material_ref='case:1/item:2')
        inv.accept_questions([{'statement': '换成另一种说法',
                               'entities': ['氨氯地平', '克拉霉素']}])
        revision = inv.plan_revisions[0]
        self.assertEqual(revision['retained'], revision['before'],
                         '同一实体集、只改措辞时，assessments 必须仍然有效')

        inv.gap('material:case:1/item:3', 'material_conflict', '材料差异',
                material_ref='case:1/item:3')
        inv.accept_questions([{'statement': '核查', 'entities': ['克拉霉素', '氨氯地平']}])
        self.assertEqual(inv.plan_revisions[1]['retained'], [],
                         'claim_id 对顺序敏感：换了顺序就是另一个 claim，不得假装保留')

    def _inv_with_material_drug(self):
        """单药权威药单 + 一个**材料独有**的药名。

        必须用材料独有的药名：权威药名已被覆盖度检查保护，想"丢掉"它必然先
        撞上 ``subquestion_coverage_incomplete``，测试就会因为**错误的理由**
        通过。材料药名只受防伪造这条检查保护——那正是要防的口子。
        """
        inv = self._inv()
        inv.facts = {'medications': [{'display_name': '氨氯地平'}],
                     'semantic': [], 'open_conflicts': []}
        inv.material_items = {'case:1/item:2': {'name': '维生素D',
                                                'kind': 'unresolved', 'current': []}}
        return inv

    def _declare_both(self, inv):
        return inv.accept_questions([{'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
                                     {'statement': '核查维生素D', 'entities': ['维生素D']}])

    def _mark_vitamin_unsolved(self, inv):
        for claim in inv.claims:
            if '维生素D' in claim['entities']:
                claim['status'] = 'insufficient'

    def test_a_revision_that_drops_an_unsolved_problem_is_refused(self):
        """不得通过删除未解决问题伪造完成。"""
        inv = self._inv_with_material_drug()
        self.assertEqual(self._declare_both(inv), [])
        self._mark_vitamin_unsolved(inv)
        errors = inv.accept_questions([{'statement': '核查氨氯地平', 'entities': ['氨氯地平']}])
        self.assertEqual(errors, ['revision_drops_open_problem'])

    def test_a_revision_that_keeps_the_problem_is_accepted(self):
        inv = self._inv_with_material_drug()
        self._declare_both(inv)
        self._mark_vitamin_unsolved(inv)
        self.assertEqual(inv.accept_questions([
            {'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
            {'statement': '核查维生素D（含材料差异）', 'entities': ['维生素D']}]), [])

    def test_a_refused_revision_leaves_the_unsolved_problem_intact(self):
        """拒绝必须真的什么都没改——否则"拒绝"只是一个返回码。"""
        inv = self._inv_with_material_drug()
        self._declare_both(inv)
        self._mark_vitamin_unsolved(inv)
        before_claims = [claim['claim_id'] for claim in inv.claims]
        before_revisions = list(inv.plan_revisions)
        self.assertEqual(inv.accept_questions([{'statement': '核查氨氯地平',
                                                'entities': ['氨氯地平']}]),
                         ['revision_drops_open_problem'])
        self.assertEqual([claim['claim_id'] for claim in inv.claims], before_claims)
        self.assertEqual(inv.plan_revisions, before_revisions)
        self.assertIn('维生素D', inv.unsolved_entities())

    def test_a_dropped_problem_keeps_completion_out_of_reach(self):
        """拒绝的**后果**：那个未解决的问题仍然挡着 checks_completed。

        单看"返回了错误码"还不够——必须证明缺口没有被这次被拒的修订顺手带走。
        """
        inv = self._inv_with_material_drug()
        inv.authority_read = True
        self._declare_both(inv)
        self._mark_vitamin_unsolved(inv)
        inv.checks = {key: 'checked' for key in inv.checks}
        self.assertEqual(inv.accept_questions([{'statement': '核查氨氯地平',
                                                'entities': ['氨氯地平']}]),
                         ['revision_drops_open_problem'])
        self.assertTrue(any(g['status'] == 'open' for g in inv.gaps),
                        '被拒的修订不得带走缺口')
        self.assertNotEqual(inv.forced_stop(), 'checks_completed')

    def test_the_gap_list_exposes_plan_questions_only_while_planning_is_open(self):
        from stage0.investigation import allowed_tools
        inv = self._inv()
        self.assertIn('plan_questions', allowed_tools(inv))
        inv.accept_questions([{'statement': 'x', 'entities': ['氨氯地平', '克拉霉素']}])
        self.assertNotIn('plan_questions', allowed_tools(inv))

    def test_plan_questions_cannot_bypass_other_gaps(self):
        """没有修订触发条件时，plan_questions 不得被挂到别的缺口上继续使用。

        闸门现在由"是否可以规划"决定，而不是由它链接到哪个 gap_id 决定——
        但护栏本身不变：不满足触发条件时，挂到任何缺口上都应被拒。
        """
        from stage0.investigation import proposal_errors
        inv = self._inv()
        inv.accept_questions([{'statement': 'x', 'entities': ['氨氯地平', '克拉霉素']}])
        evidence_gap = next(g for g in inv.gaps
                            if g['kind'] == 'evidence_missing' and g['status'] == 'open')
        errors = proposal_errors(inv, {'decision': 'tool', 'tool': 'plan_questions',
                                       'gap_id': evidence_gap['gap_id'], 'expected_observation': 'x',
                                       'arguments': {'questions': [
                                           {'statement': 'x', 'entities': ['氨氯地平', '克拉霉素']}]}})
        self.assertEqual(errors, ['plan_questions_only_when_revisable'])

    def test_plan_questions_tool_is_only_visible_inside_an_investigation(self):
        from stage0.agent import AgentState, LLMPlanner
        from stage0.harness.default_tools import DEFAULT_TOOL_SPECS, PLAN_QUESTIONS_SPEC
        schemas = {**{name: spec.model_schema for name, spec in DEFAULT_TOOL_SPECS.items()},
                   PLAN_QUESTIONS_SPEC.name: PLAN_QUESTIONS_SPEC.model_schema}
        planner = LLMPlanner(tool_schemas=schemas, proposal_provider=lambda payload: {})
        state = AgentState(session_id='s', turn_id='t', event=CareEvent('user_message', 'x'))
        names = [item['function']['name'] for item in planner.tool_definitions(state)]
        self.assertNotIn('plan_questions', names)
        state.investigation = self._inv()
        names = [item['function']['name'] for item in planner.tool_definitions(state)]
        self.assertIn('plan_questions', names)


MATERIAL_CSV = ('name,dose,unit,schedule,date,subject\n'
                '氨氯地平,10,mg,每日一次,2026-01-05,local-demo\n'
                '克拉霉素,250,mg,每日两次,2026-01-05,local-demo\n')


class MaterialVisibilityTest(unittest.TestCase):
    """材料差异是既有的确定性结果，模型必须能看到它才能决定调查哪个分歧。"""

    def _product(self, store):
        from stage0.product import ProductStore
        return ProductStore(store)

    def test_index_exposes_the_deterministic_diff_and_source_coordinates(self):
        with _env() as store:
            product = self._product(store)
            store.apply_medication_change(action='add', name='氨氯地平', ingredients=[],
                session_id='s', turn_id='t', source='test', dose='5mg', schedule='每日一次')
            case = product.import_csv('k1', MATERIAL_CSV)
            from stage0.product import MaterialIndex
            payload = MaterialIndex(product).index()
            blob = json.dumps(payload, ensure_ascii=False)
            self.assertIn(case['id'], blob)
            kinds = {item['kind'] for material in payload['materials'] for item in material['items']}
            # 10mg in the material vs 5mg on record is a real discrepancy; the
            # index must carry it rather than flattening materials to text.
            self.assertIn('changed', kinds)
            self.assertTrue(any(item['locations'] for material in payload['materials']
                                for item in material['items']),
                            '差异必须带原文定位，否则模型无法指认来源')

    def test_index_never_leaks_raw_document_bytes(self):
        with _env() as store:
            product = self._product(store)
            product.import_csv('k1', MATERIAL_CSV)
            from stage0.product import MaterialIndex
            blob = json.dumps(MaterialIndex(product).index(), ensure_ascii=False)
            self.assertNotIn('base64', blob)
            self.assertNotIn('raw', blob.lower().replace('raw_', ''))

    def test_read_material_item_reports_correction_history(self):
        with _env() as store:
            product = self._product(store)
            case = product.import_csv('k1', MATERIAL_CSV)
            item = case['items'][0]
            product.decide(case['id'], item['item_id'], 'k2', product.revisions(), 'correct',
                           {'dose': '5'}, None)
            from stage0.product import MaterialIndex
            detail = MaterialIndex(product).item(case['id'], item['item_id'])
            self.assertEqual(detail['fields']['dose'], '5')
            self.assertEqual(detail['original_fields']['dose'], '10')
            self.assertTrue(detail['corrections'])
            self.assertTrue(detail['locations'])

    def test_reading_a_missing_item_is_an_error_not_an_empty_answer(self):
        with _env() as store:
            product = self._product(store)
            case = product.import_csv('k1', MATERIAL_CSV)
            from stage0.product import MaterialIndex, ProductError
            with self.assertRaises(ProductError):
                MaterialIndex(product).item(case['id'], 'does-not-exist')

    def test_material_tools_are_absent_until_an_index_is_attached(self):
        """未注入材料时目录保持不变：不得广告一个用不了的工具。"""
        with _env() as store:
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=DDITool(lambda meds: []), rag_tool=_rag())
            self.assertNotIn('list_materials', agent.executor.catalog())
            from stage0.product import MaterialIndex
            agent.attach_material_index(MaterialIndex(self._product(store)))
            self.assertIn('list_materials', agent.executor.catalog())
            self.assertIn('read_material_item', agent.executor.catalog())
            agent.attach_material_index(MaterialIndex(self._product(store)))  # idempotent
            self.assertIn('list_materials', agent.executor.catalog())

    def test_attaching_materials_rebinds_the_planner_catalog(self):
        """prompt / guard / executor 必须看到同一套工具，否则三者会漂移。"""
        with _env() as store:
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=DDITool(lambda meds: []), rag_tool=_rag(),
                llm_planner_enabled=True, proposal_provider=lambda payload: {'decision': 'respond'})
            self.assertNotIn('list_materials', agent.planner.validator.tool_schemas)
            from stage0.product import MaterialIndex
            agent.attach_material_index(MaterialIndex(self._product(store)))
            self.assertIn('list_materials', agent.planner.validator.tool_schemas)
            self.assertIn('list_materials', agent.planner.llm_planner.tool_schemas)


class MultiCallSelectionTest(unittest.TestCase):
    """一个响应里多个工具调用时，不得丢掉模型的新意图去重复旧动作。"""

    @staticmethod
    def _call(name, arguments):
        from types import SimpleNamespace as NS
        return NS(function=NS(name=name, arguments=json.dumps(arguments, ensure_ascii=False)))

    def _state(self, executed):
        from stage0.agent import AgentState, Observation
        state = AgentState(session_id='s', turn_id='t', event=CareEvent('user_message', 'x'))
        state.observations = [Observation(tool=name, purpose='p', arguments=arguments,
                                          result={}, ok=True)
                              for name, arguments in executed]
        return state

    def test_a_redundant_read_does_not_displace_the_models_new_intent(self):
        from stage0.agent import LLMPlanner
        state = self._state([('memory_read', {'query': 'current_medications'})])
        chosen = LLMPlanner._first_unexecuted(
            [self._call('memory_read', {'query': 'current_medications'}),
             self._call('list_materials', {})], state)
        self.assertEqual(chosen.function.name, 'list_materials')

    def test_when_every_call_is_a_repeat_the_first_is_kept(self):
        from stage0.agent import LLMPlanner
        state = self._state([('memory_read', {'query': 'snapshot'})])
        chosen = LLMPlanner._first_unexecuted(
            [self._call('memory_read', {'query': 'snapshot'}),
             self._call('memory_read', {'query': 'snapshot'})], state)
        self.assertEqual(chosen.function.name, 'memory_read')

    def test_without_state_the_first_call_is_kept(self):
        from stage0.agent import LLMPlanner
        chosen = LLMPlanner._first_unexecuted(
            [self._call('memory_read', {'query': 'snapshot'}), self._call('list_materials', {})], None)
        self.assertEqual(chosen.function.name, 'memory_read')

    def test_proposal_metadata_does_not_defeat_the_repeat_check(self):
        """gap_id / expected_observation 不参与比较，否则同一动作永远不算重复。"""
        from stage0.agent import LLMPlanner
        state = self._state([('memory_read', {'query': 'snapshot'})])
        call = self._call('memory_read', {'query': 'snapshot', 'gap_id': 'claim:a',
                                          'expected_observation': '再次确认'})
        self.assertTrue(LLMPlanner._already_executed(call, state))


class VisitPrepDatasetTest(unittest.TestCase):
    """任务集本身的性质：成对、只改证据、评分规则先定。"""

    @staticmethod
    def _tasks():
        return json.loads((Path(__file__).with_name('agent_evals')
                           / 'visitprep_dev.json').read_text(encoding='utf-8'))

    def test_twelve_tasks_in_six_families_arranged_in_pairs(self):
        tasks = self._tasks()
        self.assertEqual(len(tasks), 12)
        self.assertEqual(len({task['family_id'] for task in tasks}), 6)
        pairs = {}
        for task in tasks:
            pairs.setdefault(task['pair_id'], []).append(task)
        self.assertEqual(len(pairs), 6)
        self.assertTrue(all(len(group) == 2 for group in pairs.values()),
                        '每个 pair_id 必须恰好两个任务')

    def test_paired_tasks_differ_only_in_the_evidence(self):
        pairs = {}
        for task in self._tasks():
            pairs.setdefault(task['pair_id'], []).append(task)
        for pair_id, (left, right) in pairs.items():
            self.assertEqual(left['goal'], right['goal'],
                             f'{pair_id}: 成对任务的用户问题必须逐字一致')
            self.assertEqual(left['initial_state'], right['initial_state'],
                             f'{pair_id}: 成对任务的患者事实必须一致')
            self.assertNotEqual(left['expected'], right['expected'],
                                f'{pair_id}: 成对任务的预期结论必须不同，否则不成对')

    def test_every_task_states_the_rubric_before_the_run(self):
        for task in self._tasks():
            expected = task['expected']
            self.assertIn('expected_diff_kind', expected, task['task_id'])
            self.assertIn('allowed_terminal_reasons', expected, task['task_id'])
            self.assertIn('must_ask_fields', expected, task['task_id'])
            self.assertTrue(expected['allowed_terminal_reasons'], task['task_id'])
            self.assertLessEqual(task['budget']['max_cycles'], 12, task['task_id'])

    def test_every_declared_rubric_key_is_actually_read_by_the_evaluator(self):
        """数据集里不得出现"声明了却没人执行"的键。

        `search_budget` 曾经如此：两个任务声明了它，评分与执行都只读全局常量，
        于是那句声明没有任何约束力——一条**不可证伪**的规则，只是它证伪的方向
        是"看起来更严"。新增键必须同时接线，否则这个测试会失败。
        """
        from stage0.agent_evals.scoring import READ_EXPECTED_KEYS
        for task in self._tasks():
            unread = set(task['expected']) - set(READ_EXPECTED_KEYS)
            self.assertEqual(unread, set(),
                             f"{task['task_id']} 声明了没人读的评分键：{sorted(unread)}")

    def test_a_declared_search_budget_reaches_the_state_machine(self):
        """声明了就必须生效：上限由任务给出，而不是被全局常量吃掉。"""
        from stage0.investigation import InvestigationState, MAX_SEARCHES
        inv = InvestigationState('g', 'local-demo')
        self.assertEqual(inv.search_limit(), MAX_SEARCHES)
        inv.search_budget = 1
        self.assertEqual(inv.search_limit(), 1)
        inv.queries = ['q']
        inv.checks = {key: 'checked' for key in inv.checks}
        inv.termination_reason = None
        self.assertEqual(inv.forced_stop(), 'budget_insufficient')

    def test_the_arms_cover_the_two_factor_grid(self):
        """材料可见性 × 谁来规划，四格必须都有臂——否则差额无法归因。"""
        from stage0.agent_evals.run_visitprep import ARMS
        grid = {(item['investigation'] == '1', bool(item.get('llm') or item.get('script')),
                 bool(item['materials'])) for item in ARMS.values()}
        for planner in (True, False):
            for materials in (True, False):
                self.assertIn((True, planner, materials), grid,
                              f'缺少臂：规划器={planner} 材料={materials}')
        self.assertTrue(any(item.get('llm') for item in ARMS.values()))
        self.assertTrue(any(item['investigation'] == '0' for item in ARMS.values()))

    def test_provider_failures_are_broken_down_by_exception_type(self):
        """provider_error 必须能拆到具体异常类型，否则无法判断该优化网关还是规划。"""
        from stage0.agent_evals.run_visitprep import _provider_failures
        results = [{'observed': {'planner_steps': [
            {'provider_attempts': [
                {'outcome': 'response', 'latency_ms': 1200.0},
                {'outcome': 'timeout', 'error_type': 'APITimeoutError', 'latency_ms': 60000.0},
                {'outcome': 'rate_limit', 'error_type': 'RateLimitError', 'latency_ms': 300.0},
            ]}]}}]
        summary = _provider_failures(results)
        self.assertEqual(summary['attempts_by_outcome'],
                         {'response': 1, 'timeout': 1, 'rate_limit': 1})
        self.assertEqual(summary['failures_by_error_type'],
                         {'APITimeoutError': 1, 'RateLimitError': 1})
        self.assertEqual(summary['call_latency_ms']['max'], 60000.0)

    def test_provider_summary_is_null_not_zero_when_nothing_was_measured(self):
        """无测量时必须报 null，不能回填 0——0 会被读成"很快"。"""
        from stage0.agent_evals.run_visitprep import _provider_failures
        summary = _provider_failures([{'observed': {}}])
        self.assertIsNone(summary['call_latency_ms']['median'])
        self.assertEqual(summary['failed_calls'], 0)

    def test_the_evaluator_has_no_per_task_branching(self):
        """评分器不得认识具体任务：只读 expected，不按 id/文件名/族名分支。"""
        source = (Path(__file__).with_name('agent_evals')
                  / 'run_visitprep.py').read_text(encoding='utf-8')
        body = source.split('def evaluate', 1)[1].split('\ndef ', 1)[0]
        for task in self._tasks():
            self.assertNotIn(task['task_id'], body)
            self.assertNotIn(task['family_id'], body)
        self.assertNotIn('task_id ==', body)
        self.assertNotIn('pair_id ==', body)


UNSUPPORTED = '两药合用会导致严重出血，必须立即停药'


def _finished_investigation():
    from stage0.investigation import InvestigationState
    inv = InvestigationState('我下周去看心内科，帮我看看这几份材料', 'local-demo')
    inv.facts = {'medications': [], 'semantic': [], 'open_conflicts': []}
    inv.termination_reason = 'checks_completed'
    inv.checks = {key: 'checked' for key in inv.checks}
    return inv


class ReportEvidenceTest(unittest.TestCase):
    """模型写的解释必须过证据支持检查：无法核实的降为待确认，不得当结论。"""

    def test_model_statement_without_read_evidence_is_demoted_not_deleted(self):
        inv = _finished_investigation()
        inv.claims = [{'claim_id': 'claim:a', 'statement': UNSUPPORTED, 'entities': ['氨氯地平'],
                       'status': 'supported', 'supporting_evidence': [], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'model'}]
        pending = inv.verify_statements()
        self.assertEqual([p['claim_id'] for p in pending], ['claim:a'])
        self.assertEqual(pending[0]['reason'], 'no_read_evidence')
        text = inv.report_text()
        self.assertIn('待确认', text)
        self.assertNotIn(UNSUPPORTED, text.split('待确认', 1)[0],
                         '未核实的解释不得出现在结论区')

    def test_a_citation_that_was_never_read_back_does_not_count(self):
        inv = _finished_investigation()
        inv.evidence_refs = ['ev-1']
        inv.claims = [{'claim_id': 'claim:a', 'statement': '值得记录的差异', 'entities': ['氨氯地平'],
                       'status': 'supported', 'supporting_evidence': ['ev-1'], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'model'}]
        pending = inv.verify_statements()
        self.assertEqual([p['reason'] for p in pending], ['citation_not_read_back'])
        inv.read_refs = ['ev-1']
        self.assertEqual(inv.verify_statements(), [])

    def test_material_read_refs_count_as_citations(self):
        inv = _finished_investigation()
        inv.material_read_refs = ['case:1/item:1']
        inv.claims = [{'claim_id': 'claim:a', 'statement': '材料记的剂量与当前记录不一致',
                       'entities': ['氨氯地平'], 'status': 'supported',
                       'supporting_evidence': ['case:1/item:1'], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'model'}]
        self.assertEqual(inv.verify_statements(), [])

    def test_code_authored_claims_are_not_treated_as_model_explanations(self):
        inv = _finished_investigation()
        inv.claims = [{'claim_id': 'claim:a', 'statement': '合成药甲、合成药乙的标签证据',
                       'entities': ['合成药甲'], 'status': 'supported', 'supporting_evidence': [],
                       'opposing_evidence': [], 'source_status': 'unknown',
                       'condition_status': 'unknown', 'source': 'code_default'}]
        self.assertEqual(inv.verify_statements(), [])

    def test_the_report_answers_all_five_visit_prep_questions(self):
        inv = _finished_investigation()
        text = inv.report_text()
        for marker in ('解决了什么', '来源支持', '材料', '缺少依据', '医生'):
            self.assertIn(marker, text, f'报告缺少必答项：{marker}')
        self.assertIn('不是无风险', text)

    def test_an_absence_of_evidence_is_never_reported_as_no_risk(self):
        """证据不足是有效结果，但绝不能自动转成"没有风险"。"""
        inv = _finished_investigation()
        inv.checks = {key: 'uncovered' for key in inv.checks}
        inv.termination_reason = 'budget_insufficient'
        inv.claims = [{'claim_id': 'claim:a', 'statement': '合成药甲的标签证据', 'entities': ['合成药甲'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'code_default'}]
        text = inv.report_text()
        self.assertIn('尚无完成项', text)
        self.assertIn('不等于证明绝对安全', text)
        self.assertNotIn('无法确认风险', text)
        section2 = text.split('## 2', 1)[1].split('## 3', 1)[0]
        self.assertNotIn('合成药甲的标签证据', section2,
                         'insufficient 的 claim 不得出现在"有来源支持的事实"一节')

    def test_a_concrete_harm_sentence_is_reported_by_subjects_not_repeated(self):
        """交付文本受关键词安全检查约束，模型原文不得被原样复述成结论。"""
        inv = _finished_investigation()
        inv.claims = [{'claim_id': 'claim:a', 'statement': UNSUPPORTED, 'entities': ['氨氯地平', '克拉霉素'],
                       'status': 'supported', 'supporting_evidence': ['ev-1'], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'model'}]
        inv.read_refs = ['ev-1']
        text = inv.report_text()
        self.assertNotIn(UNSUPPORTED, text)
        self.assertIn('氨氯地平、克拉霉素', text)

    def test_a_citation_that_does_not_support_the_statement_is_not_a_conclusion(self):
        """引用真实存在、也确实回读过，但引用体里没有这句断言所说的内容。

        "回读过"与"支持"是两件事：前者旧口径已经判了，后者是本轮新加的。
        不支持的断言降级为**待确认项**，不用免责声明替代证据校验。
        """
        inv = _finished_investigation()
        inv.evidence_refs = ['ev-1']
        inv.read_refs = ['ev-1']
        inv.claims = [{'claim_id': 'claim:a', 'statement': '氨氯地平剂量为10mg',
                       'entities': ['氨氯地平'], 'status': 'supported',
                       'supporting_evidence': ['ev-1'], 'opposing_evidence': [],
                       'source_status': 'current', 'condition_status': 'verified',
                       'source': 'model', 'support_status': 'no_supporting_span'}]
        pending = inv.verify_statements()
        self.assertEqual([item['reason'] for item in pending],
                         ['citation_does_not_support_statement'])
        text = inv.report_text()
        self.assertNotIn('氨氯地平剂量为10mg', text.split('## 4', 1)[0],
                         '证据不支持的断言不得出现在结论区')
        self.assertIn('氨氯地平剂量为10mg', text,
                      '降级不等于删除：它必须以待确认项的身份仍然可见')

    def test_a_legacy_claim_without_the_support_key_is_not_retroactively_denied(self):
        """缺 ``support_status`` = 口径升级前采集的记录，按不可判定恢复。"""
        inv = _finished_investigation()
        inv.evidence_refs = ['ev-1']
        inv.read_refs = ['ev-1']
        inv.claims = [{'claim_id': 'claim:a', 'statement': '氨氯地平的标签证据',
                       'entities': ['氨氯地平'], 'status': 'supported',
                       'supporting_evidence': ['ev-1'], 'opposing_evidence': [],
                       'source_status': 'current', 'condition_status': 'verified',
                       'source': 'model'}]
        self.assertEqual(inv.verify_statements(), [])
        self.assertIn('氨氯地平的标签证据', inv.report_text().split('## 4', 1)[0])

    def test_a_contradicted_claim_is_not_rendered_as_a_supported_fact(self):
        """第 2 节叫"有来源支持的事实"，被反对的断言不得挂在它底下。"""
        inv = _finished_investigation()
        inv.claims = [{'claim_id': 'claim:a', 'statement': '某个被反对的断言',
                       'entities': ['氨氯地平'], 'status': 'contradicted',
                       'supporting_evidence': [], 'opposing_evidence': ['ev-1'],
                       'source_status': 'current', 'condition_status': 'verified',
                       'source': 'model', 'support_status': 'unknown'}]
        text = inv.report_text()
        section_two = text.split('## 2', 1)[1].split('## 3', 1)[0]
        self.assertNotIn('某个被反对的断言', section_two)
        self.assertIn('某个被反对的断言', text.split('## 3', 1)[1],
                      '降级不等于消失：它必须仍然出现在报告里')

    def test_a_model_subquestion_is_a_question_not_a_fact(self):
        """模型提出的子问题是**问句**，只能出现在第 5 节。

        改造前 `plan_questions` 的 statement 会变成 claim，一旦被判 supported
        就以**事实**身份写进第 2 节——调查问题被当成了结论输出。
        """
        inv = _finished_investigation()
        inv.questions = [{'gap_id': 'g', 'field': 'f',
                          'question': '材料里的维生素D是否需要核对？'}]
        text = inv.report_text()
        section_two = text.split('## 2', 1)[1].split('## 3', 1)[0]
        self.assertNotIn('维生素D是否需要核对', section_two)
        section_five = text.split('## 5', 1)[1]
        self.assertIn('维生素D是否需要核对', section_five)

    def test_an_unsupported_model_statement_becomes_a_question_to_ask(self):
        """D5：模型自己提的问题不得只在降级路径才出现。

        未获支持的模型断言在报告里是**待确认项**；它同时也是就诊时该问出口的
        问题，所以要一并出现在第 5 节。
        """
        inv = _finished_investigation()
        inv.claims = [{'claim_id': 'claim:a', 'statement': '材料里的维生素D是否需要核对',
                       'entities': ['维生素D'], 'status': 'insufficient',
                       'supporting_evidence': [], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown',
                       'source': 'model', 'support_status': 'unknown'}]
        section_five = inv.report_text().split('## 5', 1)[1]
        self.assertIn('材料里的维生素D是否需要核对', section_five)

    def test_material_reads_join_the_citation_set_through_observation(self):
        from stage0.agent import Observation
        inv = _finished_investigation()
        inv.observe(Observation('read_material_item', 'p',
                                {'case_id': 'case:1', 'item_id': 'item:1'}, {'ok': True}, True),
                    evidence_store=None)
        self.assertEqual(inv.material_read_refs, ['case:1/item:1'])


WARFARIN_WARNING = {
    'drug_a': '华法林', 'drug_b': '阿司匹林', 'effect': '出血风险增加',
    'source_text': '华法林与阿司匹林合用可增加出血风险，需监测凝血功能。',
    'source_url': 'label://warfarin-aspirin', 'confidence': 'high',
}


if __name__ == '__main__':
    unittest.main()
