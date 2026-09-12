# 自主调查闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让模型真正决定"查什么、查几轮、何时改写查询、调查哪个分歧"，把确定性代码降为该路径的**降级策略**，并让这个自主性产生可测量的用户价值（有来源的就诊准备报告）。

**Architecture:** 在既有 `investigation.py` 契约上做**可加性**改造：`next_action()` 按职责四分为强制停止 / 模型策略 / 降级策略 / 状态同步；新增 `plan_questions` 让模型拥有子问题；新增两个只读材料工具让模型看到上传材料及其确定性差异；堵掉纠错上下文泄露；把归因拆成五个互斥类别。持久化字段只增不改，不 bump `VERSION`/`CONTRACT`，避免制造迁移悬崖。

**Tech Stack:** Python 3.13、`unittest`（仓库既有风格，非 pytest）、SQLite、FastAPI（`stage0/server.py`）、React（`frontend/`，本轮不改）。

**Spec:** `docs/agent-capability-upgrade/visit-prep-2026-09-12/DESIGN.md`

## Global Constraints

- 测试运行方式（仓库既定）：`.venv/Scripts/python.exe -m unittest stage0.<module> -v`。**不要**用 pytest 语法写断言。
- 测试必须是**纯离线**：不发起任何远程调用。真实模型只在 `--live` 显式入口下使用。
- 只使用白名单内端点与合成数据；`assert_live_authorized()` 是唯一闸门，不得绕过。
- 既有约束**一行不删**：`PlannerPolicyGuard` 的安全不变量、`EvidenceStore` 的来源/哈希/作用域校验、`turn_budget` 的预算与租约、写操作的幂等回执。
- 持久化形状**只增不改**：`InvestigationState.VERSION` 与 `CONTRACT` 保持 `investigation@1` / `medication-evidence-review@1`。
- 不新增多 Agent、不做框架替换、不改 `frontend/`、不改 `AGENT_INVESTIGATION_ENABLED` 的产品默认值。
- 医学边界不变：不诊断、不处方、不建议开始/停止用药或调整剂量；`insufficient`/`unknown` 不等于"没有风险"。
- 每个任务结束必须 `git commit`；提交信息用仓库既有的英文主题行风格（见 `git log`）。

---

## 文件结构

| 文件 | 职责 | 本计划中的变化 |
|---|---|---|
| `stage0/investigation.py` | 调查状态机：缺口、覆盖、终止、报告文本 | 职责四分；`subquestions` 缺口；`accept_questions`；可加性字段 |
| `stage0/harness/default_tools.py` | 工具规格的单一真源 | 新增 `PLAN_QUESTIONS_SPEC`、`LIST_MATERIALS_SPEC`、`READ_MATERIAL_ITEM_SPEC` 与注册函数 |
| `stage0/harness/tools.py` | 执行器：权限、schema、预算、审计 | 权限表新增两项 |
| `stage0/agent.py` | 规划器、守卫、循环、归因 | 归因五分类；堵纠错泄露；`_decide` 只调强制停止；investigation-only 工具隔离 |
| `stage0/product.py` | 材料/差异域模型 | 新增只读适配器 `MaterialIndex` |
| `stage0/care_tasks.py` | 待办契约与报告产物 | 挂载材料适配器；报告五问；解释的证据支持检查 |
| `stage0/agent_evals/visitprep_dev.json` | 合成任务集（新建） | 12 个成对任务 |
| `stage0/agent_evals/run_visitprep.py` | 就诊准备 A/B 评测器（新建） | 评分规则、归因统计、逐条结果 |
| `stage0/test_agent_visit_prep.py` | 本轮的回归与归因测试（新建） | 全部任务的测试落点 |

---

## Task 1: 归因五分类（堵 P2）

**Files:**
- Modify: `stage0/agent.py`（`HybridPlanner._trace` 约 2234-2258；`MedicationCoordinatorAgent._decide` 约 3052-3069；`_handle` 约 2964-2981）
- Test: `stage0/test_agent_visit_prep.py`（新建）

**Interfaces:**
- Produces: `_trace()["source"]` 取值集合 `{"llm", "llm_post_correction", "system_forced", "fallback", "deterministic"}`；`_trace()["hydrated_arguments"]: bool`。
- Produces: `MedicationCoordinatorAgent._system_forced_action(state) -> ToolAction | None`。

- [ ] **Step 1: 写失败测试**

```python
"""就诊准备闭环：归因、子问题、材料可见性、报告证据支持。"""
from __future__ import annotations
import os
import unittest
from unittest.mock import patch

from stage0.agent import (CareEvent, DDITool, MedicationCoordinatorAgent, RAGTool)
from stage0.memory import MemoryStore


def _rag(chunks, warnings=None):
    class Fixed(RAGTool):
        def __call__(self, query, **kwargs):
            return {'query': query, 'mode': 'fixed', 'corpus_version': 'test-v1',
                    'results': list(chunks), 'warnings': list(warnings or [])}
        def _get_retriever(self):
            raise RuntimeError('offline exact retrieval')
    return Fixed()


class AttributionTest(unittest.TestCase):
    def test_code_forced_warning_write_is_labelled_system_forced(self):
        """代码为满足安全不变量自己构造并执行的动作，不得记成模型选择。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            store.apply_medication_change(action='add', name='华法林', ingredients=[],
                session_id='s', turn_id='t0', source='test', dose='3mg')
            warning = {'drug_a': '华法林', 'drug_b': '阿司匹林', 'effect': '出血风险增加',
                       'source_text': '华法林与阿司匹林合用增加出血风险', 'source_url': 'label://x',
                       'confidence': 'high'}
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=DDITool(lambda meds: [warning]), rag_tool=_rag([]),
                max_cycles=4, llm_planner_enabled=True,
                proposal_provider=lambda payload: {'decision': 'respond'})
            with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'}):
                response = agent.handle(CareEvent('user_message', '看看我的用药风险'),
                                        session_id='s', turn_id='t1')
            sources = [e['planner']['source'] for e in response.tool_trace
                       if e.get('phase') == 'plan' and e.get('planner')]
            self.assertIn('system_forced', sources,
                          f'强制写入未单独标记；实际 planner sources = {sources}')
            store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: FAIL — `AssertionError: 强制写入未单独标记；实际 planner sources = ['llm']`（或列表里没有 `system_forced`）

- [ ] **Step 3: 实现**

`agent.py` — 把 `_decide` 里的内联强制动作抽成命名方法，并让循环把它记为 `system_forced`：

```python
    def _system_forced_action(self, state: AgentState) -> ToolAction | None:
        """代码为满足安全不变量而自行构造的动作（不是模型选择）。

        目前只有一条：观察到尚未持久化的 DDI/条件警告时，必须先
        record_warnings 才能 respond。这是安全不变量，不是调查策略，
        因此由代码执行；但归因必须显式标成 system_forced，不得混入
        模型选择或策略降级。
        """
        inv = state.investigation
        if inv is None or state.degraded_reason:
            return None
        guard = PlannerPolicyGuard(snapshot_provider=self.memory.snapshot,
                                   medication_grounding=self.memory.current_medications)
        if guard._warning_source(state) is None:
            return None
        proposal = {'decision': 'tool', 'tool': 'memory_write',
                    'purpose': 'record_investigation_warnings',
                    'arguments': {'operation': 'record_warnings'}}
        if not guard.validate(state, proposal).valid:
            return None
        return guard.materialize(state, proposal)

    def _decide(self, state: AgentState) -> ToolAction | None:
        # Code-owned completion / hydration must not reuse a previous model's
        # metadata and inflate accepted proposals or repeat a provider error.
        if isinstance(self.planner, HybridPlanner):
            self.planner.last_decision_trace = None
        self._prepare_investigation(state)
        inv = state.investigation
        if inv:
            forced = self._system_forced_action(state)
            if forced is not None:
                if isinstance(self.planner, HybridPlanner):
                    self.planner.last_decision_trace = self.planner._trace(
                        'system_forced', asdict(forced), 'accepted', True, [], None,
                        time.perf_counter(), fallback_kind=None)
                return forced
            inv.forced_stop()
            if inv.termination_reason:
                return None
        return self.planner.decide(state)
```

`HybridPlanner._trace` — 新增 `hydrated_arguments`（`correction_for` 之外的第二个泄露面）：

```python
            "argument_corrections": corrections or [],
            # 代码替模型补了决定性参数时必须单独可见：一个"通过"的提案若
            # 参数被代填，就不是纯粹的模型规划。
            "hydrated_arguments": bool(corrections),
```

`_rejection_reason` 与 trace 的 `source` 字段在 `decide()` 中已经写死 `"llm"`；改为在 `decide()` 里根据是否刚发生过纠正选择字面量：

```python
        self.last_decision_trace = self._trace(
            "llm_post_correction" if was_corrected else "llm",
            proposal, "accepted", True, [], None, started,
            fallback_kind=None, corrections=list(self.validator.last_corrections),
        )
```

其中 `was_corrected` 在 `decide()` 开头取：`was_corrected = self.last_rejection is not None`（`last_rejection` 只在拒绝时设置，`reset_rejection_memory()` 与接受路径都会清空）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: PASS

- [ ] **Step 5: 跑既有回归，确认没有破坏归因契约**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_planner_reliability stage0.test_agent_investigation stage0.test_agent_adaptive -v`
Expected: 全部 PASS（`test_planner_reliability` 锁定了 `source`/`fallback_kind` 的既有取值）

- [ ] **Step 6: 提交**

```bash
git add stage0/agent.py stage0/test_agent_visit_prep.py
git commit -m "Attribution: separate code-forced actions from model choices"
```

---

## Task 2: 堵住纠错上下文泄露（堵 P1）

**Files:**
- Modify: `stage0/agent.py`（`HybridPlanner.correction_for` 约 2175-2200）
- Test: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Consumes: Task 1 的 `llm_post_correction` 归因。
- Produces: `correction_for()` 返回 `{'previous_proposal_was_rejected', 'rejection_reasons', 'allowed_tools_now', 'open_gap_ids', 'termination_ready', 'instruction'}` —— **不再含 `next_expected_action_hint`**。

- [ ] **Step 1: 写失败测试**

```python
class CorrectionContextTest(unittest.TestCase):
    def test_correction_task_never_carries_an_executable_action(self):
        """纠错提示只能给约束，不能给动作与参数——否则模型照抄即通过，
        却会被记成独立自主规划。"""
        from stage0.agent import HybridPlanner, PlannerPolicyGuard, AgentState, CareEvent
        planner = HybridPlanner(tool_schemas={'rag_search': {'type': 'object',
            'properties': {'query': {'type': 'string'}}, 'required': ['query']}})
        state = AgentState(session_id='s', turn_id='t', event=CareEvent('user_message', 'x'))
        planner.last_rejection = {'proposal': {'decision': 'tool', 'tool': 'rag_search',
                                               'arguments': {'query': '固定搜索词'}},
                                  'errors': [{'code': 'invalid_gap_link', 'category': 'safety',
                                              'message': 'gap 无效'}],
                                  'reason': 'safety_rejection'}
        correction = planner.correction_for(state)
        self.assertIsNotNone(correction)
        self.assertNotIn('next_expected_action_hint', correction)
        blob = json.dumps(correction, ensure_ascii=False)
        self.assertNotIn('固定搜索词', blob,
                         '纠错上下文泄露了被拒提案之外的代码算好的搜索词/参数')
        self.assertNotIn('"tool":', blob)
```

（文件顶部需 `import json`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.CorrectionContextTest -v`
Expected: FAIL — `AssertionError: 'next_expected_action_hint' unexpectedly found`

- [ ] **Step 3: 实现**

```python
    def correction_for(self, state: AgentState) -> dict[str, Any] | None:
        """Structured correction task for the next proposal: WHAT was rejected,
        WHY, WHAT constraints now hold.

        Deliberately carries NO action and NO arguments.  The previous version
        returned the code-computed next action (including its fixed search
        terms, evidence id and top_k) as ``next_expected_action_hint``.  A model
        that simply echoed it passed validation while the trace recorded
        ``source: "llm"`` — i.e. code-supplied planning was counted as
        independent autonomous planning.  Constraints are feedback; the action
        must be the model's own.  The validator re-checks every new proposal
        exactly as before, so nothing here weakens a rule.
        """
        rejection = self.last_rejection
        if not rejection:
            return None
        inv = state.investigation
        allowed = None
        open_gaps = None
        termination_ready = None
        if inv is not None:
            from .investigation import allowed_tools
            allowed = [name for name in allowed_tools(inv) if name != 'respond']
            open_gaps = [g['gap_id'] for g in inv.gaps if g['status'] == 'open']
            termination_ready = bool(inv.termination_reason)
        return {
            'previous_proposal_was_rejected': rejection['proposal'],
            'rejection_reasons': rejection['errors'],
            'allowed_tools_now': allowed,
            'open_gap_ids': open_gaps,
            'termination_ready': termination_ready,
            'instruction': ('上一提案因上述原因被安全代码拒绝且未执行；请自行提出一个不同的、满足要求的动作，'
                            '不要重复被拒提案。约束：工具须取自 allowed_tools_now，'
                            'gap_id 须取自 open_gap_ids，'
                            'respond 仅在 termination_ready 为 true 时可用。'
                            '本提示只给约束，不提供动作或参数。'),
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: PASS

- [ ] **Step 5: 加一条端到端归因断言**

在同一个测试模块追加：用 scripted `proposal_provider` 先返回一个必被拒的提案、再返回一个合法提案，断言**第二次**被记成 `llm_post_correction` 而不是 `llm`。

```python
    def test_post_correction_acceptance_is_labelled(self):
        import tempfile
        from pathlib import Path
        replies = [
            {'decision': 'tool', 'tool': 'rag_search', 'gap_id': 'nope',
             'expected_observation': 'x', 'arguments': {'query': 'q'}},
            {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
             'expected_observation': '获得完整当前事实', 'arguments': {'query': 'snapshot'}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            store.apply_medication_change(action='add', name='氨氯地平', ingredients=[],
                session_id='s', turn_id='t0', source='test', dose='5mg')
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=DDITool(lambda meds: []), rag_tool=_rag([]),
                max_cycles=3, llm_planner_enabled=True,
                proposal_provider=lambda payload: replies.pop(0) if replies else {'decision': 'respond'})
            with patch.dict(os.environ, {'AGENT_INVESTIGATION_ENABLED': '1'}):
                response = agent.handle(CareEvent('user_message', '帮我核对用药'),
                                        session_id='s', turn_id='t1')
            sources = [e['planner']['source'] for e in response.tool_trace
                       if e.get('phase') == 'plan' and e.get('planner')]
            self.assertIn('llm_post_correction', sources, f'sources={sources}')
            store.close()
```

- [ ] **Step 6: 运行并提交**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: PASS

```bash
git add stage0/agent.py stage0/test_agent_visit_prep.py
git commit -m "Correction context: hand back constraints, never a ready-made action"
```

---

## Task 3: `next_action()` 职责四分

**Files:**
- Modify: `stage0/investigation.py`（`next_action` 317-352、`finish` 354-365、`planner_view` 94-112）
- Modify: `stage0/agent.py`（`_decide` 已在 Task 1 改为调用 `forced_stop()`；`run_open_review` 约 3138）
- Test: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Produces: `InvestigationState.forced_stop() -> str | None`（副作用设置 `self.termination_reason`）。
- Produces: `InvestigationState.degraded_next_action() -> ToolAction | None`。
- Produces: `InvestigationState.next_action()` = `degraded_next_action()` 的薄兼容层（既有测试与 `finish()` 继续可用）。
- Produces: `PROTOCOL_VERSION = 'investigation-protocol@2'`。

- [ ] **Step 1: 写失败测试**

```python
class ResponsibilitySplitTest(unittest.TestCase):
    def test_forced_stop_is_pure_and_model_path_leaves_candidates_empty(self):
        """正常（模型）路径不得预填下一步动作或固定搜索词。"""
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对用药相互作用', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': 'x', 'entities': ['氨氯地平'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': []}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                     'description': 'd', 'claim_id': 'claim:a'}]
        self.assertIsNone(inv.forced_stop())
        self.assertEqual(inv.candidates, [], '正常路径不得预填候选动作')

    def test_degraded_path_still_produces_the_scripted_action(self):
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对用药相互作用', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [{'display_name': '氨氯地平'}], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': 'x', 'entities': ['氨氯地平'],
                       'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': []}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'open',
                     'description': 'd', 'claim_id': 'claim:a'}]
        action = inv.degraded_next_action()
        self.assertIsNotNone(action)
        self.assertEqual(action.tool, 'rag_search')
        self.assertIn('氨氯地平', action.arguments['query'])
        self.assertEqual(inv.candidates and inv.candidates[0]['tool'], 'rag_search')

    def test_forced_stop_covers_the_non_negotiable_conditions(self):
        from stage0.investigation import InvestigationState
        inv = InvestigationState('g', 'local-demo')
        inv.authority_read = True
        inv.facts = {'medications': [], 'semantic': [], 'open_conflicts': []}
        inv.claims = []
        inv.gaps = []
        inv.checks = {k: 'checked' for k in inv.checks}
        self.assertEqual(inv.forced_stop(), 'checks_completed')
        self.assertEqual(inv.termination_reason, 'checks_completed')
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.ResponsibilitySplitTest -v`
Expected: FAIL — `AttributeError: 'InvestigationState' object has no attribute 'forced_stop'`

- [ ] **Step 3: 实现**

`investigation.py` — 把现有 `next_action()` 的**前一半**（纯停止条件）与**后一半**（脚本动作）拆开，脚本动作继续持有固定搜索词：

```python
PROTOCOL_VERSION = 'investigation-protocol@2'
```

```python
    # ---- 职责四分（协议 v2）------------------------------------------------
    # forced_stop()          纯状态检查 + 强制停止条件（执行约束，不可协商）
    # model_policy()         由 LLMPlanner 执行——本类不实现
    # degraded_next_action() 确定性降级策略（含固定搜索词；仅在模型路径失效时使用）
    # observe()/sync_authority()  事实读取与状态同步（基础设施）

    def forced_stop(self) -> str | None:
        """Non-negotiable stop conditions only.  Returns and sets
        ``termination_reason``; produces NO action, so it cannot pre-plan.

        Deliberately narrow: an already-set termination, a tool failure, an
        unresolved evidence conflict (code must not vote or let the user pick a
        side), a conflict-free completion, an exhausted search budget, and a
        repeat with no new progress.
        """
        if self.termination_reason:
            return self.termination_reason
        if any(g['kind'] == 'evidence_conflict' and g['status'] == 'open' for g in self.gaps):
            self.termination_reason = 'waiting_review'
            return self.termination_reason
        if (self.claims and all(v == 'checked' for v in self.checks.values())
                and not any(g['status'] == 'open' for g in self.gaps)):
            self.termination_reason = 'checks_completed'
        elif len(self.queries) >= MAX_SEARCHES:
            self.termination_reason = 'budget_insufficient'
        return self.termination_reason

    def degraded_next_action(self):
        """The scripted, deterministic policy — the DEGRADED path only.

        Its fixed search wording and ordering live here on purpose: they are a
        fallback, not the normal planning policy.  The model path must never
        see this action (see ``candidates`` below).
        """
        from .agent import ToolAction
        self.candidates = []
        def action(tool, gap_id, arguments, expected):
            item = ToolAction(tool, 'investigation:' + gap_id, arguments, '解决已记录缺口', gap_id, expected)
            self.candidates = [asdict(item)]
            return item
        if self.termination_reason:
            return None
        if not self.authority_read:
            return action('memory_read', 'authority', {'query': 'snapshot'}, '获得完整当前事实及版本')
        missing = [g for g in self.gaps if g['kind'] == 'patient_fact_missing' and g['status'] == 'open' and g.get('field')]
        if missing:
            self.questions = [{'gap_id': g['gap_id'], 'field': g['field'], 'question': g['description']} for g in missing]
            return action('ask_clarification', missing[0]['gap_id'],
                          {'question': '\n'.join(g['description'] for g in missing)},
                          '等待补充指定字段；未写入临床审批')
        for ref in self.evidence_refs:
            if ref not in self.read_refs:
                claim_id = next((g['gap_id'] for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'),
                                self.claims[0]['claim_id'] if self.claims else 'authority')
                return action('read_evidence', claim_id, {'evidence_id': ref, 'limit': 2000},
                              '回读并校验原文、实体、否定和适用条件')
        stop = self.forced_stop()
        if stop:
            return None
        gap = next((g for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'), None)
        if gap is None:
            self.termination_reason = 'no_progress'
            return None
        claim = next((c for c in self.claims if c['claim_id'] == gap.get('claim_id')),
                     self.claims[0] if self.claims else None)
        terms = ' '.join(claim['entities']) if claim else self.goal[:150]
        suffix = ('药物相互作用 风险', '适用条件 禁忌 否定 相互作用', '证据不足 相互作用 日期')[len(self.queries) % 3]
        return action('rag_search', gap['gap_id'], {'query': terms + ' ' + suffix, 'top_k': 5},
                      '获得新增可核验证据或明确冲突')

    def next_action(self):
        """Thin compatibility shim.  The normal (model) path calls
        ``forced_stop()`` instead; only the degraded path calls this."""
        return self.degraded_next_action()
```

`finish()` 保持不变（它调用 `next_action()`，等价于降级路径）。

`agent.py` 的 `run_open_review` 循环体里，`self._decide(state)` 之后 `inv.termination_reason` 由 `forced_stop` 设置，无需改动。

- [ ] **Step 4: 运行新测试与既有 investigation 回归**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep stage0.test_agent_investigation stage0.test_agent_open_tasks -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/investigation.py stage0/agent.py stage0/test_agent_visit_prep.py
git commit -m "Investigation: split forced stop, model policy and the degraded script"
```

---

## Task 4: `subquestions` 缺口与 `plan_questions` 工具

**Files:**
- Modify: `stage0/investigation.py`（`sync_authority` 209-219、`observe` 223-295、`allowed_tools` 28-47、`proposal_errors` 381-408）
- Modify: `stage0/harness/tools.py`（`PERMISSION_ROLES` 57-68）
- Modify: `stage0/harness/default_tools.py`（新增 spec 与注册）
- Modify: `stage0/agent.py`（`INVESTIGATION_ONLY_TOOLS`、`tool_definitions` 1695-1708、`PlannerPolicyGuard.validate` 1034-1037、`_tool_descriptions` 881-894）
- Test: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Consumes: `forced_stop()`（Task 3）。
- Produces: `InvestigationState.GAP_PLAN = 'subquestions'`；`InvestigationState.subquestion_source: str`；`InvestigationState.allowed_entities() -> set[str]`；`InvestigationState.accept_questions(questions: list[dict]) -> list[str]`（返回错误码列表，空列表=接受）。
- Produces: `PLAN_QUESTIONS_SPEC: ToolSpec`（`name='plan_questions'`，参数 `{"questions": [{"statement": str, "entities": [str]}]}`）。
- Produces: 权限 `"plan:questions"`。

- [ ] **Step 1: 写失败测试**

```python
class SubquestionsTest(unittest.TestCase):
    def _inv(self):
        from stage0.investigation import InvestigationState
        inv = InvestigationState('核对氨氯地平与克拉霉素能否同服', 'local-demo')
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

    def test_model_questions_become_claims_and_close_the_gap(self):
        inv = self._inv()
        errors = inv.accept_questions([
            {'statement': '氨氯地平与克拉霉素的相互作用', 'entities': ['氨氯地平', '克拉霉素']},
        ])
        self.assertEqual(errors, [])
        self.assertEqual(inv.subquestion_source, 'model')
        self.assertEqual([c['source'] for c in inv.claims], ['model'])
        self.assertEqual([g['status'] for g in inv.gaps if g['gap_id'] == 'subquestions'], ['resolved'])

    def test_invented_drug_names_are_refused(self):
        inv = self._inv()
        errors = inv.accept_questions([{'statement': 'x', 'entities': ['氨氯地平', '阿司匹林']}])
        self.assertEqual(errors, ['subquestion_entity_not_in_scope'])
        self.assertEqual(inv.claims, [])

    def test_questions_must_cover_every_authoritative_medication(self):
        inv = self._inv()
        errors = inv.accept_questions([{'statement': 'x', 'entities': ['氨氯地平']}])
        self.assertEqual(errors, ['subquestion_coverage_incomplete'])

    def test_plan_questions_tool_is_only_visible_inside_an_investigation(self):
        from stage0.agent import LLMPlanner, AgentState, CareEvent
        from stage0.harness.default_tools import DEFAULT_TOOL_SPECS, PLAN_QUESTIONS_SPEC
        schemas = {**{n: s.model_schema for n, s in DEFAULT_TOOL_SPECS.items()},
                   PLAN_QUESTIONS_SPEC.name: PLAN_QUESTIONS_SPEC.model_schema}
        planner = LLMPlanner(tool_schemas=schemas)
        state = AgentState(session_id='s', turn_id='t', event=CareEvent('user_message', 'x'))
        names = [f['function']['name'] for f in planner.tool_definitions(state)]
        self.assertNotIn('plan_questions', names)
```

`_StubMemory` 需要提供 `snapshot()` 与 `scope_revision()`：

```python
class _StubMemory:
    def __init__(self, medications=None):
        self._meds = medications or [{'display_name': '氨氯地平', 'ref': 'm1', 'dose': '5mg'},
                                     {'display_name': '克拉霉素', 'ref': 'm2', 'dose': '250mg'}]
    def snapshot(self):
        return {'medications': self._meds, 'semantic': [], 'open_conflicts': []}
    def scope_revision(self, name):
        return 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: FAIL — `AttributeError: 'InvestigationState' object has no attribute 'allowed_entities'`

- [ ] **Step 3: 实现 —— investigation.py**

新增常量与字段：

```python
GAP_PLAN = 'subquestions'
```

`InvestigationState` 新增可加性字段（缺字段时取默认，旧状态照常 `restore`）：

```python
    subquestion_source: str = 'unset'      # 'unset' | 'model' | 'code_default'
    material_refs: list = field(default_factory=list)
    material_read_refs: list = field(default_factory=list)
```

`sync_authority()` 中，把"自动生成药对 claims"整段替换为留下一个规划缺口：

```python
        if not self.claims and meds:
            # 协议 v2：不再由代码穷举药对。子问题归模型；模型不调用
            # plan_questions 时由降级策略补上并标 subquestion_source。
            self.gap(GAP_PLAN, 'plan_missing', '声明本轮要核查的子问题（拆分问题的唯一入口）。')
```

`allowed_entities()` 与 `accept_questions()`：

```python
    def allowed_entities(self) -> set[str]:
        """Entities a sub-question may reference: the authoritative medication
        names plus the names carried by materials staged for this scope.  A
        model may not invent a drug."""
        names = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        names |= {str(q['candidate']['fields'].get('name'))
                  for q in self.material_refs
                  if isinstance(q, dict) and q.get('candidate') and q['candidate'].get('fields', {}).get('name')}
        return names

    def accept_questions(self, questions) -> list[str]:
        """Validate and adopt model-declared sub-questions.  Returns a list of
        error codes; empty means accepted.  Rejections never fall back to a
        code-authored substitute — the model must satisfy the contract or the
        degraded policy is used (and labelled)."""
        if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_CLAIMS:
            return ['invalid_subquestion_count']
        allowed = self.allowed_entities()
        normalised = []
        for item in questions:
            if not isinstance(item, dict) or not isinstance(item.get('statement'), str) \
                    or not item['statement'].strip() or len(item['statement']) > 200:
                return ['invalid_subquestion_statement']
            entities = item.get('entities')
            if not isinstance(entities, list) or not entities or any(not isinstance(e, str) for e in entities):
                return ['invalid_subquestion_entities']
            if any(e not in allowed for e in entities):
                return ['subquestion_entity_not_in_scope']
            normalised.append({'statement': item['statement'].strip(), 'entities': list(entities)})
        covered = {e for item in normalised for e in item['entities']}
        required = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        if not required.issubset(covered):
            return ['subquestion_coverage_incomplete']
        self.claims = []
        for item in normalised:
            identifier = 'claim:' + digest(item)[:12]
            self.claims.append({'claim_id': identifier, 'statement': item['statement'],
                'entities': item['entities'], 'status': 'insufficient', 'supporting_evidence': [],
                'opposing_evidence': [], 'source_status': 'unknown', 'condition_status': 'unknown',
                'source': 'model'})
            self.gap(identifier, 'evidence_missing', '核查' + '、'.join(item['entities']) + '的支持和反对证据',
                     claim_id=identifier)
        self.subquestion_source = 'model'
        for g in self.gaps:
            if g['gap_id'] == GAP_PLAN:
                g['status'] = 'resolved'
        return []
```

（注意 `digest(item)` 后不要有空格——写成 `digest(item)[:12]`。）

`allowed_tools()`：在 authority 读完后暴露规划工具：

```python
    allowed = ['memory_write', 'memory_read']
    if any(g['gap_id'] == GAP_PLAN and g['status'] == 'open' for g in inv.gaps):
        allowed.append('plan_questions')
```

`observe()` 中处理 `plan_questions` 观察：

```python
        if observation.tool == 'plan_questions':
            errors = self.accept_questions(observation.arguments.get('questions'))
            if errors:
                self.gap('plan:' + ','.join(errors), 'plan_missing',
                         '子问题声明未通过校验：' + ','.join(errors))
                self.termination_reason = 'no_progress'
            return
```

`proposal_errors()` 新增两条规则（在 gap 链接检查之后）：

```python
    if tool == 'plan_questions':
        if gap['gap_id'] != GAP_PLAN:
            return ['plan_questions_only_for_subquestions_gap']
        return []
```

- [ ] **Step 4: 实现 —— 工具与权限**

`stage0/harness/tools.py` 的 `PERMISSION_ROLES` 增加：

```python
    "plan:questions": frozenset({"caregiver", "ops", "reviewer"}),
    "materials:read": frozenset({"caregiver", "ops", "reviewer"}),
```

`stage0/harness/default_tools.py` 新增：

```python
PLAN_QUESTIONS_SPEC = ToolSpec(
    name="plan_questions",
    description=("声明本轮核查的子问题——拆分问题的唯一入口。每条子问题的 entities 必须取自权威药单"
                 "或已上传材料的候选药名，且必须覆盖权威药单的每个药名；证据变化时可再次调用以修订。"
                 "该工具只在 investigation 的 subquestions 缺口打开时可用。"),
    argument_schema={
        "type": "object",
        "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
            "type": "object",
            "properties": {"statement": {"type": "string"},
                           "entities": {"type": "array", "items": {"type": "string"}}},
            "required": ["statement", "entities"]}}},
        "required": ["questions"],
    },
    result_shape="dict(accepted, questions, allowed_entities, subquestion_source)",
    kind="read",
    required_permission="plan:questions",
    idempotency="pure",
)
```

在 `build_default_executor` 的 `for name, spec in DEFAULT_TOOL_SPECS.items()` 循环之后注册：

```python
    def _plan_questions_handler(request):
        # 纯回显：状态变更只发生在 InvestigationState.observe()，与 read_evidence
        # 一样由状态机从 observation.arguments 读取，执行器不写状态。
        questions = request.arguments.get("questions")
        inv = getattr(request.state, "investigation", None)
        return {"accepted": True, "questions": questions,
                "allowed_entities": sorted(inv.allowed_entities()) if inv is not None else [],
                "subquestion_source": "model"}

    executor.register(PLAN_QUESTIONS_SPEC, _plan_questions_handler)
```

`stage0/agent.py`：把 `plan_questions` 加进描述表与 investigation-only 集合：

```python
INVESTIGATION_ONLY_TOOLS = frozenset({'plan_questions'})
```

`_tool_descriptions()` 的元组追加 `PLAN_QUESTIONS_SPEC`：

```python
    from .harness.default_tools import (BATCH_READ_SPEC, DELEGATE_TASK_SPEC,
                                        PLAN_QUESTIONS_SPEC, READ_EVIDENCE_SPEC)
    for spec in (READ_EVIDENCE_SPEC, BATCH_READ_SPEC, DELEGATE_TASK_SPEC, PLAN_QUESTIONS_SPEC):
```

`LLMPlanner.tool_definitions()` 在无 investigation 时过滤掉 investigation-only 工具：

```python
        else:
            permitted = [name for name in self.tool_schemas
                         if name not in INVESTIGATION_ONLY_TOOLS]
```

`PlannerPolicyGuard.validate()` 在 `unknown_tool` 检查之后加一条协议规则：

```python
        if tool in INVESTIGATION_ONLY_TOOLS and state.investigation is None:
            reject("investigation_only_tool",
                   f"{tool} is only valid inside an investigation", "protocol")
            return ProposalValidation(False, errors)
```

- [ ] **Step 5: 运行测试与回归**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep stage0.test_agent_investigation stage0.test_agent_open_tasks stage0.test_planner_reliability -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add stage0/investigation.py stage0/harness/tools.py stage0/harness/default_tools.py stage0/agent.py stage0/test_agent_visit_prep.py
git commit -m "Sub-questions belong to the model; the code keeps only the degraded fallback"
```

---

## Task 5: 材料索引与材料读取工具

**Files:**
- Modify: `stage0/product.py`（新增 `MaterialIndex` 于 `ProductStore` 之后）
- Modify: `stage0/harness/default_tools.py`（`LIST_MATERIALS_SPEC`、`READ_MATERIAL_ITEM_SPEC`、`register_material_tools`）
- Modify: `stage0/agent.py`（`attach_material_index`）
- Modify: `stage0/care_tasks.py`（`_execute_evidence_review` 约 295 挂载适配器）
- Test: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Consumes: 权限 `"materials:read"`（Task 4）。
- Produces: `MaterialIndex(store, scope_id=SCOPE)`，方法 `index() -> dict`、`item(case_id, item_id) -> dict`。
- Produces: `LIST_MATERIALS_SPEC`（无必填参数）、`READ_MATERIAL_ITEM_SPEC`（必填 `case_id`、`item_id`）。
- Produces: `MedicationCoordinatorAgent.attach_material_index(index) -> None`（幂等）。
- Produces: `register_material_tools(executor, index) -> None`。

- [ ] **Step 1: 写失败测试**

```python
class MaterialVisibilityTest(unittest.TestCase):
    def _store(self, directory):
        from stage0.memory import MemoryStore
        from stage0.product import ProductStore
        from stage0.care_tasks import CareTasks
        memory = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
        return ProductStore(memory), CareTasks

    def test_index_exposes_deterministic_diff_without_raw_bytes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            store, _ = self._store(directory)
            store.memory.apply_medication_change(action='add', name='氨氯地平', ingredients=[],
                session_id='s', turn_id='t', source='test', dose='5mg', schedule='每日一次')
            case = store.import_csv('k1', 'name,dose,unit,schedule,date,subject\n'
                                          '氨氯地平,10,mg,每日一次,2026-01-05,local-demo\n')
            from stage0.product import MaterialIndex
            index = MaterialIndex(store)
            blob = json.dumps(index.index(), ensure_ascii=False)
            self.assertIn(case['id'], blob)
            self.assertIn('changed', blob, '差异 kind 必须对模型可见')
            self.assertIn('location', blob.lower())
            self.assertNotIn('raw_base64', blob)
            self.assertNotIn('base64', blob)

    def test_material_tools_are_absent_until_an_index_is_attached(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            agent = MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda m: []), rag_tool=_rag([]))
            self.assertNotIn('list_materials', agent.executor.catalog())
            from stage0.product import MaterialIndex, ProductStore
            agent.attach_material_index(MaterialIndex(ProductStore(store)))
            self.assertIn('list_materials', agent.executor.catalog())
            self.assertIn('read_material_item', agent.executor.catalog())
            agent.attach_material_index(MaterialIndex(ProductStore(store)))  # idempotent
            store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.MaterialVisibilityTest -v`
Expected: FAIL — `ImportError: cannot import name 'MaterialIndex' from 'stage0.product'`

- [ ] **Step 3: 实现 `MaterialIndex`（`stage0/product.py`）**

```python
class MaterialIndex:
    """Read-only adapter: what the agent may see of uploaded materials.

    Materials are staged, caregiver-unconfirmed candidates.  This adapter
    exposes the deterministic diff ``recompute()`` already computed (kind +
    issues + source coordinates) so the model can decide WHICH discrepancy to
    investigate; it never exposes the raw document bytes and never writes.
    """

    def __init__(self, store: 'ProductStore', scope_id: str = SCOPE):
        self._store = store
        self._scope_id = scope_id

    @staticmethod
    def _fields(candidate):
        return dict(candidate.get('fields') or {})

    def index(self) -> dict:
        materials = []
        for case in self._store.objects('case'):
            items = []
            for item in case.get('items', []):
                candidate = item.get('candidate')
                items.append({
                    'item_id': item['item_id'],
                    'fields': self._fields(candidate) if candidate else {},
                    'locations': dict((candidate or {}).get('locations') or {}),
                    'kind': item.get('kind'),
                    'issues': list(item.get('issues') or []),
                    'current': [m.get('ref') for m in (item.get('current') or [])],
                    'confirmed': item.get('status') != 'pending',
                })
            materials.append({
                'case_id': case['id'], 'document_id': case.get('document_id'),
                'parser_version': case.get('parser_version'), 'created_at': case.get('created_at'),
                'status': case.get('status'), 'item_count': len(items),
                'pending_count': sum(1 for i in items if not i['confirmed']),
                'items': items,
            })
        return {'materials': materials, 'revision': self._store.memory.scope_revision('materials')}

    def item(self, case_id, item_id) -> dict:
        case = self._store.get(case_id, 'case')
        item = next((i for i in case.get('items', []) if i['item_id'] == item_id), None)
        if item is None:
            raise ProductError('材料条目不存在', 404)
        candidate = item.get('candidate') or {}
        return {'case_id': case_id, 'item_id': item_id,
                'fields': self._fields(candidate),
                'original_fields': dict(candidate.get('original_fields') or {}),
                'corrections': list(candidate.get('corrections') or []),
                'locations': dict(candidate.get('locations') or {}),
                'kind': item.get('kind'), 'issues': list(item.get('issues') or []),
                'status': item.get('status'),
                'document_id': case.get('document_id'), 'parser_version': case.get('parser_version')}
```

- [ ] **Step 4: 实现工具规格与注册**

```python
LIST_MATERIALS_SPEC = ToolSpec(
    name="list_materials",
    description=("列出本轮已上传材料及其确定性差异：每条目的字段、原文定位、与当前权威记录的差异"
                 "（kind: same/changed/new/possible_duplicate/not_listed/unresolved）和未决问题。"
                 "条目是 caregiver 尚未确认的候选；索引里的条目不等于已核实事实，"
                 "要作为引用必须先 read_material_item 读原文。"),
    argument_schema={"type": "object", "properties": {}, "required": []},
    result_shape="dict(materials[{case_id, document_id, item_count, pending_count, items[]}], revision)",
    kind="read", required_permission="materials:read", idempotency="pure", cacheable=True,
)

READ_MATERIAL_ITEM_SPEC = ToolSpec(
    name="read_material_item",
    description=("按 case_id/item_id 读取单条材料的原文与定位，含 caregiver 已做的更正历史。"
                 "只有读过原文的条目才能作为报告引用。"),
    argument_schema={"type": "object", "properties": {"case_id": {"type": "string"},
                                                      "item_id": {"type": "string"}},
                     "required": ["case_id", "item_id"]},
    result_shape="dict(case_id, item_id, fields, original_fields, corrections, locations, kind, issues)",
    kind="read", required_permission="materials:read", idempotency="pure", cacheable=True,
)


def register_material_tools(executor: ToolExecutor, index: Any) -> None:
    """Register the two read-only material tools.  Called only when a
    MaterialIndex is attached, so runs without one keep the exact catalog they
    had before — no unavailable tool is advertised."""
    executor.register(LIST_MATERIALS_SPEC, lambda request: index.index())
    executor.register(READ_MATERIAL_ITEM_SPEC,
                      lambda request: index.item(request.arguments["case_id"],
                                                 request.arguments["item_id"]))
```

- [ ] **Step 5: 实现 `attach_material_index`（`stage0/agent.py`）**

```python
    def attach_material_index(self, index: Any) -> None:
        """Make uploaded materials visible to the planner.  Idempotent; the
        planner catalog is rebound so the guard and the prompt see the same
        tool set as the executor."""
        if getattr(self, "material_index", None) is index:
            return
        self.material_index = index
        from .harness.default_tools import register_material_tools
        register_material_tools(self.executor, index)
        schemas = self.executor.catalog()
        if isinstance(self.planner, HybridPlanner):
            self.planner.bind_tools(schemas)
```

在 `__init__` 末尾（`self.memory.recheck_hook` 赋值之后）加 `self.material_index = None`，并把描述表补上两个新 spec：

```python
    from .harness.default_tools import (BATCH_READ_SPEC, DELEGATE_TASK_SPEC,
                                        LIST_MATERIALS_SPEC, PLAN_QUESTIONS_SPEC,
                                        READ_EVIDENCE_SPEC, READ_MATERIAL_ITEM_SPEC)
    for spec in (READ_EVIDENCE_SPEC, BATCH_READ_SPEC, DELEGATE_TASK_SPEC,
                 PLAN_QUESTIONS_SPEC, LIST_MATERIALS_SPEC, READ_MATERIAL_ITEM_SPEC):
```

- [ ] **Step 6: 挂到产品路径（`stage0/care_tasks.py`）**

`_execute_evidence_review` 在调用 `run_open_review` 之前：

```python
        agent = self.agent()
        from .product import MaterialIndex
        agent.attach_material_index(MaterialIndex(self.p))
        result = agent.run_open_review(
```

（把原来的 `result = self.agent().run_open_review(` 改成上面的两行 + `result = agent.run_open_review(`。）

- [ ] **Step 7: 运行测试与回归并提交**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep stage0.test_product_tasks stage0.test_agent_open_tasks -v`
Expected: 全部 PASS

```bash
git add stage0/product.py stage0/harness/default_tools.py stage0/agent.py stage0/care_tasks.py stage0/test_agent_visit_prep.py
git commit -m "Materials: let the model see uploaded materials and their deterministic diff"
```

---

## Task 6: 就诊准备报告五问与解释的证据支持检查

**Files:**
- Modify: `stage0/investigation.py`（`report_text` 367-378；`observe` 的 `read_evidence` 分支 255-295）
- Modify: `stage0/care_tasks.py`（`_review_markdown` 426-437）
- Test: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Consumes: `claims[].source == 'model'`（Task 4）、`material_read_refs` 字段（Task 4）。
- Produces: `InvestigationState.verify_statements() -> list[dict]`，返回未能核实的解释条目 `[{'claim_id', 'statement', 'reason'}]`。
- Produces: `InvestigationState.report_text()` 增加"就诊准备五问"与"待确认"分节。

- [ ] **Step 1: 写失败测试**

```python
class ReportEvidenceTest(unittest.TestCase):
    def test_model_statement_without_read_evidence_is_demoted_to_pending(self):
        from stage0.investigation import InvestigationState
        inv = InvestigationState('g', 'local-demo')
        inv.facts = {'medications': [], 'semantic': [], 'open_conflicts': []}
        inv.claims = [{'claim_id': 'claim:a', 'statement': '两药合用会导致严重出血，必须停药',
                       'entities': ['氨氯地平'], 'status': 'supported',
                       'supporting_evidence': [], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'model'}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'resolved',
                     'description': 'd', 'claim_id': 'claim:a'}]
        inv.checks = {k: 'checked' for k in inv.checks}
        inv.termination_reason = 'checks_completed'
        pending = inv.verify_statements()
        self.assertEqual([p['claim_id'] for p in pending], ['claim:a'])
        text = inv.report_text()
        self.assertIn('待确认', text)
        self.assertNotIn('两药合用会导致严重出血，必须停药', text.split('待确认', 1)[0],
                         '未核实的解释不得出现在结论区')
        for marker in ('解决了什么', '来源支持', '材料', '缺少依据', '医生'):
            self.assertIn(marker, text)

    def test_material_read_refs_count_as_citations(self):
        from stage0.investigation import InvestigationState
        inv = InvestigationState('g', 'local-demo')
        inv.facts = {'medications': [], 'semantic': [], 'open_conflicts': []}
        inv.material_read_refs = ['case:1/item:1']
        inv.claims = [{'claim_id': 'claim:a', 'statement': '材料记的剂量与当前记录不一致',
                       'entities': ['氨氯地平'], 'status': 'supported',
                       'supporting_evidence': ['case:1/item:1'], 'opposing_evidence': [],
                       'source_status': 'unknown', 'condition_status': 'unknown', 'source': 'model'}]
        inv.gaps = [{'gap_id': 'claim:a', 'kind': 'evidence_missing', 'status': 'resolved',
                     'description': 'd', 'claim_id': 'claim:a'}]
        inv.checks = {k: 'checked' for k in inv.checks}
        inv.termination_reason = 'checks_completed'
        self.assertEqual(inv.verify_statements(), [])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.ReportEvidenceTest -v`
Expected: FAIL — `AttributeError: 'InvestigationState' object has no attribute 'verify_statements'`

- [ ] **Step 3: 实现**

`investigation.py` —— 材料读取进入引用集；`read_evidence` 的既有权衡不变，新增材料分支：

```python
        if observation.tool == 'read_material_item':
            ref = f"{observation.arguments.get('case_id')}/{observation.arguments.get('item_id')}"
            if ref not in self.material_read_refs:
                self.material_read_refs.append(ref)
            return
```

```python
    def verify_statements(self) -> list[dict]:
        """Evidence-support check for MODEL-authored explanations.

        A claim statement written by the model is only a conclusion when the
        evidence it cites was actually READ BACK in this run — the same rule the
        label-evidence path already enforces ('found' is not 'read and
        verified').  Anything else is demoted to a question for the visit, never
        silently deleted and never rendered as a finding.
        """
        read = set(self.read_refs) | set(self.material_read_refs)
        pending = []
        for claim in self.claims:
            if claim.get('source') != 'model':
                continue
            refs = set(claim.get('supporting_evidence') or []) | set(claim.get('opposing_evidence') or [])
            if claim.get('status') == 'insufficient' or not refs:
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'no_read_evidence'})
            elif not refs.issubset(read):
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'citation_not_read_back'})
        self.pending_statements = pending
        return pending
```

`InvestigationState` 新增字段 `pending_statements: list = field(default_factory=list)`。

`report_text()` 重写为五问结构：

```python
    def report_text(self):
        self.verify_statements()
        checked = [k for k, value in self.checks.items() if value == 'checked']
        labels = {'authority': '权威用药及关键事实', 'interaction_evidence': '标签证据',
                  'applicability': '材料适用条件'}
        pending_ids = {p['claim_id'] for p in self.pending_statements}
        lines = ['# 就诊准备报告', '', f'目标：{self.goal}', '']
        lines += ['## 1. 本次调查解决了什么',
                  '已核查范围：' + ('、'.join(labels[k] for k in checked) or '尚无完成项') + '。',
                  '终止原因：' + str(self.termination_reason) + '。', '']
        lines += ['## 2. 有来源支持的事实', '']
        supported = [c for c in self.claims if c['status'] != 'insufficient' and c['claim_id'] not in pending_ids]
        for claim in supported:
            lines.append(f"- {claim['statement']}：{claim['status']}。"
                         f"支持引用：{', '.join(claim['supporting_evidence']) or '无'}；"
                         f"反对引用：{', '.join(claim['opposing_evidence']) or '无'}。")
        if not supported:
            lines.append('- 本契约内尚无可作为结论的事实。证据不足不等于没有风险。')
        lines.append('')
        lines += ['## 3. 不同材料之间的差异', '']
        material_gaps = [g for g in self.gaps if g.get('kind') in {'evidence_conflict', 'material_conflict'}]
        lines += [f"- {g['description']}" for g in material_gaps] or \
                 ['- 本次未在已读取材料间发现可记录的差异；未读取的材料不在此列。']
        lines.append('')
        lines += ['## 4. 仍缺少依据的问题', '']
        open_gaps = [g['description'] for g in self.gaps if g['status'] == 'open']
        lines += [f'- {item}' for item in open_gaps] or ['- 本契约内没有剩余缺口。']
        lines += [f"- 未核实的解释（待确认）：{p['statement']}（原因：{p['reason']}）"
                  for p in self.pending_statements]
        lines.append('')
        lines += ['## 5. 就诊时可以向医生或药师确认什么', '']
        lines += [f'- {q["question"]}' for q in self.questions] or \
                 ['- 可将本报告的差异与未决项逐条向医生或药师确认。']
        lines += ['', '这份报告仅说明有界核查结果；insufficient/unknown 不是无风险，'
                      '未列药物不代表停药。本系统不做诊断、处方或用药调整建议。']
        return '\n'.join(lines)
```

`care_tasks._review_markdown` 去掉重复的标题（报告自带 H1），保留任务包装：

```python
    def _review_markdown(self, task, inv):
        from .investigation import InvestigationState
        lines = [f'<!-- care task {task["id"]} -->', f'目标：{task["goal"]}', '',
                 InvestigationState.restore(inv, SCOPE).report_text()]
```

- [ ] **Step 4: 运行测试与相关回归并提交**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep stage0.test_agent_open_tasks stage0.test_product_tasks -v`
Expected: 全部 PASS

```bash
git add stage0/investigation.py stage0/care_tasks.py stage0/test_agent_visit_prep.py
git commit -m "Report: five visit-prep answers, and explanations must survive an evidence check"
```

---

## Task 7: 合成任务集与 A/B 评测器

**Files:**
- Create: `stage0/agent_evals/visitprep_dev.json`（12 个任务，6 族 × 2 成对）
- Create: `stage0/agent_evals/run_visitprep.py`
- Test: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Consumes: 全部前序任务。
- Produces: 任务 schema（`visitprep-task@1`）与 `run_visitprep.evaluate(task, arm) -> dict`。

- [ ] **Step 1: 写任务集**

每个任务形如（成对任务只改 `material_cases` 里的一条关键证据与 `expected`，**用户问题逐字相同**）：

```json
{
  "schema_version": "visitprep-task@1",
  "task_id": "vp-diff-001a",
  "family_id": "material_dose_differs",
  "pair_id": "vp-diff-001",
  "goal": "我下周去看心内科，帮我把这几份材料和我现在的用药对一下，看有什么要对医生说。",
  "initial_state": {"medications": [{"name": "氨氯地平", "dose": "5mg", "date": "2025-11-02"}]},
  "material_cases": [{"csv": "name,dose,unit,schedule,date,subject\n氨氯地平,10,mg,每日一次,2026-01-05,local-demo\n"}],
  "materials": [
    {"chunk_id": "c1", "drug_name": "氨氯地平", "section": "用法用量",
     "text": "氨氯地平常用剂量为每日一次5mg，最大剂量10mg；剂量调整需监测血压。"}
  ],
  "expected": {
    "must_read_material": true,
    "expected_diff_kind": "changed",
    "must_ask_fields": [],
    "forbid_supported_when_absent": true,
    "required_report_sections": ["2. 有来源支持的事实", "3. 不同材料之间的差异", "5. 就诊时可以向医生或药师确认什么"],
    "allowed_terminal_reasons": ["checks_completed", "budget_insufficient", "no_progress"]
  },
  "budget": {"max_cycles": 10, "wall_clock_seconds": 120, "token_budget": 150000, "call_budget": 32}
}
```

对照臂 `vp-diff-001b` 只把 CSV 的剂量改成与当前记录一致（`5,mg`），`expected_diff_kind` 改为 `same`。

六族与成对差异点（每族两个任务，两两成对）：

| 族 | pair_id | a 臂 | b 臂（唯一变量） |
|---|---|---|---|
| 信息完整 | `vp-full-001` | 材料与记录一致，有标签证据 | 标签证据缺失 |
| 关键事实缺失 | `vp-missing-002` | 材料缺单位 | 材料缺日期 |
| 多来源冲突 | `vp-conflict-003` | 标签甲说"增加风险" | 标签乙说"未见增加" |
| 新旧版本差异 | `vp-diff-004` | 材料剂量 10mg（与记录 5mg 不同） | 材料剂量 5mg（相同） |
| 首次检索无结果 | `vp-noresult-005` | 首轮语料无命中，改写后命中 | 首轮即命中 |
| 无关/长材料干扰 | `vp-distract-006` | 材料含 3 条无关药行 | 材料只含相关药行 |

**不得**按 `task_id` 或文件名写专用分支——评分只读 `expected` 字段。

- [ ] **Step 2: 写失败测试**

```python
class VisitPrepEvalTest(unittest.TestCase):
    def test_dataset_covers_six_families_twelve_tasks_in_pairs(self):
        import json
        from pathlib import Path
        data = json.loads((Path(__file__).with_name('agent_evals')
                           / 'visitprep_dev.json').read_text(encoding='utf-8'))
        self.assertEqual(len(data), 12)
        self.assertEqual(len({t['family_id'] for t in data}), 6)
        pairs = {}
        for task in data:
            pairs.setdefault(task['pair_id'], []).append(task)
        self.assertTrue(all(len(v) == 2 for v in pairs.values()), '每个 pair_id 恰好两个任务')

    def test_paired_tasks_differ_only_in_evidence(self):
        import json
        from pathlib import Path
        data = json.loads((Path(__file__).with_name('agent_evals')
                           / 'visitprep_dev.json').read_text(encoding='utf-8'))
        pairs = {}
        for task in data:
            pairs.setdefault(task['pair_id'], []).append(task)
        for pair_id, (left, right) in pairs.items():
            self.assertEqual(left['goal'], right['goal'],
                             f'{pair_id}: 成对任务的用户问题必须逐字一致')
            self.assertEqual(left['initial_state'], right['initial_state'],
                             f'{pair_id}: 成对任务的患者事实必须一致')
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.VisitPrepEvalTest -v`
Expected: FAIL — `FileNotFoundError: visitprep_dev.json`

- [ ] **Step 4: 实现评测器**

`run_visitprep.py` 复用 `run_eval.py` 的隔离方式（temp dir + `MemoryStore` + `ProductStore`），**两臂同源**：

```python
ARMS = {
    # A: HEAD 的当前固定工作流——确定性规划器，无 investigation
    'fixed':  {'AGENT_INVESTIGATION_ENABLED': '0', 'llm_planner_enabled': False},
    # B: 协议 v4 的模型调查路径
    'model':  {'AGENT_INVESTIGATION_ENABLED': '1', 'llm_planner_enabled': True},
}
```

评分（先定后跑，写在模块 docstring 里）：

```python
def evaluate(task, observed):
    expected = task['expected']
    failures, notes = [], []
    report = observed.get('report_markdown') or ''
    # 1 目标达成
    if observed.get('error'):
        failures.append('execution_error')
    # 2 关键事实覆盖：必须触发差异发现
    if expected.get('expected_diff_kind'):
        if observed.get('diff_kinds_seen') is None:
            failures.append('no_material_index_read')
        elif expected['expected_diff_kind'] not in observed['diff_kinds_seen']:
            failures.append('expected_diff_not_found')
    # 3 引用支持：报告中每条结论必须带实际回读的引用
    if observed.get('unsupported_statements'):
        failures.append('unsupported_conclusion')
    # 4 冲突识别
    if expected.get('must_report_conflict') and '不同材料之间的差异' not in report:
        failures.append('conflict_section_missing')
    # 5 必要补问
    asked = set(observed.get('asked_fields') or [])
    if not set(expected.get('must_ask_fields', [])).issubset(asked):
        failures.append('necessary_question_missing')
    if asked - set(expected.get('must_ask_fields', [])):
        failures.append('unnecessary_question')
    # 6 错误结论 / 7 无效调用
    if expected.get('forbid_supported_when_absent') and observed.get('supported_claims'):
        failures.append('unsupported_claim')
    if observed.get('invalid_calls'):
        failures.append('invalid_tool_call')
    # 8-10 耗时、成本、降级率单独统计，不参与 pass/fail
    return {'passed': not failures, 'failures': failures, 'notes': notes,
            'terminal_reason': observed.get('termination_reason'),
            'question_recall': {'num': len(set(expected.get('must_ask_fields', [])) & asked),
                                'den': len(expected.get('must_ask_fields', []))},
            'wall_ms': observed.get('wall_ms'), 'planner_calls': observed.get('planner_calls'),
            'degraded': bool(observed.get('degraded_reason')),
            'attribution': observed.get('attribution')}
```

`attribution` 直接聚合 trace 里的 `planner.source` 五个类别——**工具调用次数与路径相似度不进任何指标**。

- [ ] **Step 5: 跑离线 A/B**

Run: `.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm fixed --out output/visit-prep-2026-09-12/offline-fixed.json`
Run: `.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm model --out output/visit-prep-2026-09-12/offline-model.json`
Expected: 两臂各 12 条逐任务结果 + 汇总；`real_model_quality: "unavailable"` 明确标注

- [ ] **Step 6: 提交**

```bash
git add stage0/agent_evals/visitprep_dev.json stage0/agent_evals/run_visitprep.py stage0/test_agent_visit_prep.py
git commit -m "Visit-prep eval: twelve paired tasks and a two-arm rubric fixed in advance"
```

---

## Task 8: 真实模型批次与交付报告

**Files:**
- Create: `scripts/run-visitprep-live.py`
- Create: `docs/agent-capability-upgrade/visit-prep-2026-09-12/RESULT.md`

**Interfaces:**
- Consumes: Task 7 的 `run_visitprep`。
- Produces: `output/visit-prep-2026-09-12/live/`（`effective-config.json`、`manifest.json`、逐任务 JSON）。

- [ ] **Step 1: 写 live 入口（带硬上限）**

沿用 `scripts/run-planner-live-acceptance-v3.py` 的纪律：`--enable-live` 显式开关、`out.mkdir(exist_ok=False)` 拒绝复用目录、派发前写 manifest、`assert_live_authorized()` 解析实际端点而非复述常量。

```python
CALL_CAP = 400            # 全局硬上限：超出即停止采样并如实报告
PLANNED_K = 1
PER_TASK_CALLS = 12
SECONDS_PER_TASK = 180
```

每完成一个任务就把累计 `planner_calls` 写进 manifest；达到 `CALL_CAP` 立即停止，**未跑的任务标记为 `not_sampled`，不省略、不补跑**。

- [ ] **Step 2: 跑批次**

Run: `.venv/Scripts/python.exe scripts/run-visitprep-live.py --enable-live --out output/visit-prep-2026-09-12/live`

- [ ] **Step 3: 写 RESULT.md**

必须包含，且不得省略任何一项：

1. 修改内容与关键代码位置（file:line）；
2. 可操作的演示入口（`/tasks` 发起 `evidence_review` 待办的完整步骤）与可复现命令；
3. 固定流程 vs 模型路径的**任务质量对照表**（逐任务，不用百分位）；
4. 归因四类分别计数：模型选择 / 系统强制操作 / 模型纠错 / 策略降级；
5. 延迟口径三行分开：**成功回合**延迟、**全部回合**耗时、超时与失败数；
6. **至少两个"固定流程会遗漏/浪费步骤而模型调整后改善"的具体案例**，每个附 trace 引用；若实验未发现，**如实写"未发现"**；
7. 已完成 / 失败 / **未验证**三类清单；
8. 结论：是否产生可证明的 Agent 增益及其证据；**允许"不达标"**。

- [ ] **Step 4: 提交**

```bash
git add scripts/run-visitprep-live.py docs/agent-capability-upgrade/visit-prep-2026-09-12/RESULT.md
git commit -m "Visit-prep: live cohort within a hard call cap, and an honest result"
```

---

## 自检

**Spec 覆盖**：§三 3.1→Task 3｜3.2→Task 4｜3.3→Task 2｜3.4→Task 1｜§四→Task 5｜§五→Task 5 Step 6｜§六→Task 6｜§七→Task 7/8。

**命名一致性**：`forced_stop` / `degraded_next_action` / `accept_questions` / `allowed_entities` / `verify_statements` / `subquestion_source` / `material_read_refs` / `attach_material_index` / `MaterialIndex.index()` / `MaterialIndex.item()` / `register_material_tools` 在全部任务中拼写一致。

**已知未覆盖**（如实记录，不假装完成）：
- `frontend/` 不改：报告新分节的渲染沿用既有 Markdown 组件，未做界面级验收。
- `--live` 的成对任务全集受 400 次调用上限约束；实际覆盖数在 `RESULT.md` 逐条列出。
- 独立 held-out 仍然缺失（`product_evals/tasks/held_out/` 为空），任何泛化主张都必须标注为 unavailable。
