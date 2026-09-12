# 可信验收与调查闭环修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"通过"重新可证伪——修掉 24 个恒真检查、把批次调用上限接进真实请求入口、让计划修订与"解释须有证据"两条闭环真正闭合。

**Architecture:** 评分协议抽成独立的纯函数模块（`agent_evals/scoring.py`），可脱离重跑重评历史产物；批次额度建在**持久账本**（`llm_attempts`，发送前写入）这个只增计数之上，由 `BudgetSession.call` 在每次发送前取额；材料条目收敛成**一种规范形状**（`material_items`），写入端与读取端共用；计划修订以**追加式历史**实现，并由"不得删除未解决实体集"这一条强制防止伪造完成；解释↔证据校验作为独立命名 scope `claim-support@1` 叠加在既有 `conservative-lexical-v1` 之上，不覆盖它。

**Tech Stack:** Python 3.13、`unittest`（仓库既有风格，**不是** pytest）、SQLite、FastAPI、React（本轮不改 `frontend/` 组件，只做浏览器验收）。

**Spec:** `docs/agent-capability-upgrade/verification-2026-09-12/DESIGN.md`

## Global Constraints

- 测试运行方式（仓库既定）：`.venv/Scripts/python.exe -m unittest stage0.<module> -v`。**不要**用 pytest 语法写断言，不要引入 pytest 依赖。
- 测试必须**纯离线**：不发起任何远程调用。真实模型只在 `--live` 显式入口下使用。
- 只使用白名单内端点；`assert_live_authorized()` 是唯一闸门，不得绕过。不得把密钥写进任何产物（只记 `provider`/`model`/`base_url`）。
- 既有约束**一行不删**：`PlannerPolicyGuard` 的安全不变量、`EvidenceStore` 的来源/哈希/作用域校验、`turn_budget` 的预算与租约、写操作的幂等回执。
- 持久化形状**只增不改**：`InvestigationState.VERSION` 保持 `investigation@1`、`CONTRACT` 保持 `medication-evidence-review@1`。新增字段一律带默认值。
- **预算计数器只增不减**（`merge_budget` 对 `COUNTERS` 取 `max()`）。任何"余额"都不得实现为可递减的可变状态。
- 评分器**不得**识别具体 `task_id`、族名或文件名；规则一律从 `task['expected']` 读出。既有测试 `test_the_evaluator_has_no_per_task_branching` 必须继续通过。
- 历史产物**只读**：重评输出到新文件，不覆盖、不改写旧结论。
- 不新增多 Agent、不做框架迁移、不改 `AGENT_INVESTIGATION_ENABLED` 产品默认值、不改 `frontend/src/`。
- 医学边界不变：不诊断、不处方、不建议开始/停止用药或调整剂量；`insufficient`/`unknown` 不等于"没有风险"。
- 每个 Task 结束必须 `git commit`，提交信息用仓库既有的英文主题行风格。

---

### Task 1: 评分模块骨架与三条轴

**Files:**
- Create: `stage0/agent_evals/scoring.py`
- Create: `stage0/test_visitprep_scoring.py`
- Modify: `stage0/agent_evals/run_visitprep.py:118-184`（`evaluate` 改为委托）

**Interfaces:**
- Consumes: `task['expected']`、`observed`（由 `run_task` 产出）
- Produces:
  - `PROTOCOL = 'visitprep-eval@2'`
  - `section_body(report: str, heading: str) -> str`
  - `has_content(body: str) -> bool`
  - `classify_terminal(task: dict, observed: dict) -> str` → `'completed' | 'waiting' | 'stopped' | 'absent'`
  - `score_report_quality(task: dict, observed: dict) -> dict` → `{'ok': bool, 'failures': list[str]}`
  - `score_autonomy(observed: dict) -> bool`
  - `score_outcome(task: dict, observed: dict) -> dict`（Task 2/3/4 会往 `score_report_quality` 里加规则）

- [ ] **Step 1: 写失败测试**

创建 `stage0/test_visitprep_scoring.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_scoring -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage0.agent_evals.scoring'`

- [ ] **Step 3: 实现 scoring.py**

```python
"""visitprep-eval@2 —— 三条独立轴，五项互斥计数。

为什么另开一个模块而不是原地改 ``run_visitprep.evaluate``：评分必须能**脱离
重跑**重评历史产物，也必须能被负向对照单测直接调用。

本模块不读任何具体 ``task_id``、族名或文件名——规则一律来自
``task['expected']``，与 ``run_visitprep`` 的既有约定一致。
"""
from __future__ import annotations

import re

PROTOCOL = 'visitprep-eval@2'

# 终态分类。``allowed_terminal_reasons`` 说明"允许停在哪"，
# 但"完整完成"只能由 completed 与 waiting 取得——预算耗尽与 provider
# 失败不得自动算完整完成。
COMPLETION_REASONS = {'checks_completed'}
WAITING_REASONS = {'waiting_review', 'waiting_input'}
STOPPED_REASONS = {'budget_insufficient', 'no_progress', 'cancelled', 'unrecoverable_failure'}

# ``report_text()`` 的空态句子。它们占着一节的位置却没有内容，
# 所以"标题存在"不等于"这一节写了东西"。
PLACEHOLDERS = (
    '本次未在已读取的材料与记录之间发现可记录的差异',
    '本契约内没有剩余缺口。',
    '本次没有得到可作为结论的事实',
    '尚无完成项',
)

_HEADING = re.compile(r'^##\s+(?P<title>.+?)\s*$', re.MULTILINE)


def section_body(report: str, heading: str) -> str:
    """``## <heading>`` 到下一个 ``## `` 之间的正文；找不到标题返回空串。"""
    matches = list(_HEADING.finditer(report or ''))
    for index, match in enumerate(matches):
        if match.group('title').strip() == heading.strip():
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
            return report[start:end]
    return ''


def has_content(body: str) -> bool:
    """正文里有非占位条目才算有内容。"""
    for line in (body or '').splitlines():
        line = line.strip()
        if not line or not line.startswith('-'):
            continue
        if any(placeholder in line for placeholder in PLACEHOLDERS):
            continue
        return True
    return False


def classify_terminal(task: dict, observed: dict) -> str:
    """允许集合 + 完成/等待/停止三分类。"""
    reason = observed.get('termination_reason')
    if not reason:
        return 'absent'
    allowed = set((task.get('expected') or {}).get('allowed_terminal_reasons') or [])
    if allowed and reason not in allowed:
        return 'stopped'
    if reason in COMPLETION_REASONS:
        return 'completed'
    if reason in WAITING_REASONS:
        return 'waiting'
    return 'stopped'


def score_report_quality(task: dict, observed: dict) -> dict:
    """内容规则。Task 2 与 Task 3 会往这里加可达性检查。"""
    expected = task.get('expected') or {}
    report = observed.get('report_markdown') or ''
    failures = []
    if observed.get('error'):
        failures.append('execution_error')

    for section in expected.get('required_report_sections') or []:
        if not has_content(section_body(report, section)):
            failures.append('empty_or_missing_section:' + section)

    if observed.get('invalid_calls'):
        failures.append('invalid_tool_call')
    return {'ok': not failures, 'failures': failures}


def score_autonomy(observed: dict) -> bool:
    """无策略降级、子问题归模型、零 fallback。"""
    if observed.get('degraded_reason'):
        return False
    if observed.get('subquestion_source') != 'model':
        return False
    return not (observed.get('attribution') or {}).get('policy_fallback')


def score_outcome(task: dict, observed: dict) -> dict:
    """三条轴 + 五项互斥计数之一。``undetermined`` 是并列标记，不占计数。"""
    if observed.get('undetermined'):
        return {'protocol': PROTOCOL, 'undetermined': True,
                'undetermined_reasons': list(observed.get('undetermined') or [])}

    quality = score_report_quality(task, observed)
    terminal = classify_terminal(task, observed)
    autonomous = score_autonomy(observed)

    if observed.get('error'):
        bucket = 'execution_failed_or_not_sampled'
    elif observed.get('degraded_reason') or observed.get('subquestion_source') != 'model':
        bucket = 'degraded_outcome'
    elif quality['ok'] and terminal in {'completed', 'waiting'} and autonomous:
        bucket = 'autonomous_without_degradation'
    elif quality['ok'] and terminal in {'completed', 'waiting'}:
        bucket = 'report_quality_pass'
    else:
        bucket = 'terminal_expected' if terminal != 'stopped' else 'report_quality_pass'

    return {
        'protocol': PROTOCOL,
        'report_quality': quality,
        'terminal_state': terminal,
        'autonomy': autonomous,
        'complete': bool(quality['ok'] and terminal == 'completed' and autonomous),
        'bucket': bucket,
    }
```

**注意**：`bucket` 的五项互斥在 Task 4 收敛为最终版；本步先让它可用。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_scoring -v`
Expected: PASS（6 个用例）

- [ ] **Step 5: 让 run_visitprep 委托给 scoring**

把 `run_visitprep.py:118-184` 的 `evaluate` 换成薄委托（保留 `wall_ms`、`planner_calls` 等既有字段名，避免下游断链）：

```python
def evaluate(task, observed):
    """The rubric. Delegates to the versioned scoring protocol."""
    from . import scoring
    outcome = scoring.score_outcome(task, observed)
    return {
        **outcome,
        'passed': bool(outcome.get('complete')),
        'failures': (outcome.get('report_quality') or {}).get('failures', []),
        'terminal_reason': observed.get('termination_reason'),
        'wall_ms': observed.get('wall_ms'),
        'planner_calls': observed.get('planner_calls'),
        'degraded': bool(observed.get('degraded_reason')),
        'degraded_reason': observed.get('degraded_reason'),
        'attribution': observed.get('attribution') or {},
    }
```

- [ ] **Step 6: 跑既有评测器测试，确认没有被改坏**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: FAIL（`test_provider_failures_are_broken_down_by_exception_type` 与若干严格断言会因通过率口径变化而失败）。**记录失败的用例名**——它们是 Task 11 要按新口径更新的对象，本步**不要**为了让它们通过而放宽新评分器。

- [ ] **Step 7: Commit**

```bash
git add stage0/agent_evals/scoring.py stage0/test_visitprep_scoring.py stage0/agent_evals/run_visitprep.py
git commit -m "Scoring: three axes and five counts, and a heading is not evidence"
```

---

### Task 2: 冲突检查改为正文 + 真实分歧 + 具名双方

**Files:**
- Modify: `stage0/agent_evals/scoring.py`（`score_report_quality`）
- Modify: `stage0/investigation.py:405-408`（缺口记录双方）、`:685-688`（第 3 节渲染）
- Modify: `stage0/agent_evals/run_visitprep.py:305-346`（`observed` 增 `material_conflicts`）
- Test: `stage0/test_visitprep_scoring.py`

**Interfaces:**
- Consumes: `observed['material_conflicts']` = `[{'ref': str, 'kind': str, 'counterparts': [str, ...]}]`
- Produces: `score_report_quality` 新增失败码 `conflict_not_reported`、`conflict_side_missing`

- [ ] **Step 1: 写失败测试**

追加到 `stage0/test_visitprep_scoring.py`：

```python
class ConflictTest(unittest.TestCase):
    DIFF_LINE = '- 材料 case:1/item:2（changed）与当前记录 ref:med:7 存在差异\n'

    def _task(self):
        return _task(must_report_conflict=True)

    def test_a_heading_with_a_placeholder_body_does_not_report_a_conflict(self):
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n')
        observed = _observed(report_markdown=report,
                             material_conflicts=[{'ref': 'case:1/item:2', 'kind': 'changed',
                                                  'counterparts': ['ref:med:7']}])
        self.assertIn('conflict_not_reported',
                      scoring.score_report_quality(self._task(), observed)['failures'])

    def test_a_body_that_denies_the_conflict_does_not_count(self):
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 材料 case:1/item:2 与当前记录 ref:med:7 一致，没有差异。\n')
        observed = _observed(report_markdown=report,
                             material_conflicts=[{'ref': 'case:1/item:2', 'kind': 'changed',
                                                  'counterparts': ['ref:med:7']}])
        self.assertIn('conflict_not_reported',
                      scoring.score_report_quality(self._task(), observed)['failures'])

    def test_reporting_only_one_side_fails(self):
        report = '## 3. 不同材料之间的差异\n\n- 材料 case:1/item:2 的剂量不同\n'
        observed = _observed(report_markdown=report,
                             material_conflicts=[{'ref': 'case:1/item:2', 'kind': 'changed',
                                                  'counterparts': ['ref:med:7']}])
        self.assertIn('conflict_side_missing',
                      scoring.score_report_quality(self._task(), observed)['failures'])

    def test_both_sides_named_passes(self):
        report = '## 3. 不同材料之间的差异\n\n' + self.DIFF_LINE
        observed = _observed(report_markdown=report,
                             material_conflicts=[{'ref': 'case:1/item:2', 'kind': 'changed',
                                                  'counterparts': ['ref:med:7']}])
        self.assertEqual(scoring.score_report_quality(self._task(), observed)['failures'], [])

    def test_a_conflict_that_was_never_actually_observed_is_not_required(self):
        """synthetic-ok：没有真实分歧时，占位句是合法内容。"""
        report = ('## 3. 不同材料之间的差异\n\n'
                  '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。\n')
        self.assertEqual(
            scoring.score_report_quality(self._task(), _observed(report_markdown=report))['failures'],
            [])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_scoring.ConflictTest -v`
Expected: FAIL — `KeyError`/`AssertionError`，`conflict_not_reported` 尚未实现

- [ ] **Step 3: 实现冲突检查**

在 `scoring.py` 的 `score_report_quality` 内、`required_report_sections` 循环之后插入：

```python
    conflicts = observed.get('material_conflicts') or []
    if expected.get('must_report_conflict') and conflicts:
        body = section_body(report, '3. 不同材料之间的差异')
        named = [line for line in (body or '').splitlines()
                 if line.strip().startswith('-')
                 and not any(p in line for p in PLACEHOLDERS)]
        if not named:
            failures.append('conflict_not_reported')
        else:
            # 一条真实分歧必须在报告里出现，且**双方具名**：材料条目一侧
            # 与它所对比的当前记录一侧。只写"材料 X 有差异"是把一个
            # 反对来源写成了半句话——读者无法去核对另一边。
            ok = False
            for conflict in conflicts:
                for line in named:
                    if conflict['ref'] in line and all(
                            counterpart in line for counterpart in conflict.get('counterparts') or []):
                        ok = True
            if not ok:
                any_ref = any(conflict['ref'] in line for line in named)
                failures.append('conflict_side_missing' if any_ref else 'conflict_not_reported')
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_scoring.ConflictTest -v`
Expected: PASS（5 个用例）

- [ ] **Step 5: 让报告真的写出双方（否则新检查恒假）**

在 `stage0/investigation.py:399-408`，把 `material_conflict` 缺口的描述与 details 改为带对方 ref。`read_material_item` 的返回不含 `current`，所以对方 ref 取自 `material_items`。

> **本步必须同时声明 `material_items` 字段**（Task 6 才在 `observe()` 里填充它）。不声明就会在 `read_material_item` 分支上 `AttributeError`——T2 先于 T6 执行。字段本身是任务 6 的**规范形状**，这里只提前把容器放好：

```python
    # 已"看到"的材料条目，唯一规范形状（ref -> {'name','kind','current'}）。
    # Task 6 在这里写入；Task 2 的差异渲染已经要读它。
    material_items: dict = field(default_factory=dict)
```

本步先写入 `counterparts`（Task 6 之前恒为空列表，`all([])` 为真，检查仍可满足但较弱）：

```python
            if kind and kind != 'same':
                issues = [str(item) for item in (detail.get('issues') or [])]
                counterparts = list((self.material_items.get(ref) or {}).get('current') or [])
                self.gap('material:' + ref, 'material_conflict',
                         f"材料 {ref} 与当前记录"
                         + ('（' + '、'.join(counterparts) + '）' if counterparts else '')
                         + f"的差异：{kind}"
                         + ('；未决问题：' + '、'.join(issues) if issues else ''),
                         material_ref=ref, kind_detail=kind, counterparts=counterparts)
```

**第 3 节渲染（`:686-687`）不需要改**——它渲染的就是缺口的 `description`，而上一段已把双方写进了 `description`。本步**不要**动 `report_text()`，避免与 Task 10 的改动冲突。用下面这条命令确认双方确实进了报告：

Run: `.venv/Scripts/python.exe -c "from stage0.investigation import InvestigationState as I; inv=I('g','local-demo'); inv.gap('material:case:1/item:2','material_conflict','材料 case:1/item:2 与当前记录（ref:med:7）的差异：changed', material_ref='case:1/item:2', kind_detail='changed', counterparts=['ref:med:7']); print(inv.report_text().split('## 3',1)[1].split('## 4',1)[0])"`
Expected: 输出里同时出现 `case:1/item:2` 与 `ref:med:7`

- [ ] **Step 6: 在 observed 里带上真实分歧**

在 `run_visitprep.py` 的 `outcome` 字典（`:305-346`）中新增一项：

```python
                    'material_conflicts': [
                        {'ref': g.get('material_ref'),
                         'kind': g.get('kind_detail'),
                         'counterparts': list(g.get('counterparts') or [])}
                        for g in (investigation.get('gaps') or [])
                        if g.get('kind') == 'material_conflict'],
```

- [ ] **Step 7: 运行既有材料可见性测试**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.MaterialVisibilityTest -v`
Expected: PASS（缺口描述变化不破坏既有断言；若 `test_index_exposes_the_deterministic_diff_and_source_coordinates` 失败，检查 `counterparts` 是否在无 `material_items` 时仍为 `[]`）

- [ ] **Step 8: Commit**

```bash
git add stage0/agent_evals/scoring.py stage0/agent_evals/run_visitprep.py stage0/investigation.py stage0/test_visitprep_scoring.py
git commit -m "Conflicts: a section heading is not a finding, and both sides must be named"
```

---

### Task 3: 引用正确性 — `claim-support@1`

**Files:**
- Create: `stage0/claim_support.py`
- Create: `stage0/test_claim_support.py`
- Modify: `stage0/investigation.py:446-486`（回读时计算）、`:488-506`（`_assess` 汇总）、`:612-635`（`verify_statements` 降级）、`:676-684`（第 2 节谓词）

**Interfaces:**
- Produces:
  - `SCOPE = 'claim-support@1'`
  - `assess_support(*, statement: str, quote: str, entities: list[str], material_item: dict | None = None) -> dict` → `{'scope', 'status', 'reasons'}`
  - `status ∈ {'supported_by_span', 'no_span', 'field_mismatch', 'not_applicable'}`
- Consumes: 既有 `evidence_quality.assess_claim`（不变）

- [ ] **Step 1: 写失败测试**

创建 `stage0/test_claim_support.py`：

```python
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


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_claim_support -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage0.claim_support'`

- [ ] **Step 3: 实现 claim_support.py**

```python
"""claim-support@1 —— 被引用的证据是否**支持**该断言，而非仅仅"被读取过"。

独立命名 scope，**叠加**在 ``evidence_quality.conservative-lexical-v1`` 之上，
不覆盖它：后者决定一条 claim 有没有支持/反对（决定 ``status``），本 scope
决定该断言是否落在它所引用的**具体片段**里。分开的理由是历史 assessments
不能因为口径升级而集体失效——缺本 scope 的旧记录按 ``not_applicable`` 恢复。

同药名、或"引用被回读过"，都不足以支持整个断言。
"""
from __future__ import annotations

import re

SCOPE = 'claim-support@1'

# 剂量/日期类数字事实。单位可省，但数字本身必须对得上。
_FACT = re.compile(r'\d+(?:\.\d+)?\s*(?:mg|μg|ug|g|ml|毫克|克|毫升|片|粒|单位)?', re.IGNORECASE)


def _facts(text: str) -> set[str]:
    return {re.sub(r'\s+', '', item).lower() for item in _FACT.findall(text or '')}


def assess_support(*, statement: str, quote: str, entities, material_item=None) -> dict:
    """Return ``{'scope', 'status', 'reasons'}``.

    ``supported_by_span`` 是本 scope 唯一的肯定判定；任何其他值都意味着
    该断言**不得**作为结论渲染。
    """
    quote = quote or ''
    if not quote.strip():
        return {'scope': SCOPE, 'status': 'not_applicable',
                'reasons': ['no_evidence_body']}

    reasons = []
    if material_item is not None and str(material_item.get('kind') or '') == 'same' \
            and re.search(r'差异|不同|不一致', statement or ''):
        # 材料与当前记录被判定为 same，而断言在说它们不同。
        return {'scope': SCOPE, 'status': 'field_mismatch',
                'reasons': ['material_kind_is_same']}

    missing = [entity for entity in (entities or []) if entity not in quote]
    if missing:
        reasons.append('entity_absent_from_span:' + ','.join(map(str, missing)))

    absent_facts = sorted(fact for fact in _facts(statement) if fact not in _facts(quote)
                          and fact not in re.sub(r'\s+', '', quote).lower())
    if absent_facts:
        reasons.append('fact_absent_from_span:' + ','.join(absent_facts))

    if reasons:
        return {'scope': SCOPE, 'status': 'no_span', 'reasons': reasons}
    return {'scope': SCOPE, 'status': 'supported_by_span', 'reasons': []}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_claim_support -v`
Expected: PASS（5 个用例）

- [ ] **Step 5: 接进回读路径**

在 `stage0/investigation.py:472-477`，`assess_claim` 之后追加（注意 `assessment` 是 dict，新增键是**追加式**的）：

先在模块顶部与既有 `from .evidence_quality import assess_claim`（`:15`）并列加一行 import——**不要**放进循环里逐次 import：

```python
from .claim_support import assess_support
```

再在 `assess_claim(...)` 调用之后追加：

```python
                support = assess_support(statement=claim['statement'], quote=text,
                                         entities=claim['entities'])
                assessment['support_status'] = support['status']
                assessment['support_scope'] = support['scope']
                assessment['support_reasons'] = support['reasons']
```

- [ ] **Step 6: 在 `_assess` 汇总 claim 级 support_status**

在 `stage0/investigation.py:493-495` 之后插入：

```python
            support_assessments = [a for ref, a in assessments.items() if a['status'] == 'supported']
            spans = [a.get('support_status') for a in support_assessments]
            if not support_assessments:
                claim['support_status'] = 'unknown'
            elif all(span is None for span in spans):
                # 旧记录：本 scope 之前采集的 assessment。按"不可判定"恢复，
                # 不当作"不支持"——口径升级不该追溯否定历史结论。
                claim['support_status'] = 'not_applicable'
            elif any(span == 'supported_by_span' for span in spans):
                claim['support_status'] = 'supported_by_span'
            else:
                claim['support_status'] = 'no_supporting_span'
```

并在 `_new_claim`（`:210-212`）的初始字典里加一个默认键：

```python
            'source_status': 'unknown', 'condition_status': 'unknown', 'source': source,
            'support_status': 'unknown'})
```

- [ ] **Step 7: 写失败测试——引用真实但不支持断言必须被拒**

追加到 `stage0/test_agent_visit_prep.py` 的 `ReportEvidenceTest` 类：

```python
    def test_a_citation_that_does_not_support_the_statement_is_not_a_conclusion(self):
        """引用真实存在、也确实回读过，但引用体里没有这个数字——不算支持。"""
        inv = _finished_investigation()
        inv.evidence_refs = ['ev-1']
        inv.read_refs = ['ev-1']
        inv.claims = [{'claim_id': 'claim:a', 'statement': '氨氯地平剂量为10mg',
                       'entities': ['氨氯地平'], 'status': 'supported',
                       'supporting_evidence': ['ev-1'], 'opposing_evidence': [],
                       'source_status': 'current', 'condition_status': 'verified',
                       'source': 'model', 'support_status': 'no_supporting_span'}]
        pending = inv.verify_statements()
        self.assertEqual([p['reason'] for p in pending], ['citation_does_not_support_statement'])
        text = inv.report_text()
        self.assertNotIn('氨氯地平剂量为10mg', text.split('## 4', 1)[0],
                         '证据不支持的断言不得出现在结论区')
```

- [ ] **Step 8: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.ReportEvidenceTest.test_a_citation_that_does_not_support_the_statement_is_not_a_conclusion -v`
Expected: FAIL — `[] != ['citation_does_not_support_statement']`

- [ ] **Step 9: 实现降级与第 2 节谓词**

在 `verify_statements`（`:612-635`）的 refs 检查之后追加分支：

```python
            elif claim.get('support_status') == 'no_supporting_span':
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'citation_does_not_support_statement'})
```

并把第 2 节谓词（`:676-677`）改为：

```python
        concluded = [claim for claim in self.claims
                     if claim['status'] == 'supported'
                     and claim.get('support_status') in {'supported_by_span', 'not_applicable'}
                     and claim['claim_id'] not in pending_ids]
```

- [ ] **Step 10: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.ReportEvidenceTest -v`
Expected: PASS（含新增用例；`test_a_citation_that_was_never_read_back_does_not_count` 仍通过）

- [ ] **Step 11: Commit**

```bash
git add stage0/claim_support.py stage0/test_claim_support.py stage0/investigation.py stage0/test_agent_visit_prep.py
git commit -m "Claim support: a citation that was read is not a citation that supports"
```

---

### Task 4: 负向对照补齐 + 历史重评

**Files:**
- Modify: `stage0/agent_evals/scoring.py`（`score_outcome` 收敛 + `rescore`）
- Modify: `stage0/test_visitprep_scoring.py`
- Create: `stage0/agent_evals/rescore.py`

**Interfaces:**
- Produces:
  - `score_outcome` 的 `bucket` 收敛为五项互斥
  - `rescore(artifact: dict) -> dict` → 每条任务带 `protocol`/`undetermined`/`not_comparable_to`
  - CLI：`python -m stage0.agent_evals.rescore --in <dir> --out <dir>`

- [ ] **Step 1: 写失败测试**

追加到 `stage0/test_visitprep_scoring.py`：

```python
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

    def test_a_perfect_report_after_budget_exhaustion_is_not_complete(self):
        outcome = scoring.score_outcome(_task(), _observed(termination_reason='budget_insufficient'))
        self.assertFalse(outcome['complete'])


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

    def test_a_complete_old_record_is_rescored_without_guessing(self):
        artifact = {'protocol': 'visitprep-eval@1', 'arm': 'fixed', 'tasks': [
            {'task_id': 't1', 'family_id': 'f', 'observed': {
                'report_markdown': '## 2. 有来源支持的事实\n\n- 有内容\n',
                'termination_reason': 'checks_completed',
                'subquestion_source': 'model', 'attribution': {}}}]}
        out = scoring.rescore(artifact)
        self.assertEqual(out['tasks'][0]['terminal_state'], 'completed')
        self.assertFalse(out['tasks'][0]['undetermined'])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_scoring -v`
Expected: FAIL — `AttributeError: module 'stage0.agent_evals.scoring' has no attribute 'rescore'`

- [ ] **Step 3: 实现 score_outcome 的最终五项互斥 + rescore**

用下面替换 `score_outcome` 的 bucket 段：

```python
    degraded = bool(observed.get('degraded_reason')) \
        or observed.get('subquestion_source') != 'model' \
        or bool((observed.get('attribution') or {}).get('policy_fallback'))

    if observed.get('error'):
        bucket = 'execution_failed_or_not_sampled'
    elif degraded:
        # 降级优先于质量：一个由确定性兜底**产出动作**的回合，即使报告好看，
        # 也不是"自主达成"。把它记进 report_quality_pass 会把降级读成成功。
        # （T1 的评审发现 policy_fallback 会漏进 report_quality_pass；本条为修正。）
        bucket = 'degraded_outcome'
    elif quality['ok'] and terminal == 'completed' and autonomous:
        bucket = 'autonomous_without_degradation'
    elif quality['ok'] and terminal in {'completed', 'waiting'}:
        bucket = 'report_quality_pass'
    elif terminal in {'completed', 'waiting'}:
        bucket = 'terminal_expected'
    else:
        bucket = 'execution_failed_or_not_sampled'
```

**配套负向对照**（加进 `NegativeControlTest`）：`attribution={'policy_fallback': 1}` 且报告完美、终态 `checks_completed` 时，bucket 必须是 `degraded_outcome` 而非 `report_quality_pass`。

并在 `scoring.py` 追加 rescore：

```python
# 重评历史产物时**必须**在场的字段。缺任何一项都记 undetermined：
# 补造证据比承认不可判定更糟。
_REQUIRED_OBSERVED = ('report_markdown', 'termination_reason')


def rescore(artifact: dict, task_index: dict | None = None) -> dict:
    """用本协议重评一份既有产物。不覆盖、不改写旧结论。"""
    tasks = []
    for item in artifact.get('tasks') or []:
        observed = dict(item.get('observed') or {})
        missing = [key for key in _REQUIRED_OBSERVED if observed.get(key) is None]
        entry = {'task_id': item.get('task_id'), 'family_id': item.get('family_id'),
                 'arm': item.get('arm') or artifact.get('arm'), 'protocol': PROTOCOL,
                 'original_score': item.get('score')}
        if missing:
            entry.update({'undetermined': True,
                          'undetermined_reasons': ['missing:' + key for key in missing]})
        else:
            task = (task_index or {}).get(item.get('task_id')) or {'expected': {}}
            entry.update(score_outcome(task, observed))
            entry['undetermined'] = False
        tasks.append(entry)
    return {
        'protocol': PROTOCOL,
        'original_protocol': artifact.get('protocol'),
        'arm': artifact.get('arm'),
        'not_comparable_to': ('旧产物在 visitprep-eval@1 下采集，缺 v2 所需字段时'
                              '只能记 undetermined；口径不同，不得与 v2 批次逐格比较。'),
        'tasks': tasks,
        'summary': {
            'total': len(tasks),
            'undetermined': sum(1 for t in tasks if t.get('undetermined')),
            'by_bucket': {b: sum(1 for t in tasks if t.get('bucket') == b)
                          for b in ('autonomous_without_degradation', 'report_quality_pass',
                                    'terminal_expected', 'degraded_outcome',
                                    'execution_failed_or_not_sampled')},
        },
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_scoring -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 写重评入口**

创建 `stage0/agent_evals/rescore.py`：

```python
"""用当前评分协议重评既有产物，输出到**新**目录。

旧产物一律只读：重评写新文件，并标注 rescored_by 与 not_comparable_to。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import scoring

DATA = Path(__file__).with_name('visitprep_dev.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--in-dir', required=True)
    parser.add_argument('--out-dir', required=True)
    args = parser.parse_args()
    index = {task['task_id']: task
             for task in json.loads(DATA.read_text(encoding='utf-8'))}
    source, target = Path(args.in_dir), Path(args.out_dir)
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob('*.json')):
        artifact = json.loads(path.read_text(encoding='utf-8'))
        result = scoring.rescore(artifact, index)
        result['rescored_by'] = scoring.PROTOCOL
        result['source_file'] = path.name
        (target / path.name).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"{path.name}: {json.dumps(result['summary'], ensure_ascii=False)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 6: 对真实历史产物跑一次重评**

Run: `.venv/Scripts/python.exe -m stage0.agent_evals.rescore --in-dir output/visit-prep-2026-09-12 --out-dir output/verification-2026-09-12/rescored`
Expected: 每个文件打印一行 summary；`live-model.json` 等缺 `subquestion_source` 的文件应出现非零 `undetermined`

- [ ] **Step 7: 确认旧产物未被修改**

Run: `git status --short output/visit-prep-2026-09-12`
Expected: 空输出（旧产物未被触碰）

- [ ] **Step 8: Commit**

```bash
git add stage0/agent_evals/scoring.py stage0/agent_evals/rescore.py stage0/test_visitprep_scoring.py
git commit -m "Rescore: negative controls for every check, and history re-scored without guessing"
```

---

### Task 5: 批次额度接进真实请求入口

**Files:**
- Create: `stage0/agent_evals/batch_budget.py`
- Create: `stage0/test_visitprep_budget.py`
- Modify: `stage0/agent_evals/run_visitprep.py:216-356`（`run_task` 签名与 finally 入账）、`:386-393`（`main` 循环）

**Interfaces:**
- Produces:
  - `class BatchAllowance`：`cap`、`spent`、`remaining()`、`exhausted()`、`charge(n)`
  - `attempts_sent(memory, run_id) -> int`（读持久账本）
- Consumes: `llm_attempts` 表（`memory.py:580`），行由 `reserve_llm_attempt` 在 dispatch **之前**写入

- [ ] **Step 1: 写失败测试**

创建 `stage0/test_visitprep_budget.py`：

```python
"""批次调用上限的边界：cap=1、多次重试、跨任务共享、异常退出仍留账。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from stage0.agent_evals.batch_budget import BatchAllowance, attempts_sent
from stage0.memory import MemoryStore


class BatchAllowanceTest(unittest.TestCase):
    def test_cap_one_admits_exactly_one_charge(self):
        allowance = BatchAllowance(1)
        self.assertFalse(allowance.exhausted())
        allowance.charge(1)
        self.assertTrue(allowance.exhausted())
        self.assertEqual(allowance.remaining(), 0)

    def test_cross_task_charges_accumulate(self):
        allowance = BatchAllowance(5)
        allowance.charge(2)
        allowance.charge(2)
        self.assertEqual(allowance.remaining(), 1)
        allowance.charge(1)
        self.assertTrue(allowance.exhausted())

    def test_a_zero_cap_is_exhausted_before_anything_runs(self):
        self.assertTrue(BatchAllowance(0).exhausted())

    def test_remaining_never_goes_negative(self):
        allowance = BatchAllowance(1)
        allowance.charge(9)
        self.assertEqual(allowance.remaining(), 0)


class LedgerTest(unittest.TestCase):
    def test_the_ledger_counts_attempts_written_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                store.workflow_run_start(run_id='r1', thread_id='r1', event_id=None,
                                         idempotency_key=None, graph_version='legacy')
                budget = {'cycles_consumed': 0}
                store.reserve_llm_attempt('a1', 'r1', 'planner', 10, 1.0, budget)
                store.reserve_llm_attempt('a2', 'r1', 'planner', 10, 1.0, budget)
                self.assertEqual(attempts_sent(store, 'r1'), 2)
                self.assertEqual(attempts_sent(store, 'other'), 0)
            finally:
                store.close()

    def test_an_unsettled_attempt_still_counts(self):
        """异常退出时预留行仍在——这正是"已发生的调用不得丢失"的依据。"""
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                store.workflow_run_start(run_id='r1', thread_id='r1', event_id=None,
                                         idempotency_key=None, graph_version='legacy')
                store.reserve_llm_attempt('a1', 'r1', 'planner', 10, 1.0, {'cycles_consumed': 0})
                self.assertEqual(len(store.unsettled_llm_attempts('r1')), 1)
                self.assertEqual(attempts_sent(store, 'r1'), 1)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_budget -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage0.agent_evals.batch_budget'`

- [ ] **Step 3: 实现 batch_budget.py**

```python
"""批次级调用额度：把剩余额度送进**真实请求入口**，而不是在任务之间估算。

单次任务的额度由 ``BudgetSession.call`` 在**每次发送前**取用（重试也各取一次），
所以一个任务不可能越过交给它的额度。

记账读**持久账本** ``llm_attempts``：该行由 ``reserve_llm_attempt`` 在 dispatch
**之前**写入，因此任务抛异常、甚至进程被杀，已发生的调用记录都还在。

本模块不维护可递减的余额——本仓的预算计数器只增不减（``merge_budget`` 对
``COUNTERS`` 取 ``max()``），余额一旦可减，就会被 checkpoint 恢复撤销。
"""
from __future__ import annotations


class BatchAllowance:
    """A batch's call allowance, charged from the durable ledger between tasks."""

    def __init__(self, cap: int):
        self.cap = max(0, int(cap))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.cap - self.spent)

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def charge(self, attempts) -> None:
        self.spent += max(0, int(attempts or 0))

    def to_dict(self) -> dict:
        return {'cap': self.cap, 'spent': self.spent, 'remaining': self.remaining()}


def attempts_sent(memory, run_id: str) -> int:
    """Dispatch attempts recorded for a run. Written before the call is made."""
    row = memory.connection.execute(
        'SELECT COUNT(*) FROM llm_attempts WHERE run_id=?', (run_id,)).fetchone()
    return int(row[0]) if row else 0
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_visitprep_budget -v`
Expected: PASS（6 个用例）

- [ ] **Step 5: 把额度接进 run_task**

`run_visitprep.py` 的 `run_task` 签名改为：

```python
def run_task(task, arm, live=False, allowance=None):
```

在 `env` 字典（`:242-249`）里，把写死的调用预算改为按剩余额度下发，并在批次约束下关闭 refusal refund：

```python
        call_budget = 32 if allowance is None else allowance.remaining()
        env = {'AGENT_INVESTIGATION_ENABLED': config['investigation'],
               'AGENT_TURN_BUDGET_SECONDS': '180', 'AGENT_TURN_TOKEN_BUDGET': '150000',
               'AGENT_TURN_CALL_BUDGET': str(call_budget), 'LLM_MAX_RETRIES': '0',
               'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0',
               'AGENT_SUBQUESTION_PLANNER': 'model' if config.get('script') else ''}
        if allowance is not None:
            # 429 退还不适用于批次约束：退款允许尝试数达到 2×call_budget，
            # 那会让"批次上限"变成计费调用上限而不是发送上限。
            env['PLANNER_PROVIDER_REFUND_REFUSALS'] = '0'
```

- [ ] **Step 6: 在 finally 里入账（异常路径不丢账）**

把 `run_task` 里 `store.close()`（`:350`）之前的结构改为：

```python
            try:
                response = agent.handle(...)
                ...  # 既有 outcome 构造，原样保留
            except Exception as exc:
                outcome = {'error': f'{type(exc).__name__}: {exc}',
                           'invalid_calls': 0, 'planner_calls': 0}
            finally:
                # 已发生的调用必须留下记录：账本行写在 dispatch 之前，
                # 所以异常退出也能读到真实尝试数。
                outcome['provider_attempts_sent'] = attempts_sent(store, 'visitprep')
```

并在文件头 import：

```python
from .batch_budget import BatchAllowance, attempts_sent
```

- [ ] **Step 7: 改写 main 的额度循环**

把 `run_visitprep.py:386-393` 换成：

```python
    allowance = BatchAllowance(args.call_cap) if args.live else None
    results, not_sampled = [], []
    for task in tasks:
        if allowance is not None and allowance.exhausted():
            not_sampled.append(task['task_id'])
            continue
        result = run_task(task, args.arm, live=args.live, allowance=allowance)
        if allowance is not None:
            allowance.charge(result['observed'].get('provider_attempts_sent'))
        results.append(result)
    spent = allowance.spent if allowance is not None else 0
```

并把 `report` 里的 `'planner_calls_spent': spent` 保留为**账本口径**，另加一行说明：

```python
        'planner_calls_spent': spent,          # 账本口径：实际发送的尝试数
        'call_budget': allowance.to_dict() if allowance is not None else None,
```

- [ ] **Step 8: 跑离线臂确认没被改坏**

Run: `.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm scripted --out output/verification-2026-09-12/offline-scripted.json`
Expected: 正常产出（`allowance` 为 None，离线路径不受影响）

- [ ] **Step 9: Commit**

```bash
git add stage0/agent_evals/batch_budget.py stage0/test_visitprep_budget.py stage0/agent_evals/run_visitprep.py
git commit -m "Batch cap: the allowance reaches the request, and an exception keeps its ledger"
```

---

### Task 6: 材料条目统一为一种形状（C1）

**Files:**
- Modify: `stage0/investigation.py:109-110`（新增字段）、`:188-200`（`allowed_entities`）、`:382-390`（`observe`）
- Modify: `stage0/test_agent_visit_prep.py:332-338`（手工拼装 → 全链路）

**Interfaces:**
- Produces: `InvestigationState.material_items: dict[str, dict]` → `ref -> {'name', 'kind', 'current'}`
- Consumes: `MaterialIndex.index()` 的 item 形状（`item['fields']['name']`、`item['current']`）

- [ ] **Step 1: 写失败测试（全链路，不手工拼装）**

替换 `stage0/test_agent_visit_prep.py:332-338` 的 `test_material_candidate_names_count_as_allowed_entities`：

```python
    def test_material_candidate_names_count_as_allowed_entities(self):
        """材料候选药名也允许——否则模型无法就材料里的差异提问。

        走真实数据流：导入 CSV → list_materials → observe() 记录 →
        allowed_entities()。手工拼一个字典会把形状错误掩盖掉（这正是
        改造前该分支在生产中不可达的原因）。
        """
        from stage0.product import MaterialIndex, ProductStore
        with _env() as store:
            product = ProductStore(store)
            case = product.import_csv('chain', 'name,dose,unit,schedule,date,subject\n'
                                               '维生素D,400,IU,每日一次,2026-01-05,local-demo\n')
            inv = self._inv()
            inv.material_items = {}
            from stage0.agent import Observation
            from stage0.investigation import InvestigationState
            index = MaterialIndex(product).index()
            inv.observe(Observation(tool='list_materials', purpose='x', arguments={},
                                    result=index, ok=True), store)
            self.assertIn('维生素D', inv.allowed_entities())
```

（若 `Observation` 的构造签名不同，按 `stage0/agent.py:102-122` 的字段顺序构造。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest.test_material_candidate_names_count_as_allowed_entities -v`
Expected: FAIL — `'维生素D' not found`

- [ ] **Step 3: 实现统一形状**

`stage0/investigation.py` 字段区（`:109-110` 之后）新增：

```python
    # 本轮已"看到"的每个材料条目，**唯一**的规范形状。只由 observe() 从
    # list_materials 写入，由 allowed_entities() 与差异渲染读取。
    # 改造前这里有两套形状（写入字符串、读取字典），导致材料药名许可
    # 在生产中根本不可达。
    material_items: dict = field(default_factory=dict)
```

`allowed_entities`（`:188-200`）改为：

```python
    def allowed_entities(self) -> set[str]:
        """Names a sub-question may reference: the authoritative medication
        names, plus the names carried by materials staged for this scope.  A
        planner may not invent a drug."""
        names = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        for detail in self.material_items.values():
            name = (detail or {}).get('name')
            if name:
                names.add(str(name))
        return names
```

`observe` 的 `list_materials` 分支（`:382-390`）改为：

```python
        if observation.tool == 'list_materials':
            # Enumerate what this run may subsequently read.  Listing is not
            # reading: these refs only become citations once read back.
            for material in (observation.result or {}).get('materials', []) or []:
                for item in material.get('items', []) or []:
                    ref = f"{material.get('case_id')}/{item.get('item_id')}"
                    if ref not in self.material_refs:
                        self.material_refs.append(ref)
                    self.material_items[ref] = {
                        'name': ((item.get('fields') or {}).get('name')),
                        'kind': item.get('kind'),
                        'current': list(item.get('current') or []),
                    }
            return
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: PASS

- [ ] **Step 5: 验证 Task 2 的双方具名现在真的成立**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.MaterialVisibilityTest -v`
Expected: PASS；并确认 `read_material_item` 后 `material_conflict` 缺口的 `counterparts` 非空

- [ ] **Step 6: 确认持久化可恢复旧状态**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_investigation -v`
Expected: PASS（`material_items` 有默认值，旧状态 `cls(**raw)` 仍可加载）

- [ ] **Step 7: Commit**

```bash
git add stage0/investigation.py stage0/test_agent_visit_prep.py
git commit -m "Materials: one canonical item shape, and the full import-list-ask chain"
```

---

### Task 7: 关闭残留缺口 + 真实尝试计数（C2 / C3）

**Files:**
- Modify: `stage0/investigation.py:24-26`（常量）、`:103-105`（新增计数器）、`:276-283`（`accept_questions` 成功路径）、`:410-423`（`observe`）
- Modify: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Produces: `InvestigationState.plan_attempts: int`
- 语义：`MAX_PLAN_ATTEMPTS` 变为**连续失败**次数，一次成功归零

- [ ] **Step 1: 写失败测试**

追加到 `stage0/test_agent_visit_prep.py` 的 `SubquestionsTest`：

```python
    def test_a_successful_revision_closes_the_leftover_error_gaps(self):
        """被拒过的声明在后来成功时，其错误缺口必须关闭。

        改造前 plan:* 缺口永不解析，而 forced_stop() 的 checks_completed
        要求"无任何 open 缺口"——一次被拒后成功的规划会让整轮只能以
        budget_insufficient/no_progress 收尾。
        """
        inv = self._inv()
        inv.gap('plan:subquestion_coverage_incomplete', 'plan_missing', '旧错误')
        self.assertEqual(inv.accept_questions([{'statement': '核查', 'entities': ['氨氯地平']}]), [])
        leftover = [g for g in inv.gaps if g['gap_id'].startswith('plan:') and g['status'] == 'open']
        self.assertEqual(leftover, [])

    def test_repeated_identical_rejections_do_count_towards_the_limit(self):
        """重复同样的错误也必须计入——改造前数的是 distinct gap id。"""
        inv = self._inv()
        for _ in range(3):
            inv.observe(_plan_obs([{'statement': 'x', 'entities': ['不在药单里的药']}]), None)
        self.assertEqual(inv.termination_reason, 'no_progress')
```

（`_plan_obs` 是一个小助手，构造一条 `plan_questions` 的 `Observation`；按既有 `_env`/`Observation` 的构造方式写。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: FAIL — 残留 `plan:` 缺口仍为 open；重复被拒未触发上限

- [ ] **Step 3: 实现**

`stage0/investigation.py` 常量区（`:24-26`）改为：

```python
# 连续被拒的子问题声明次数上限。与"成功修订轮数"是**两个不同的界**：
# 反复被拒不消耗修订额度，成功修订重置被拒计数。
MAX_PLAN_ATTEMPTS = 3
MAX_PLAN_REVISIONS = 2      # Task 8 使用
```

字段区（`:103-105` 附近）新增：

```python
    plan_attempts: int = 0
```

`accept_questions` 成功路径（`:276-283`）改为：

```python
        self.claims = []
        for item in normalised:
            self._new_claim(item['statement'], item['entities'], 'model')
        self.subquestion_source = 'model'
        self.plan_attempts = 0
        for g in self.gaps:
            # 一次成功的声明同时取代 GAP_PLAN 与此前所有的 plan:* 错误缺口。
            # 不关掉它们，"已被修正的错误"会永久挡住 checks_completed。
            if g['gap_id'] == GAP_PLAN or g['gap_id'].startswith('plan:'):
                g['status'] = 'resolved'
        return []
```

`observe` 的 `plan_questions` 分支（`:417-423`）改为：

```python
            errors = self.accept_questions(observation.arguments.get('questions'))
            if errors:
                self.plan_attempts += 1
                self.gap('plan:' + ','.join(errors), 'plan_missing',
                         '子问题声明未通过校验（' + ','.join(errors) + '），请修订后重新提交。')
                if self.plan_attempts >= MAX_PLAN_ATTEMPTS:
                    self.termination_reason = 'no_progress'
            return
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stage0/investigation.py stage0/test_agent_visit_prep.py
git commit -m "Planning: a corrected error stops blocking completion, and retries are counted"
```

---

### Task 8: 有界修订与修订历史（C4 / C5）

**Files:**
- Modify: `stage0/investigation.py`（`revision_trigger`、`accept_questions`、`allowed_tools`、`proposal_errors`）
- Modify: `stage0/harness/default_tools.py:175-194`（描述与新闸门一致）
- Modify: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Produces:
  - `InvestigationState.plan_revisions: list` → `[{revision, trigger, before, after, retained}]`
  - `InvestigationState.revision_trigger() -> str | None`
  - 缺口 `plan_revision_capped`（可读、非失败）

- [ ] **Step 1: 写失败测试**

追加到 `stage0/test_agent_visit_prep.py`：

```python
    def test_new_evidence_reopens_planning_so_the_model_can_revise(self):
        """接受之后出现新证据（材料差异）时，plan_questions 必须再次可用。"""
        inv = self._inv()
        self.assertEqual(inv.accept_questions([{'statement': '核查', 'entities': ['氨氯地平']}]), [])
        self.assertNotIn('plan_questions', allowed_tools(inv))
        inv.gap('material:case:1/item:2', 'material_conflict', '材料差异',
                material_ref='case:1/item:2')
        self.assertIn('plan_questions', allowed_tools(inv))
        self.assertEqual(inv.revision_trigger(), 'new_material_evidence')

    def test_a_revision_is_recorded_with_its_history_and_retained_evidence(self):
        inv = self._inv()
        inv.accept_questions([{'statement': '核查', 'entities': ['氨氯地平']}])
        inv.assessments = {'claim:' + 'x': {}}
        inv.gap('material:case:1/item:2', 'material_conflict', '材料差异',
                material_ref='case:1/item:2')
        self.assertEqual(inv.accept_questions([{'statement': '核查材料差异',
                                               'entities': ['氨氯地平']}]), [])
        self.assertEqual(len(inv.plan_revisions), 1)
        revision = inv.plan_revisions[0]
        self.assertEqual(revision['trigger'], 'new_material_evidence')
        self.assertEqual(revision['revision'], 1)
        self.assertIn('retained', revision)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: FAIL — `AttributeError: 'InvestigationState' object has no attribute 'revision_trigger'`

- [ ] **Step 3: 实现**

`stage0/investigation.py` 字段区新增：

```python
    # 追加式修订历史。一条修订记下触发原因、实体集的变更与**保留**的证据。
    plan_revisions: list = field(default_factory=list)
```

新增方法（放在 `accept_questions` 之前）：

```python
    def revision_trigger(self) -> str | None:
        """为什么当前子问题集**可以**被修订——绝不是"重置计划"。

        首次规划由 GAP_PLAN 打开；此后只有**新证据**才重新打开规划，
        并且受 MAX_PLAN_REVISIONS 约束。"""
        if any(g['gap_id'] == GAP_PLAN and g['status'] == 'open' for g in self.gaps):
            return 'first_plan'
        if len(self.plan_revisions) >= MAX_PLAN_REVISIONS:
            return None
        revised_refs = {rev.get('material_ref') for rev in self.plan_revisions}
        if any(g['kind'] == 'material_conflict' and g['status'] == 'open'
               and g.get('material_ref') not in revised_refs for g in self.gaps):
            return 'new_material_evidence'
        claim_ids = {claim['claim_id'] for claim in self.claims}
        if any(g['kind'] in {'evidence_missing', 'evidence_conflict'} and g['status'] == 'open'
               and g.get('claim_id') not in claim_ids for g in self.gaps):
            return 'uncovered_gap'
        return None
```

`accept_questions` 在 `self.claims = []` 之前插入修订记账：

```python
        trigger = self.revision_trigger()
        before = [claim['claim_id'] for claim in self.claims]
        is_revision = bool(self.claims)
        self.claims = []
        for item in normalised:
            self._new_claim(item['statement'], item['entities'], 'model')
        after = [claim['claim_id'] for claim in self.claims]
        if is_revision:
            # 仍有效的证据：claim_id 由实体集决定，实体集未变的 claim 会拿到
            # 同一个 id，其 assessments 因此继续有效。显式记录，不靠"撞上"。
            self.plan_revisions.append({
                'revision': len(self.plan_revisions) + 1,
                'trigger': trigger,
                'before': before,
                'after': after,
                'retained': sorted(set(before) & set(after)),
            })
```

`allowed_tools`（`:54-56`）改为：

```python
    if inv.revision_trigger() is not None:
        allowed.append('plan_questions')
```

`proposal_errors` 的 `plan_questions` 分支（`:716-719`）改为：

```python
    if tool == 'plan_questions':
        # 只在真正可以规划时可用：首次规划，或有新证据触发的修订。
        return [] if inv.revision_trigger() is not None else ['plan_questions_only_when_revisable']
```

`harness/default_tools.py:177-179` 的描述改为与闸门一致：

```python
    description=("声明本轮核查的子问题——拆分问题的唯一入口。每条子问题的 entities 必须取自权威药单"
                 "或已上传材料的候选药名，且全部子问题合起来必须覆盖权威药单的每个药名。"
                 "首次规划之后，只有出现新证据（如新读到的材料差异）时才可再次调用以修订；"
                 "修订不得删除仍未解决的问题。"),
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: PASS

- [ ] **Step 5: 确认既有锁定测试按新语义更新**

`test_the_gap_list_exposes_plan_questions_only_while_planning_is_open`（`:340`）与 `test_plan_questions_cannot_bypass_other_gaps`（`:347`）断言的是旧闸门。按新闸门改写：前者改为"仅在 `revision_trigger()` 非 None 时暴露"；后者把期望错误码改为 `plan_questions_only_when_revisable`。**不得删除这两条测试**——它们是防"用 plan_questions 绕开别的缺口"的护栏。

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add stage0/investigation.py stage0/harness/default_tools.py stage0/test_agent_visit_prep.py
git commit -m "Revision: new evidence reopens planning, with history and retained evidence"
```

---

### Task 9: 防伪造完成（C6）

**Files:**
- Modify: `stage0/investigation.py`（`unsolved_entities`、`accept_questions`）
- Modify: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- Produces: `InvestigationState.unsolved_entities() -> set[str]`；错误码 `revision_drops_open_problem`

- [ ] **Step 1: 写失败测试**

**测试必须用材料独有的药名**（`维生素D`），**不能**用权威药单里的药名。原因：`accept_questions` 的**覆盖度检查先于**本任务的删除检查，而覆盖度要求每个权威药名都被覆盖——想"丢掉"一个权威药名，必然先撞上 `subquestion_coverage_incomplete`，测试就会因为**错误的原因**失败。材料独有的药名只受本任务的检查保护，这正是本条要防的口子。

```python
    def test_a_revision_that_drops_an_unsolved_problem_is_refused(self):
        """不得通过删除未解决问题伪造完成。

        用材料独有的药名：权威药单里的药名已被覆盖度检查保护，
        材料药名只有这条检查保护。
        """
        inv = self._inv()
        inv.material_items = {'case:1/item:2': {'name': '维生素D', 'kind': 'unresolved',
                                                'current': []}}
        self.assertEqual(inv.accept_questions([
            {'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
            {'statement': '核查维生素D', 'entities': ['维生素D']}]), [])
        for claim in inv.claims:
            if '维生素D' in claim['entities']:
                claim['status'] = 'insufficient'
        errors = inv.accept_questions([{'statement': '核查氨氯地平', 'entities': ['氨氯地平']}])
        self.assertEqual(errors, ['revision_drops_open_problem'])

    def test_a_revision_that_keeps_the_problem_is_accepted(self):
        inv = self._inv()
        inv.material_items = {'case:1/item:2': {'name': '维生素D', 'kind': 'unresolved',
                                                'current': []}}
        inv.accept_questions([{'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
                              {'statement': '核查维生素D', 'entities': ['维生素D']}])
        for claim in inv.claims:
            if '维生素D' in claim['entities']:
                claim['status'] = 'insufficient'
        self.assertEqual(inv.accept_questions([
            {'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
            {'statement': '核查维生素D（含材料差异）', 'entities': ['维生素D']}]), [])

    def test_a_refused_revision_leaves_the_unsolved_problem_intact(self):
        """拒绝必须真的什么都没改——否则"拒绝"只是一个返回码。"""
        inv = self._inv()
        inv.material_items = {'case:1/item:2': {'name': '维生素D', 'kind': 'unresolved',
                                                'current': []}}
        inv.accept_questions([{'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
                              {'statement': '核查维生素D', 'entities': ['维生素D']}])
        for claim in inv.claims:
            if '维生素D' in claim['entities']:
                claim['status'] = 'insufficient'
        before_claims = [claim['claim_id'] for claim in inv.claims]
        before_revisions = list(inv.plan_revisions)
        self.assertEqual(inv.accept_questions([{'statement': '核查氨氯地平',
                                                'entities': ['氨氯地平']}]),
                         ['revision_drops_open_problem'])
        self.assertEqual([claim['claim_id'] for claim in inv.claims], before_claims)
        self.assertEqual(inv.plan_revisions, before_revisions)
        self.assertIn('维生素D', inv.unsolved_entities())
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: FAIL — 返回 `[]` 而非 `['revision_drops_open_problem']`。**若失败原因是 `subquestion_coverage_incomplete`，说明测试用错了药名**——见上面的说明。

- [ ] **Step 3: 实现**

新增方法：

```python
    def unsolved_entities(self) -> set[str]:
        """本次调查**尚未解决**的问题所涉及的实体。

        一次修订不得把它们丢掉：那会让 checks_completed 因为**忘记**一个
        问题而变便宜。"""
        conflict_claims = {g['gap_id'].split(':', 1)[1] for g in self.gaps
                           if g['kind'] == 'evidence_conflict' and g['status'] == 'open'
                           and g['gap_id'].startswith('conflict:')}
        unsolved = set()
        for claim in self.claims:
            if claim.get('source') != 'model':
                continue
            if claim['status'] == 'insufficient' or claim['claim_id'] in conflict_claims:
                unsolved.update(claim.get('entities') or [])
        return unsolved
```

在 `accept_questions` 的覆盖度检查（`:272-275`）之后、`self.claims = []` 之前插入：

```python
        if self.claims:
            dropped = self.unsolved_entities() - covered
            if dropped:
                return ['revision_drops_open_problem']
```

（`covered` 已在 `:272` 计算。）

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: PASS

- [ ] **Step 5: 写后果测试——未解决的问题仍然挡着完成**

```python
    def test_a_dropped_problem_keeps_completion_out_of_reach(self):
        """拒绝的**后果**：那个未解决的问题仍然挡着 checks_completed。

        单看"返回了错误码"还不够——必须证明缺口没有被这次被拒的修订
        顺手带走。
        """
        inv = self._inv()
        inv.facts = {'medications': [{'display_name': '氨氯地平'}],
                     'semantic': [], 'open_conflicts': []}
        inv.material_items = {'case:1/item:2': {'name': '维生素D',
                                                'kind': 'unresolved', 'current': []}}
        inv.authority_read = True
        inv.accept_questions([{'statement': '核查氨氯地平', 'entities': ['氨氯地平']},
                              {'statement': '核查维生素D', 'entities': ['维生素D']}])
        for claim in inv.claims:
            if '维生素D' in claim['entities']:
                claim['status'] = 'insufficient'
        inv.checks = {key: 'checked' for key in inv.checks}
        self.assertEqual(inv.accept_questions([{'statement': '核查氨氯地平',
                                                'entities': ['氨氯地平']}]),
                         ['revision_drops_open_problem'])
        self.assertTrue(any(g['status'] == 'open' for g in inv.gaps),
                        '被拒的修订不得带走缺口')
        self.assertNotEqual(inv.forced_stop(), 'checks_completed')
```

- [ ] **Step 6: 运行 + Commit**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.SubquestionsTest -v`
Expected: PASS

```bash
git add stage0/investigation.py stage0/test_agent_visit_prep.py
git commit -m "Revision: a problem may not be deleted to make completion cheaper"
```

---

### Task 10: 三类角色分开（D2 / D3 / D5）

**Files:**
- Modify: `stage0/investigation.py:656-702`（`report_text` 第 2 / 5 节）
- Modify: `stage0/test_agent_visit_prep.py`

**Interfaces:**
- 不变式：**调查问题**只出现在第 5 节；**待验证断言**只出现在第 4 节；**结论**只出现在第 2 节

- [ ] **Step 1: 写失败测试**

```python
    def test_a_contradicted_claim_is_not_rendered_as_a_supported_fact(self):
        inv = _finished_investigation()
        inv.claims = [{'claim_id': 'claim:a', 'statement': '某个被反对的断言',
                       'entities': ['氨氯地平'], 'status': 'contradicted',
                       'supporting_evidence': [], 'opposing_evidence': ['ev-1'],
                       'source_status': 'current', 'condition_status': 'verified',
                       'source': 'model', 'support_status': 'unknown'}]
        text = inv.report_text()
        section_two = text.split('## 2', 1)[1].split('## 3', 1)[0]
        self.assertNotIn('某个被反对的断言', section_two)

    def test_a_model_subquestion_without_a_supporting_span_is_not_a_fact(self):
        inv = _finished_investigation()
        inv.questions = [{'gap_id': 'g', 'field': 'f', 'question': '材料里的维生素D是否需要核对？'}]
        text = inv.report_text()
        section_two = text.split('## 2', 1)[1].split('## 3', 1)[0]
        self.assertNotIn('维生素D是否需要核对', section_two)
        section_five = text.split('## 5', 1)[1]
        self.assertIn('维生素D是否需要核对', section_five)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.ReportEvidenceTest -v`
Expected: FAIL — contradicted 断言出现在第 2 节

- [ ] **Step 3: 实现第 2 节谓词与第 5 节问题来源**

第 2 节谓词已在 Task 3 Step 9 改为 `status == 'supported'` + support_status；本步确认并补第 5 节。第 5 节（`:697-698`）改为同时列出模型的澄清问题与子问题：

```python
        asked = [question['question'] for question in self.questions]
        asked += [claim['statement'] for claim in self.claims
                  if claim.get('source') == 'model'
                  and claim['status'] != 'supported'
                  and claim['statement'] not in asked]
        lines += [f"- {question}" for question in asked] or \
                 ['- 可将本报告的差异与未决项逐条向医生或药师确认。']
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep.ReportEvidenceTest -v`
Expected: PASS

- [ ] **Step 5: 跑完整 visit-prep 套件**

Run: `.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v`
Expected: PASS（Task 1 Step 6 记录的失败用例在此按新口径更新）

- [ ] **Step 6: Commit**

```bash
git add stage0/investigation.py stage0/test_agent_visit_prep.py
git commit -m "Report: questions, pending statements and conclusions are told apart"
```

---

### Task 11: 数据集协议版本 + 离线全量回归

**Files:**
- Modify: `stage0/agent_evals/visitprep_dev.json`（`schema_version`）
- Modify: `stage0/test_agent_visit_prep.py:539-546`（口径断言）

- [ ] **Step 1: 升数据集协议版本**

把 `visitprep_dev.json` 的 `schema_version` 从 `visitprep-task@1` 升为 `visitprep-task@2`，并在 `run_visitprep.main` 的 `report` 里把 `'protocol'` 改为从 `scoring.PROTOCOL` 取：

```python
        'protocol': scoring.PROTOCOL,
        'dataset_schema': 'visitprep-task@2',
```

- [ ] **Step 2: 更新口径断言**

`test_every_task_states_the_rubric_before_the_run`（`:539`）追加断言：`schema_version == 'visitprep-task@2'`，并断言**没有任何** `expected` 键是评分器不读的——防止再次出现 `search_budget` 这类"声明了却没人执行"的键。

**先删数据**：把 `vp-noresult-005a`/`005b` 两个任务里的 `search_budget` 键删掉（全仓无人读它；留着会让人以为有这个约束）。

**再加护栏断言**：

```python
    # 每一个声明在任务里的 expected 键都必须被评分器真正读取。
    # search_budget 曾经被声明但无人执行，这类键会让人误以为存在约束。
    from stage0.agent_evals import scoring
    import inspect as _inspect
    source = _inspect.getsource(scoring)
    declared = {key for task in tasks for key in (task.get('expected') or {})}
    unrecognised = {key for key in declared if key not in source}
    self.assertEqual(unrecognised, set(),
                     '这些 expected 键没有任何评分代码读取它们：%s' % sorted(unrecognised))
```

> 该断言按**源码文本**匹配，因此评分器里必须出现每个键的字面量。新增 `expected` 键时要在 `scoring.py` 里真的读它，而不是只在任务 JSON 里写上。

- [ ] **Step 3: 跑离线六臂**

Run: `.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm scripted --out output/verification-2026-09-12/grid-scripted.json`
（其余五臂同法，替换 `--arm`）
Expected: 全部产出；`scripted` 臂在新口径下的通过数**可能低于** 12/12——这是口径变严的真实结果，**不**回退口径

- [ ] **Step 4: 跑全量离线回归**

Run: `.venv/Scripts/python.exe scripts/verify-agent-closeout.py --out output/verification-2026-09-12/closeout`
Expected: `status == pass`

- [ ] **Step 5: Commit**

```bash
git add stage0/agent_evals/visitprep_dev.json stage0/test_agent_visit_prep.py stage0/agent_evals/run_visitprep.py
git commit -m "Protocol: dataset schema v2, and no rubric key that nothing executes"
```

---

### Task 12: live 受控批次

**Files:**
- Create: `scripts/run-verification-live.py`（批次入口：节奏、额度、超时、取消）
- Create: `docs/agent-capability-upgrade/verification-2026-09-12/LIVE-MANIFEST.json`

**Interfaces:**
- Consumes: `run_visitprep.run_task`、`BatchAllowance`、`assert_live_authorized`
- Produces: 产物含 `planner_endpoint`、`call_budget`、`per_call_ledger`

- [ ] **Step 1: 写批次入口（预设节奏与停机条件）**

```python
"""受控 live 批次：预设批次额度、请求间隔、单次超时与取消。

遇到持续限流**停止扩量**——不靠增加重试次数抬高通过率。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from stage0.agent_evals import run_visitprep as rv
from stage0.agent_evals.batch_budget import BatchAllowance

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True, choices=['tokendance', 'siliconflow'])
    parser.add_argument('--arm', default='model')
    parser.add_argument('--call-cap', type=int, required=True)
    parser.add_argument('--min-interval', type=float, default=1.0,
                        help='两次规划调用之间的最小间隔秒数（节奏）')
    parser.add_argument('--max-rate-limit-ratio', type=float, default=0.5,
                        help='超过该 429 比例即停止扩量：这是停机条件，不是调参项')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    from stage0.extract_ddi import _load_dotenv, assert_live_authorized, resolve_llm_config
    _load_dotenv()
    os.environ['LLM_PROVIDER'] = args.provider
    resolved = resolve_llm_config()
    assert_live_authorized(resolved)

    if Path(args.out).exists():
        raise SystemExit('产物已存在：保留它，不要重复采样')

    tasks = json.loads(rv.DATA.read_text(encoding='utf-8'))
    allowance = BatchAllowance(args.call_cap)
    results, not_sampled, stopped = [], [], None
    for task in tasks:
        if allowance.exhausted():
            not_sampled.append(task['task_id'])
            continue
        if stopped:
            not_sampled.append(task['task_id'])
            continue
        time.sleep(args.min_interval)
        result = rv.run_task(task, args.arm, live=True, allowance=allowance)
        allowance.charge(result['observed'].get('provider_attempts_sent'))
        results.append(result)
        attempts = [a for step in (result['observed'].get('planner_steps') or [])
                    for a in step.get('provider_attempts') or []]
        refused = [a for a in attempts if a.get('outcome') == 'rate_limit']
        if attempts and len(refused) / len(attempts) > args.max_rate_limit_ratio:
            stopped = f'持续限流：本任务 429 比例 {len(refused)}/{len(attempts)} 超过阈值'
    manifest = {
        'provider': resolved['provider'], 'model': resolved['model'],
        'base_url': resolved.get('base_url'), 'call_cap': args.call_cap,
        'min_interval_seconds': args.min_interval,
        'stop_condition': f'429 比例 > {args.max_rate_limit_ratio}',
        'stopped_early': stopped, 'allowance': allowance.to_dict(),
        'not_sampled': not_sampled,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({'manifest': manifest, 'tasks': results},
                                          ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 2: 跑 tokendance 主臂（12 任务）**

Run: `.venv/Scripts/python.exe scripts/run-verification-live.py --provider tokendance --arm model --call-cap 300 --min-interval 1.5 --out output/verification-2026-09-12/live-tokendance.json`
Expected: 产出文件；`manifest.stopped_early` 为 `null` 或写明停机原因

- [ ] **Step 3: 跑 siliconflow 小批对照**

Run: `.venv/Scripts/python.exe scripts/run-verification-live.py --provider siliconflow --arm model --call-cap 80 --min-interval 1.5 --out output/verification-2026-09-12/live-siliconflow-small.json`
Expected: 产出；**明确标注**其限流状态，不与主臂混算

- [ ] **Step 4: 记录 manifest**

把两次运行的 manifest 合并写入 `LIVE-MANIFEST.json`（含端点、额度、节奏、停机条件与实际停机情况）。

- [ ] **Step 5: Commit**

```bash
git add scripts/run-verification-live.py docs/agent-capability-upgrade/verification-2026-09-12/LIVE-MANIFEST.json output/verification-2026-09-12
git commit -m "Live: a paced, capped batch that stops instead of retrying harder"
```

---

### Task 13: 浏览器验收（/materials 与 /tasks）

**Files:**
- Create: `scripts/verification-browser-acceptance.js`

- [ ] **Step 1: 起服务**

Run（两个终端）：
```bash
.venv/Scripts/python.exe -m uvicorn stage0.server:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```
Expected: `GET /v1/health` 返回 200；前端可访问

- [ ] **Step 2: 写验收脚本**

按仓库既有 `scripts/product-full-browser-acceptance.js` 的结构写 `scripts/verification-browser-acceptance.js`，覆盖：

1. `/materials` 上传一份用药 CSV → 断言 case 与条目出现（含差异 `kind`）；
2. `/tasks` 发起一次开放证据核查（goal 用 `visitprep_dev.json` 里那句）→ 断言任务进入 `waiting_review` 或 `checks_completed`；
3. 若出现补问 → 断言 `missing_inputs` 有内容，且**部分结果** `partial_report_refs` 非空；
4. 断言报告里第 3 节**双方具名**、第 4 节列出待核实项；
5. 断言降级展示：`degraded_label` 或 `run status=degraded` 在降级时出现。

- [ ] **Step 3: 运行**

Run: `node scripts/verification-browser-acceptance.js 2>&1 | tee output/verification-2026-09-12/browser-acceptance.log`
Expected: 断言全过；失败项逐条记录**实际**观察值

- [ ] **Step 4: Commit**

```bash
git add scripts/verification-browser-acceptance.js output/verification-2026-09-12/browser-acceptance.log
git commit -m "Browser acceptance: upload, investigate, ask, and show the degradation"
```

---

### Task 14: RESULT.md

**Files:**
- Create: `docs/agent-capability-upgrade/verification-2026-09-12/RESULT.md`

- [ ] **Step 1: 汇总，逐项回答**

`RESULT.md` 必须包含：

1. **修改位置**表（文件:行）
2. **离线回归结果**（命令 + 输出摘要 + 是否全绿）
3. **新旧评分对照**：同一批历史产物在 `visitprep-eval@1` 与 `@2` 下的逐任务对照，含 `undetermined` 与 `not_comparable_to`
4. **原始执行证据**：live 批次的 `manifest`、逐次 provider 账本、停机情况
5. **两类目标行为的结论**：首次检索无结果后改写查询 / 新证据后修订计划——**找到就给 trace 证据，没找到就明确写"未达标"**
6. **浏览器验收**（与模型质量验收**分开**成节）
7. **剩余问题**
8. **明确回答**：哪些缺陷已修复；**现在是否有可信证据**证明模型根据观察调整行动并改善了任务结果

- [ ] **Step 2: 逐条核对没有夸大**

对每一条"已修复"的主张，指出对应的**测试名**或**产物路径**。没有证据的主张一律降级为"未验证"。

- [ ] **Step 3: Commit**

```bash
git add docs/agent-capability-upgrade/verification-2026-09-12/RESULT.md
git commit -m "Result: what was fixed, what was observed, and what is still not shown"
```

---

## Self-Review

**1. Spec coverage**

| 设计章节 | 覆盖任务 |
|---|---|
| §3 评分协议 v2（三轴/五计数/冲突/引用/负向对照/重评） | Task 1、2、3、4 |
| §4 调用上限（额度传递/入账/硬上限/边界测试） | Task 5 |
| §5 C1 材料结构 | Task 6 |
| §5 C2/C3 残留缺口与计数 | Task 7 |
| §5 C4/C5 有界修订与证据保留 | Task 8 |
| §5 C6 防伪造完成 | Task 9 |
| §6 三类角色 + `claim-support@1` | Task 3（scope）、Task 10（角色） |
| §7 受控验证（离线→重评→live→浏览器） | Task 11、12、13 |
| §7 两个目标行为的观测 | Task 12 产物 + Task 14 结论 |
| §8 风险与可比性 | Task 4（`not_comparable_to`）、Task 11 Step 3 |
| §9 口径声明 | Task 14 |

无遗留缺口。

**2. Placeholder scan** — 已检查：无 TBD/TODO；每个代码步骤都给了可执行代码或精确 diff 位置；Task 10 Step 3、Task 11 Step 2 给了具体值而非"待定"。

**3. Type consistency** — 已核对：

- `BatchAllowance.remaining()/exhausted()/charge()/to_dict()` 在 Task 5 定义，Task 12 使用一致；
- `attempts_sent(memory, run_id)` 在 Task 5 定义并在 `run_task` 的 `finally` 与 Task 12 使用一致；
- `score_outcome` 返回键 `report_quality/terminal_state/autonomy/complete/bucket` 在 Task 1 定义，Task 4 的 `rescore` 与 Task 14 使用一致；
- `material_items` 在 Task 6 定义（`ref -> {'name','kind','current'}`），Task 2 Step 5 读 `['current']`、Task 6 Step 3 读 `['name']`，一致；
- `revision_trigger()` 返回 `'first_plan' | 'new_material_evidence' | 'uncovered_gap' | None`，Task 8 的 `allowed_tools`/`proposal_errors`/测试三处使用一致；
- `plan_attempts`（Task 7）与 `plan_revisions`（Task 8）是两个独立量，未混用；
- `claim_support.assess_support` 的状态值 `supported_by_span/no_span/field_mismatch/not_applicable` 在 Task 3 的定义、`_assess` 汇总、`verify_statements` 三处一致。

**一处已修正**：Task 2 的冲突检查要求"双方具名"，但对方 ref 来自 `material_items`（Task 6 才引入）。Task 2 Step 5 因此写成"在 Task 6 之前 `counterparts` 为空列表，测试仍通过"，且 Task 6 Step 5 显式验证该检查此时才真正生效——两个任务之间的依赖是**有意的**，不是遗漏。
