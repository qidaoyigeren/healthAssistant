"""取证循环（evidence loop）契约：重复检测、观察可见性与工具用途。

背景——真实批次 0/8 的现场（`output/verification-2026-09-12/live/model-four-tasks.json`）：
8/8 任务只调用 `memory_read` 与 `list_materials`，`rag_search` / `read_evidence` /
`read_material_item` 一次也没有被调用。逐请求回放（当轮的一次性取证脚本，已随实验资产清理）
显示，从第 3 步起每次请求发给模型的实质状态完全相同（allowed_tools、open_gaps、
claims 都不变），只是观察列表在增长；而把其中一步换成 `rag_search` 后，同一个状态
立刻可以走通（claim → supported，open_gaps 清空）。

因此这些测试锁定的是**接口与循环**的行为，不是模型能力：
重复必须被识别并如实反馈、工具与缺口的对应关系必须可见、被截断或被丢弃的内容
必须留下痕迹。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage0.agent import AgentPlanner, AgentState, CareEvent, LLMPlanner, Observation  # noqa: E402
from stage0.harness.progress import NoProgressTracker, read_signature  # noqa: E402
from stage0.memory import MemoryStore  # noqa: E402


def _state(**kwargs):
    return AgentState('s', 't', CareEvent('user_message', '核查用药'), **kwargs)


# ---- 一、重复读取的识别：与顺序无关 ---------------------------------------------


class RepeatDetectionTests(unittest.TestCase):
    """在多个工具之间交替读取旧信息，与连续读取同一个工具是同一件事。"""

    def _tracker(self, directory):
        store = MemoryStore(Path(directory) / 'memory.db')
        return store, NoProgressTracker(store.connection, store._lock)

    def test_alternating_repeats_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, tracker = self._tracker(directory)
            try:
                a = read_signature('memory_read', {'query': 'snapshot'}, scope_id='s',
                                   patient_revision=1)
                b = read_signature('list_materials', {}, scope_id='s', patient_revision=1)
                self.assertEqual(tracker.record('r', a, limit=5)['verdict'], 'progress')
                self.assertEqual(tracker.record('r', b, limit=5)['verdict'], 'progress')
                # A 与 B 之间夹了一次别的读取，A 仍然是"已经读过的旧信息"。
                self.assertEqual(tracker.record('r', a, limit=5)['verdict'], 'repeat')
                self.assertEqual(tracker.record('r', b, limit=5)['verdict'], 'repeat')
            finally:
                store.close()

    def test_streak_counts_consecutive_steps_that_added_nothing(self) -> None:
        """新观察带来的信息是真的进展，理应把连胜清零；只有连续"什么也没新增"
        才累积到上限。"""
        with tempfile.TemporaryDirectory() as directory:
            store, tracker = self._tracker(directory)
            try:
                a = read_signature('memory_read', {'query': 'snapshot'}, scope_id='s',
                                   patient_revision=1)
                b = read_signature('list_materials', {}, scope_id='s', patient_revision=1)
                tracker.record('r', a, limit=5)
                self.assertEqual(tracker.record('r', a, limit=5)['repeats'], 1)
                # b 是这次运行里第一条新信息 → 进展，连胜归零。
                fresh = tracker.record('r', b, limit=5)
                self.assertEqual((fresh['verdict'], fresh['repeats']), ('progress', 0))
                # 此后两条都变旧，交替读取照样累积。
                self.assertEqual(tracker.record('r', b, limit=3)['repeats'], 1)
                self.assertEqual(tracker.record('r', a, limit=3)['repeats'], 2)
                self.assertEqual(tracker.record('r', b, limit=3)['verdict'], 'stop')
            finally:
                store.close()

    def test_reworded_call_that_returns_the_same_evidence_is_a_repeat(self) -> None:
        """改变无关参数但没有新增信息：换了措辞、签名不同，但拿回的是同一批
        证据——这一步同样什么都没得到。只按签名判会把它记成进展。"""
        with tempfile.TemporaryDirectory() as directory:
            store, tracker = self._tracker(directory)
            try:
                first = read_signature('rag_search', {'query': '甲 乙 相互作用'},
                                       scope_id='s', patient_revision=1)
                reworded = read_signature('rag_search', {'query': '甲 乙 合用 风险'},
                                          scope_id='s', patient_revision=1)
                self.assertNotEqual(first, reworded)
                self.assertEqual(tracker.record('r', first, limit=2)['verdict'], 'progress')
                verdict = tracker.record('r', reworded, limit=2, new_information=False)
                self.assertEqual(verdict['verdict'], 'repeat')
                # 但"说不清有没有新增"的工具永不被这样判。
                self.assertEqual(
                    tracker.record('r', read_signature('memory_read', {'query': 'conflicts'},
                                                       scope_id='s', patient_revision=1),
                                   limit=2, new_information=True)['verdict'], 'progress')
            finally:
                store.close()

    def test_changed_revision_or_new_page_is_never_a_repeat(self) -> None:
        """分页读取、新版本、来源更新都必须允许继续：它们签名不同，因此从来
        不是"已经取到过的内容"。"""
        with tempfile.TemporaryDirectory() as directory:
            store, tracker = self._tracker(directory)
            try:
                page1 = read_signature('read_evidence', {'evidence_id': 'ev-1', 'offset': 0},
                                       scope_id='s', patient_revision=1)
                page2 = read_signature('read_evidence', {'evidence_id': 'ev-1', 'offset': 2000},
                                       scope_id='s', patient_revision=1)
                before = read_signature('memory_read', {'query': 'snapshot'}, scope_id='s',
                                        patient_revision=1)
                after_write = read_signature('memory_read', {'query': 'snapshot'}, scope_id='s',
                                             patient_revision=2)
                old_corpus = read_signature('rag_search', {'query': 'q'}, scope_id='s',
                                            patient_revision=1, corpus_version='corpus:1')
                new_corpus = read_signature('rag_search', {'query': 'q'}, scope_id='s',
                                            patient_revision=1, corpus_version='corpus:2')
                distinct = [page1, page2, before, after_write, old_corpus, new_corpus]
                self.assertEqual(len(set(distinct)), len(distinct), '这些状态必须互不相同')
                for signature in distinct:
                    self.assertEqual(tracker.record('r', signature, limit=1)['verdict'],
                                     'progress', signature[:12])
            finally:
                store.close()


# ---- 二、循环：反馈与终止 -------------------------------------------------------


def _investigation_agent(directory, provider):
    from stage0.harness_eval import make_agent
    store = MemoryStore(Path(directory) / 'memory.db')
    store.apply_medication_change(action='add', name='氨氯地平', ingredients=[],
                                  session_id='s', turn_id='t', source='fixture')
    store.apply_medication_change(action='add', name='克拉霉素', ingredients=[],
                                  session_id='s', turn_id='t', source='fixture')
    agent = make_agent(store, provider=provider)
    return store, agent


class LoopContractTests(unittest.TestCase):
    def test_no_progress_limit_is_on_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AGENT_NO_PROGRESS_LIMIT', None)
            with tempfile.TemporaryDirectory() as directory:
                store, agent = _investigation_agent(directory, lambda payload: {'decision': 'respond'})
                try:
                    self.assertGreater(agent._no_progress_limit(), 0,
                                       '重复检测默认必须是开的：默认关闭正是真实批次里'
                                       '原地打转烧完预算的直接原因')
                finally:
                    store.close()

    def test_identical_write_does_not_reset_progress(self) -> None:
        """合法写操作不能仅因"发生了写入"就无条件重置进展。"""
        with tempfile.TemporaryDirectory() as directory:
            store, agent = _investigation_agent(directory, lambda payload: {'decision': 'respond'})
            try:
                first = Observation(tool='memory_write', purpose='p',
                                    arguments={'operation': 'consolidate_event'},
                                    result={'consolidation': {'x': 1}}, ok=True)
                repeat = Observation(tool='memory_write', purpose='p',
                                     arguments={'operation': 'consolidate_event'},
                                     result={'consolidation': {'x': 1}}, ok=True)
                state = _state()
                self.assertEqual(agent._progress_verdict(state, first), 'progress')
                verdicts = [agent._progress_verdict(state, repeat) for _ in range(2)]
                self.assertIn('repeat', verdicts,
                              '同一次写入重复提交不产生新信息，不能每次都重置计数器')
            finally:
                store.close()

    def test_repeat_feedback_names_what_stands_and_what_is_open(self) -> None:
        """首次重复给简洁反馈：已取得什么、还有什么没解决；且不指定下一个工具。"""
        from stage0.investigation import InvestigationState
        with tempfile.TemporaryDirectory() as directory:
            store, agent = _investigation_agent(directory, lambda payload: {'decision': 'respond'})
            try:
                inv = InvestigationState('核查用药', 'local-demo')
                inv.sync_authority(store)
                inv.authority_read = True
                inv.sync_authority(store)
                state = _state(investigation=inv)
                observation = Observation(tool='memory_read', purpose='snapshot',
                                          arguments={'query': 'snapshot'},
                                          result={'medications': []}, ok=True)
                self.assertEqual(agent._progress_verdict(state, observation), 'progress')
                self.assertEqual(agent._progress_verdict(state, observation), 'repeat')
                self.assertTrue(observation.no_progress)
                feedback = state.no_progress_feedback
                self.assertIsNotNone(feedback, '反馈必须作为结构化字段进入下一次请求')
                text = json.dumps(feedback, ensure_ascii=False)
                # 未解决的问题要具名
                self.assertTrue(any(str(g['gap_id']) in text for g in inv.gaps
                                    if g['status'] == 'open') or 'open_gaps' in feedback,
                                f'反馈没有点名仍未解决的问题: {text}')
                # 但不得代填下一个工具或参数
                for banned in ('rag_search', 'read_evidence', 'list_materials', 'read_material_item'):
                    self.assertNotIn(banned, text,
                                     '反馈只能给约束，不能替模型指定动作')
            finally:
                store.close()

    def test_a_second_turn_does_not_inherit_the_first_turns_reads(self) -> None:
        """误伤对照：两个事件共用一个幂等键（重复提交就是这么设计的）时，
        第二回合会读到与第一回合完全相同的结果。那不是"原地打转"，是新一轮
        工作需要同一份输入——第一回合的计数不能让第二回合第一步就停摆。
        """
        def provider(payload):
            investigation = payload.get('investigation') or {}
            if not investigation.get('authority_read'):
                return {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
                        'expected_observation': '完整权威快照',
                        'arguments': {'query': 'snapshot'}}
            gap = (investigation.get('open_gaps') or [{}])[0].get('gap_id')
            return {'decision': 'tool', 'tool': 'memory_read', 'gap_id': gap,
                    'expected_observation': '当前用药',
                    'arguments': {'query': 'current_medications'}}

        env = {'AGENT_INVESTIGATION_ENABLED': '1', 'AGENT_NO_PROGRESS_LIMIT': '2',
               'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0'}
        with mock.patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            store, agent = _investigation_agent(directory, provider)
            try:
                turns = []
                for _ in range(2):
                    response = agent.handle(CareEvent('user_message', '核查用药'),
                                            session_id='s', turn_id='same-id')
                    turns.append(response.tool_trace)
                first_acts = [e for e in turns[0] if e.get('phase') == 'act']
                second_acts = [e for e in turns[1] if e.get('phase') == 'act']
                self.assertGreaterEqual(
                    len(first_acts), 3,
                    '第一回合必须真的撞上无进展上限（否则这条对照什么都没测到）')
                self.assertTrue(
                    any(entry.get('phase') == 'no_progress' for entry in turns[0]),
                    '第一回合应当在原地打转时给出无进展反馈并收尾')
                self.assertGreaterEqual(
                    len(second_acts), 3,
                    '第二回合是新的一轮工作，第一步不该被判成对上一回合的重复')
            finally:
                store.close()

    def test_feedback_reaches_the_provider_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent = _investigation_agent(directory, lambda payload: {'decision': 'respond'})
            try:
                state = _state()
                observation = Observation(tool='memory_read', purpose='snapshot',
                                          arguments={'query': 'snapshot'},
                                          result={'medications': []}, ok=True)
                agent._progress_verdict(state, observation)
                agent._progress_verdict(state, observation)
                payload = agent.planner.llm_planner.prompt_payload(state)
                self.assertIn('no_progress_feedback', payload)
                self.assertTrue(payload['no_progress_feedback'])
            finally:
                store.close()


# ---- 三、工具与观察的可见性 -----------------------------------------------------


class VisibilityTests(unittest.TestCase):
    def test_rag_search_description_states_its_role(self) -> None:
        from stage0.agent import TOOL_DESCRIPTIONS
        description = TOOL_DESCRIPTIONS['rag_search']
        self.assertTrue(
            any(word in description for word in ('证据', '缺口', '检索')),
            f'rag_search 的说明没有说它是干什么用的: {description!r}')
        self.assertNotEqual(description, 'Hybrid retrieval over the local drug-label corpus'
                                         ' with a deterministic fallback.')

    def test_evidence_missing_gap_names_the_tools_that_can_close_it(self) -> None:
        from stage0.investigation import InvestigationState
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db')
            try:
                store.apply_medication_change(action='add', name='氨氯地平', ingredients=[],
                                              session_id='s', turn_id='t', source='fixture')
                store.apply_medication_change(action='add', name='克拉霉素', ingredients=[],
                                              session_id='s', turn_id='t', source='fixture')
                inv = InvestigationState('核查用药', 'local-demo')
                inv.sync_authority(store)
                inv.authority_read = True
                inv.sync_authority(store)
                inv.accept_questions([{'statement': '确认氨氯地平', 'entities': ['氨氯地平']},
                                      {'statement': '确认克拉霉素', 'entities': ['克拉霉素']}])
                view = inv.planner_view()
                gaps = {g['gap_id']: g for g in view['open_gaps']}
                self.assertTrue(gaps, '接受子问题之后必须留下开放的证据缺口')
                for gap in gaps.values():
                    self.assertIn('closable_by', gap,
                                  '未解决问题必须写明哪些工具能推进它')
                    self.assertTrue(gap['closable_by'])
                evidence_gap = next(g for g in gaps.values() if g['kind'] == 'evidence_missing')
                self.assertIn('rag_search', evidence_gap['closable_by'])
            finally:
                store.close()

    def test_invalid_source_still_blocks_completion(self) -> None:
        """失效来源不能支持结论——它必须继续**阻塞**完成，不许因为"减少误伤"
        被塞进非阻塞白名单。这条边界与本轮的重复检测改动无关，正因如此要钉住：
        放宽"原地打转"的判定时最容易顺手放宽它。"""
        from stage0.investigation import FINDING_GAPS, InvestigationState
        self.assertNotIn('source_invalid', FINDING_GAPS)
        inv = InvestigationState('核查用药', 'local-demo')
        inv.gap('source:ev-x', 'source_invalid', '证据来源不可回读或完整性失效。',
                evidence_ref='ev-x')
        inv.claims = [{'claim_id': 'claim:x', 'statement': 's', 'entities': ['甲'],
                       'status': 'supported', 'supporting_evidence': [], 'opposing_evidence': [],
                       'source_status': 'current', 'condition_status': 'verified',
                       'source': 'model', 'support_status': 'supported_by_span'}]
        inv.checks = {key: 'checked' for key in ('authority', 'interaction_evidence', 'applicability')}
        # 三项检查全绿、claim 已 supported——只差那条失效来源。完成条件必须
        # 仍然不成立。
        inv.forced_stop()
        self.assertNotEqual(inv.termination_reason, 'checks_completed',
                            '有失效来源时不得判定为"必查项已完成"')
        self.assertTrue(any(g['kind'] == 'source_invalid' and g['status'] == 'open'
                            for g in inv.gaps))
        view = inv.planner_view()
        gap = next(g for g in view['open_gaps'] if g['kind'] == 'source_invalid')
        self.assertEqual(gap['closable_by'], [],
                         '失效来源不该被声明为"还有工具能关掉它"')

    def test_listed_and_read_material_are_told_apart(self) -> None:
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核查用药', 'local-demo')
        inv.material_refs = ['case:a/1', 'case:a/2']
        inv.material_read_refs = ['case:a/1']
        view = inv.planner_view()
        self.assertEqual(view['material_unread'], ['case:a/2'])

    def test_truncated_observation_strings_are_marked(self) -> None:
        long_text = '甲' * 1200
        item = {'result': {'content': long_text}, 'tool': 'read_evidence'}
        LLMPlanner._truncate_result_strings(item, 600)
        truncated = item['result']['content']
        self.assertLess(len(truncated), 1200)
        self.assertIn('截断', truncated,
                      '被截断的原文必须留下痕迹，否则模型会把它当成完整内容引用')

    def test_dropped_parallel_calls_are_surfaced_to_the_model(self) -> None:
        """一个响应里带多个工具调用时，被丢弃的那个必须让模型看得见。"""
        from types import SimpleNamespace

        def call(name, arguments):
            return SimpleNamespace(id='c', type='function', function=SimpleNamespace(
                name=name, arguments=json.dumps(arguments, ensure_ascii=False)))

        planner = LLMPlanner(proposal_provider=lambda payload: {})
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[
                call('memory_read', {'query': 'snapshot', 'gap_id': 'g',
                                     'expected_observation': 'e'}),
                call('rag_search', {'query': 'q', 'gap_id': 'g',
                                    'expected_observation': 'e'}),
            ]), finish_reason='tool_calls')], usage=None)
        chosen = planner._parse_response(response, _state())
        self.assertEqual(chosen['tool'], 'memory_read')
        note = json.dumps(planner.prompt_payload(_state()).get('dropped_calls') or [],
                          ensure_ascii=False)
        self.assertIn('rag_search', note,
                      '被丢弃的 rag_search 必须出现在下一次请求里，模型才可能重新提交')


if __name__ == '__main__':
    unittest.main()
