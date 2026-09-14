# 基于变化的回访决策 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **本项目不使用 subagent**（委托方明确要求单 Agent 串行）。按 superpowers:executing-plans 在本会话内逐任务执行。

**Goal:** 让同一件安全事项的第二次回访利用第一次的结果和期间变化，执行有区别、有依据的下一步。

**Architecture:** 回访任务显式绑定 `visit_id` 与本次执行意图；模型拿到一份只含引用的回访摘要（起因、上次结果、上次之后的实际变化、仍有效/已失效的答案、待确认候选、已确认安排、允许动作与预算）；游标只在成功消费时推进；两处既有缺陷（支持判定不看谓语、`available` 不写答案）先修，否则答案复用无从谈起。

**Tech Stack:** Python 3.13 / FastAPI / SQLite（`product_objects` + `workflow_runs`）/ unittest（**无 pytest**）/ React + TypeScript + Vite。

**Spec:** [docs/review-visit/CHANGE-BASED-DESIGN.md](docs/review-visit/CHANGE-BASED-DESIGN.md)

## Global Constraints

- **单 Agent 串行**：不启动子 Agent，不创建额外工作树，不并行运行测试／浏览器／构建。
- 工作目录固定为 `D:\py\HealthAssistant.worktrees\integration`（分支 `feat/review-visit`）。原工作区 `D:\py\HealthAssistant` 全程不动。
- 测试运行器是 **unittest**，不是 pytest。单套件：`python -m unittest stage0.test_<name> -v`。
- 所有测试与脚本使用 `output/visit/` 下的隔离临时库，端口避开 8000/5173。
- **改完任何工具层文件（`stage0/harness/*.py`、`default_tools.py`）先 `python -c "import ast,pathlib,sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))" <file>`** —— 上一轮的中文引号写成 ASCII 引号伪装成了"结论已失效"。
- 文案规则：没有新记录时**只能**用 `review_visits.NO_NEW_RECORDS` 那一句原话，不得写成"情况稳定""风险已解除"。
- 状态规则：候选变更在被确认前，权威记录**一个字节都不动**。
- 记账规则：缺失的用量记 `None`（unknown），**不记 0**。
- 幂等键规则：payload 可能不同，键就必须带判别项。
- 不新增第二套事项／患者档案／调度系统；不新增全站页面。
- 契约版本：`CONTRACTS['safety_case']['version']` → `2`，`SAFETY_CASE_CONTRACT` → `'safety-case@2'`。`investigation.CONTRACT`（`medication-evidence-review@1`）**不动**。

---

### Task 1: O-1 — 支持判定必须看谓语

共享实体名不足以支持一句断言。现在 `assess_support` 只查实体名与数字，于是「服药频次是什么」被一段讲出血风险的文字判成"已被支持"。

**Files:**
- Modify: `stage0/claim_support.py`
- Modify: `stage0/investigation.py`（把 `target_field` 传进去）
- Test: `stage0/test_claim_support.py`

**Interfaces:**
- Produces: `claim_support.ATTRIBUTE_TERMS: dict[str, tuple[str, ...]]`
- Produces: `claim_support.assess_support(*, statement, quote, entities, material_item=None, target_field=None) -> dict` —— 新增关键字参数 `target_field`；返回的 `reasons` 新增 `'attribute_absent_from_span:<field>'` 与 `'content_absent_from_span'`
- Produces: `InvestigationState.claim_target_field(claim_id) -> str | None`

- [ ] **Step 1: 写失败测试**

追加到 `stage0/test_claim_support.py`：

```python
class PredicateTests(unittest.TestCase):
    """只共享实体名，不足以支持一句断言（O-1）。"""

    def test_a_shared_entity_name_does_not_support_an_unrelated_attribute(self):
        verdict = claim_support.assess_support(
            statement='合成药甲目前的服药频次是什么？',
            quote='合成药甲和合成药乙存在出血风险。',
            entities=['合成药甲'], target_field='schedule')
        self.assertEqual('no_span', verdict['status'])
        self.assertIn('attribute_absent_from_span:schedule', verdict['reasons'])

    def test_a_span_that_actually_states_the_attribute_still_supports(self):
        verdict = claim_support.assess_support(
            statement='合成药甲目前的服药频次是什么？',
            quote='合成药甲的服药频次为每日两次。',
            entities=['合成药甲'], target_field='schedule')
        self.assertEqual('supported_by_span', verdict['status'])

    def test_without_a_known_field_it_falls_back_to_content_terms(self):
        unrelated = claim_support.assess_support(
            statement='合成药甲的服药频次是什么？',
            quote='合成药甲和合成药乙存在出血风险。',
            entities=['合成药甲'], target_field=None)
        self.assertEqual('no_span', unrelated['status'])
        self.assertIn('content_absent_from_span', unrelated['reasons'])

    def test_the_content_fallback_still_accepts_a_matching_span(self):
        verdict = claim_support.assess_support(
            statement='合成药甲的服药频次是什么？',
            quote='合成药甲的服药频次为每日两次。',
            entities=['合成药甲'], target_field=None)
        self.assertEqual('supported_by_span', verdict['status'])
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m unittest stage0.test_claim_support.PredicateTests -v`
Expected: FAIL —— `TypeError: assess_support() got an unexpected keyword argument 'target_field'`

- [ ] **Step 3: 实现**

在 `stage0/claim_support.py` 的 `_SAMENESS_CLAIM` 之后加：

```python
# 属性词表：一条问题问的是哪个字段，片段里就必须真的谈到那个属性。
# 词表是**必要**条件不是充分条件——它只回答"这段文字有没有在讲这件事"，
# 不回答"讲得对不对"（那是既有断言判定的事）。
ATTRIBUTE_TERMS = {
    'schedule': ('频次', '频率', '次数', '每日', '每天', '一日', '隔日', '服药时间',
                 '用药时间', 'bid', 'tid', 'qd', 'qod'),
    'dose': ('剂量', '用量', '每次', '片', 'mg', '毫克', '克', '单位'),
    'route': ('途径', '给药', '口服', '静脉', '皮下', '肌注', '外用', '吸入'),
    'start_at': ('开始', '起始', '启用', '起用', '首次'),
}

# 兜底用的停用词与疑问成分：它们在任何一句中文问题里都出现，不携带属性信息。
_STOPWORDS = frozenset('的了呢吗吧啊是什么目前现在请问是否有没有以及和与或这那该其'
                       '我要想知道多少怎样如何怎么为什么哪些哪个')


def _content_bigrams(text: str) -> set[str]:
    """去停用词后剩下的连续两字组合，用来兜底判断"片段有没有在讲同一件事"。"""
    chars = [c for c in (text or '') if c not in _STOPWORDS and not c.isdigit()]
    return {''.join(chars[i:i + 2]) for i in range(len(chars) - 1)}
```

在 `assess_support` 的签名上加 `target_field=None`，并在 `if reasons:` 之前插入：

```python
    # 谓语检查：实体名相同只说明"讲的是同一味药"，不说明"讲的是同一件事"。
    # 没有这一层，一段讲出血风险的文字会把"服药频次是什么"读成已有依据。
    terms = ATTRIBUTE_TERMS.get(str(target_field or ''))
    flat = _flatten(quote)
    if terms:
        if not any(term.lower() in flat for term in terms):
            reasons.append('attribute_absent_from_span:' + str(target_field))
    else:
        stripped = statement or ''
        for entity in (entities or ()):
            stripped = stripped.replace(str(entity), '')
        if not (_content_bigrams(stripped) & _content_bigrams(quote)):
            reasons.append('content_absent_from_span')
```

在 `stage0/investigation.py` 的 `InvestigationState` 上加方法（放在 `question_view` 附近）：

```python
    def claim_target_field(self, claim_id: str) -> str | None:
        """这条 claim 对应的问题问的是哪个字段——支持判定要用它查属性词表。"""
        for question in self.questions:
            if not question.get('question_id'):
                continue
            if 'claim:' + digest([question['question_id']])[:12] == claim_id:
                return question.get('target_field')
        return None
```

在 `_read_evidence` 里 `assess_support(...)` 调用处补上参数：

```python
                support = assess_support(statement=claim['statement'], quote=text,
                                         entities=claim['entities'],
                                         material_item=self.material_items.get(ref),
                                         target_field=self.claim_target_field(claim['claim_id']))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_claim_support -v`
Expected: PASS（含既有用例）

- [ ] **Step 5: 跑相关的既有套件确认没有新红**

Run: `python -m unittest stage0.test_parallel_product_acceptance -v`
Expected: `test_claim_support_is_not_inferred_from_a_shared_entity_name` 变为 PASS。`test_a_question_read_as_available_can_name_its_answer` 可能转为 **skipped**（`require_implemented` 在 `available` 为空时跳过）——这是预期的，Task 2 会给它一个真正成立的正面用例。

- [ ] **Step 6: 提交**

```bash
git add stage0/claim_support.py stage0/investigation.py stage0/test_claim_support.py
git commit -m "Support: a shared drug name is not a shared subject"
```

---

### Task 2: O-2 — `available` 必须能指认是什么回答了它

`_sync_question_from_claim` 现在写 `information_state=available` 却不写答案元素，于是问题读成"已回答"而背后什么都没有。修法是**回读时记下命中支持的片段**，settle 时据此写出答案。

**Files:**
- Modify: `stage0/investigation.py`
- Test: `stage0/test_parallel_product_acceptance.py`（正面用例）；`stage0/test_investigation_questions.py` 若存在则加单元用例

**Interfaces:**
- Consumes: `claim_support.assess_support`（Task 1）
- Produces: `InvestigationState.claim_support_span(claim_id) -> tuple[str | None, str | None]` 返回 `(evidence_ref, span_text)`

- [ ] **Step 1: 写失败测试**

在 `stage0/test_parallel_product_acceptance.py` 的 `AQuestionReadAsAnsweredShowsWhatAnsweredItTests` 加一条**正面**用例——材料**真的**回答了问题，此时 `available` 必须带得出答案与其来源：

```python
    @staticmethod
    def _matching_provider():
        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                return _declare('合成药甲目前的服药频次是什么？',
                                target='general_reference',
                                strategy='general_reference', field='schedule')
            if not inv.get('evidence_searched_count'):
                gap = _gap_for(inv, 'rag_search')
                if gap is None:
                    return {'decision': 'respond'}
                return {'decision': 'tool', 'tool': 'rag_search', 'gap_id': gap,
                        'expected_observation': '检索说明书原文',
                        'arguments': {'query': '合成药甲 服药频次'}}
            unread = inv.get('evidence_unread') or []
            if unread:
                gap = _gap_for(inv, 'read_evidence')
                if gap is None:
                    return {'decision': 'respond'}
                return {'decision': 'tool', 'tool': 'read_evidence', 'gap_id': gap,
                        'expected_observation': '回读原文',
                        'arguments': {'evidence_id': unread[0]}}
            return {'decision': 'respond'}
        return provider

    def test_an_available_question_names_the_evidence_that_answered_it(self):
        """正面对照：材料真的回答了问题时，available 必须带得出答案与来源。

        没有这一条，"O-2 已修"就只由**否定方向**（问题不再被读成 available）
        支撑——那无法区分"修好了"与"再也不判 available 了"。
        """
        task = self.run_investigation(self._matching_provider(), 'matching-1')
        questions = self.questions_of(task)
        available = [q for q in questions
                     if q.get('information_state') == 'available']
        self.require_implemented(bool(available), '本轮没有把任何问题读到 available')
        for question in available:
            answers = question.get('answers') or []
            self.assertTrue(answers, f'available 却没有答案元素：{question.get("statement")!r}')
            self.assertTrue(any(a.get('source_ref') for a in answers),
                            f'答案没有来源：{answers!r}')
```

`EntityOverlapRAG` 的语料里已经有「合成药乙的合成风险与服用频次相关」，但那只谈合成药乙。给这个测试类加一个自有 RAG 工厂，语料里**真的**有合成药甲的频次：

```python
class MatchingFrequencyRAG:
    chunks = [{"chunk_id": "syn-freq", "drug_name": "合成药甲", "section": "用法用量",
               "text": "【合成资料】合成药甲的服药频次为每日两次。",
               "source_url": "https://synthetic.invalid/label/freq",
               "corpus_version": "synthetic-v1"}]

    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "synthetic-isolated",
                "corpus_version": "synthetic-v1",
                "results": [dict(chunk) for chunk in self.chunks]}
```

把新用例放进一个 `rag_factory = MatchingFrequencyRAG` 的测试类里（类属性按测试类生效）。

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m unittest stage0.test_parallel_product_acceptance.AQuestionReadAsAvailableNamesItsEvidenceTests -v`
Expected: FAIL —— `available 却没有答案元素`

（若先做了 Task 1，`AQuestionReadAsAnsweredShowsWhatAnsweredItTests` 会转为 skipped；这是预期的。）

- [ ] **Step 3: 实现**

在 `_read_evidence` 里 `assessment['support_reasons'] = support['reasons']` 之后加：

```python
                if support['status'] == 'supported_by_span':
                    # 记下**命中支持的片段本身**：settle 时要用它写答案元素。
                    # 只记状态不记片段，正是 O-2 的成因——问题读成"已有依据"，
                    # 却说不出是哪段文字让它变成这样。
                    claim.setdefault('support_spans', {})[ref] = text[:600]
```

加辅助方法：

```python
    def claim_support_span(self, claim_id: str) -> tuple[str | None, str | None]:
        """这条 claim 被哪一条证据的哪一段文字支持。没有就返回 (None, None)。"""
        claim = next((c for c in self.claims if c['claim_id'] == claim_id), None)
        if claim is None:
            return None, None
        spans = claim.get('support_spans') or {}
        assessments = self.assessments.get(claim_id, {})
        for ref in (claim.get('supporting_evidence') or []):
            if assessments.get(ref, {}).get('support_status') == 'supported_by_span' and spans.get(ref):
                return ref, spans[ref]
        return None, None
```

把 `_sync_question_from_claim` 的 `if claim['status'] == 'supported' ...` 分支改成：

```python
        if claim['status'] == 'supported' and claim.get('support_status') == 'supported_by_span':
            ref, span = self.claim_support_span(claim['claim_id'])
            if ref is None:
                # 判定说"被支持"，却指不出是哪一段——不 settle。
                # 状态宣称完成了却没有东西支撑它，正是要修的那类缺陷。
                return
            question.setdefault('answers', []).append({
                'value': span, 'field': question.get('target_field'),
                'source': 'evidence', 'provenance': 'external_evidence',
                'source_ref': ref, 'quote': span, 'origin': 'code',
                'answer_ref': ref, 'at': utcnow_iso(),
                'version': dict(self.patient_version or {}),
                'object_ref': None,
                'still_uncertain': [],
                # assessment 复用既有判定，不另立一套口径。
                'assessment': {
                    'status': grounding.STATUS_VERIFIED,
                    'reason': f'{ref} 的片段支持该断言（逐字引用见 quote）',
                    'source_ref': ref, 'locator': ref,
                    'dependency_refs': list(claim.get('dependency_refs') or [])},
            })
            question['answer_ref'] = ref
            self.settle_question(question['question_id'], QUESTION_STATUS_ANSWERED,
                                 information_state=INFO_AVAILABLE,
                                 answered_by='evidence', evidence_refs=[ref])
```

（`grounding` 与 `utcnow_iso` 在该模块已导入；若未导入按文件既有风格补。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_parallel_product_acceptance -v`
Expected: 新用例 PASS。

- [ ] **Step 5: 提交**

```bash
git add stage0/investigation.py stage0/test_parallel_product_acceptance.py
git commit -m "Answered: a question read as answered can name what answered it"
```

---

### Task 3: 失效统一与重开范围

请求被重开时，investigation 里那条答案的 assessment 仍是 `verified`——两处状态各说各话。同时任何范围变化都重开全部已回答请求，无关变化会重新询问所有问题。

**Files:**
- Modify: `stage0/safety_cases.py`（`retire_stale_answers`、`apply_input`、`_sync_questions_to_case` 所在模块）
- Modify: `stage0/care_tasks.py`（`_sync_questions_to_case` 的双向同步）
- Modify: `stage0/investigation.py`（`invalidate_answer`）
- Test: `stage0/test_review_visits.py`

**Interfaces:**
- Produces: `InvestigationState.invalidate_answer(question_id, reason) -> bool`
- Produces: `SafetyCaseStore.dependency_scopes_for(item) -> list[str]`
- Consumes: 无（独立于 Task 1/2）

- [ ] **Step 1: 写失败测试**

追加到 `stage0/test_review_visits.py`：

```python
class StaleAnswerConsistencyTests(unittest.TestCase):
    """请求重开与答案失效必须是同一件事的两面。"""

    def test_a_reopened_request_does_not_leave_its_answer_verified(self):
        ...
        # 1. 建立一条已回答的请求，answered_against = 当时的 revisions
        # 2. 改一次用药记录（走既有写入入口）
        # 3. store.derive_status(case) 触发 retire_stale_answers
        # 4. 断言：请求 status == 'open'，并且 investigation 里对应答案的
        #    assessment['status'] == 'stale'（不是 'verified'）

    def test_an_unrelated_scope_change_does_not_reopen_every_question(self):
        ...
        # 一条只依赖 medications 的答案，在 semantic 变化后仍然 answered；
        # 在 medications 变化后才重开。
```

（用既有 `_Host`／`SafetyCaseStore` 装置建数据；具体构造照抄同文件已有用例的建库与建事项写法。）

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m unittest stage0.test_review_visits.StaleAnswerConsistencyTests -v`
Expected: FAIL —— 答案 assessment 仍是 `verified`；无关变化后请求被重开

- [ ] **Step 3: 实现**

`stage0/safety_cases.py` —— 在 `retire_stale_answers` 里把判定换成按依赖范围：

```python
    def dependency_scopes_for(self, item: dict[str, Any]) -> list[str] | None:
        """这条请求的回答**依赖**哪些记录范围。

        返回 ``None`` 表示解析不出来——调用方按**旧口径**处理（记录一变就重开）。
        方向与模块原有注释一致：重新打开是保守方向，宁可再问一次。
        """
        subjects = [str(ref) for ref in (item.get('subject_refs') or [])]
        if not subjects:
            return None
        scopes = set()
        for ref in subjects:
            if ref.startswith('memory:medication'):
                scopes.add('medications')
            elif ref.startswith('memory:'):
                scopes.add('semantic')
            elif ref.startswith('safety-case:'):
                return None
        return sorted(scopes) if scopes else None
```

`retire_stale_answers` 的循环体改成：

```python
        current = self.p.revisions()
        opened_scopes = []
        for item in case.get('required_inputs') or ():
            if item.get('status') != 'answered':
                continue
            recorded = item.get('answered_against') or {}
            if recorded == current:
                continue
            scopes = self.dependency_scopes_for(item)
            if scopes is not None:
                # 只比这条回答**真正依赖**的范围：无关变化不该把全部问题重问一遍。
                drifted = [s for s in scopes if recorded.get(s) != current.get(s)]
                if not drifted:
                    continue
                opened_scopes = drifted
            item['status'] = 'open'
            item['reopened_reason'] = '记录在回答之后发生变化，这条回答不再适用于当前状态'
            item['reopened_scopes'] = list(scopes or sorted(current))
            item['answer_invalidated'] = True
            item['answer_ref'] = None
            item.pop('needs_alternative_evidence', None)
            reopened.append(item['request_id'])
            case['history'].append({'at': utc_now(), 'event': 'answer_retired',
                                    'request_id': item['request_id'],
                                    'reason': item['reopened_reason'],
                                    'scopes': item['reopened_scopes'],
                                    'answered_against': recorded, 'now': current})
```

`stage0/investigation.py` —— 加方法：

```python
    def invalidate_answer(self, question_id: str, reason: str) -> bool:
        """把一条问题的已有答案标成 stale 并重开问题。

        与 `safety_cases.retire_stale_answers` 是**同一件事的两面**：那边重开请求，
        这边让答案自己承认不再适用。分开做的话，界面会同时看到"这条要重新补充"
        和"这条的答案仍然可靠"。
        """
        question = self.question(question_id)
        if question is None or not question.get('answers'):
            return False
        changed = False
        for answer in question['answers']:
            assessment = answer.get('assessment') or {}
            if assessment.get('status') == grounding.STATUS_VERIFIED:
                assessment['status'] = grounding.STATUS_STALE
                assessment['reason'] = reason
                answer['assessment'] = assessment
                changed = True
        if question.get('status') == QUESTION_STATUS_ANSWERED:
            self.settle_question(question_id, QUESTION_STATUS_OPEN,
                                 information_state=INFO_RECEIVED_UNCONFIRMED)
            changed = True
        return changed
```

`stage0/care_tasks.py` —— `_sync_questions_to_case` 加反向同步（**必须在**它已有的正向投影之前或之后均可，但要幂等）：

```python
                # 反向：请求被重开时，让 investigation 里那条答案一起失效。
                # 单向投影会让两处各说各话——请求说"要重新补充"，答案说"仍可靠"。
                if (request.get('status') == 'open'
                        and request.get('reopened_reason')
                        and question is not None):
                    if investigation.invalidate_answer(
                            question.get('question_id'),
                            str(request.get('reopened_reason'))):
                        changed = True
```

`_sync_questions_to_case` 当前签名是 `(self, store, case_id, inv)`，里面按 `by_id` 找 question。把 `investigation` 对象传进来（调用处已经能拿到，或在该方法内 `InvestigationState.restore(inv, SCOPE)` 还原一份，用还原后的对象改完再写回 `task`／`inv`）。**注意**：`inv` 是 dict，改完必须让调用方把新 dict 存回 `task['investigation']`；把方法签名改为 `_sync_questions_to_case(self, store, case_id, inv)` 返回 `(inv, changed)`，调用处赋值。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visits stage0.test_safety_cases -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/safety_cases.py stage0/investigation.py stage0/care_tasks.py stage0/test_review_visits.py
git commit -m "Invalidation: one mechanism, seen from both sides"
```

---

### Task 4: 答案带值进事项上下文

`investigation_context` 只把已回答的问题投影成 `{request_id, question, answer_kind}`——没有值、没有 assessment、没有来源。模型看不到上次答了什么，也就谈不上复用。

**Files:**
- Modify: `stage0/safety_cases.py`（`investigation_context`）
- Test: `stage0/test_safety_cases.py`

**Interfaces:**
- Produces: `investigation_context(...)['questions']['answered']` 每条新增 `value` / `assessment` / `source` / `answered_against` / `still_valid`

- [ ] **Step 1: 写失败测试**

```python
    def test_an_answered_question_carries_its_answer_and_source(self):
        """只报"这条答过"不够——模型要能读到答的是什么、依据是什么。"""
        context = store.investigation_context(case)
        answered = context['questions']['answered']
        self.assertTrue(answered)
        for item in answered:
            self.assertIn('value', item)
            self.assertIn('assessment', item)
            self.assertIn('source', item)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m unittest stage0.test_safety_cases -v`
Expected: FAIL —— `KeyError: 'value'`

- [ ] **Step 3: 实现**

`investigation_context` 里 `'answered'` 的投影改为：

```python
                'answered': [{'request_id': i['request_id'], 'question': i.get('question'),
                              'answer_kind': i.get('answer_kind'),
                              'value': _answer_value(i),
                              'source': _answer_source(i),
                              'assessment': _answer_assessment(i),
                              'answered_against': i.get('answered_against'),
                              'answered_at': i.get('answered_at'),
                              # 仍有效 = 这条回答记录的版本与当前版本一致（或只依赖未变范围）。
                              'still_valid': (i.get('answered_against') or {}) == self.p.revisions()
                                             or not i.get('answer_invalidated')}
                             for i in inputs if i.get('status') == 'answered'],
```

加三个模块级小工具（放在 `_load_json` 旁边）：

```python
def _answer_value(item: dict[str, Any]) -> Any:
    """这条回答的内容。取 `answered_parts` 里最后一条的值；没有就取收到的原文。"""
    parts = item.get('answered_parts') or []
    if parts:
        return parts[-1].get('value')
    return item.get('received_value')


def _answer_source(item: dict[str, Any]) -> Any:
    parts = item.get('answered_parts') or []
    return parts[-1].get('source') if parts else None


def _answer_assessment(item: dict[str, Any]) -> Any:
    parts = item.get('answered_parts') or []
    return parts[-1].get('assessment') if parts else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_safety_cases stage0.test_safety_mainline_e2e -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/safety_cases.py stage0/test_safety_cases.py
git commit -m "Context: an answered question carries what answered it"
```

---

### Task 5: 回访意图由事实推导

**Files:**
- Modify: `stage0/review_visits.py`
- Test: `stage0/test_review_visits.py`

**Interfaces:**
- Produces: `review_visits.visit_intent(product, case, visit, *, previous_task=None) -> dict`，键为 `goal` / `sequence` / `reason` / `previous_unfinished` / `new_since_last_visit` / `follow_up` / `recheck_reason`
- Produces: `review_visits.visit_goal(product, case, visit, *, previous_task=None) -> str`（300 字以内）

- [ ] **Step 1: 写失败测试**

```python
class VisitIntentTests(unittest.TestCase):
    def test_the_goal_states_the_reason_and_does_not_demand_reproof(self):
        intent = visits.visit_intent(product, case, visit)
        self.assertIn(visit['reason']['detail'], intent['goal'])
        self.assertNotIn('请核实该风险在当前记录下是否成立', intent['goal'])
        self.assertLessEqual(len(intent['goal']), 300)

    def test_a_second_visit_says_which_number_it_is(self):
        intent = visits.visit_intent(product, case, second_visit, previous_task=first_task)
        self.assertEqual(2, intent['sequence'])
        self.assertTrue(intent['previous_unfinished'] is not None)

    def test_recheck_is_only_named_when_a_basis_actually_failed(self):
        self.assertIsNone(visits.visit_intent(product, case, visit)['recheck_reason'])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visits.VisitIntentTests -v`
Expected: FAIL —— `AttributeError: module 'stage0.review_visits' has no attribute 'visit_intent'`

- [ ] **Step 3: 实现**

在 `stage0/review_visits.py` 加：

```python
REASON_LABELS = {REASON_DUE: '已确认的跟进安排到期', REASON_RECORD_CHANGE: '相关记录发生变化',
                 REASON_INPUT_ARRIVED: '收到了新的补充', REASON_USER_STARTED: '您主动发起'}

#: 回访目标里**允许**的下一步。程序限定可选项，模型选具体做哪一个。
VISIT_NEXT_STEPS = ('直接复用已有结论交付结果', '向用户询问一个具体缺失事实',
                    '读取相关材料或证据', '从用户描述中提出待确认变更',
                    '根据新证据重新核对某个判断', '说明当前需要等待什么')


def visit_intent(product, case: dict[str, Any], visit: dict[str, Any], *,
                 previous_task: dict[str, Any] | None = None) -> dict[str, Any]:
    """本次回访的执行意图，由四类**事实**推导。

    触发原因、上次未完成事项、上次之后的实际变化、已确认的跟进安排。
    刻意不要求"重新证明原有风险成立"——那会让每次回访把第一次重做一遍。
    只有原依据真的失效或出现相关新证据时，才给出 ``recheck_reason``。
    """
    previous = _previous_visit(product, visit)
    previous_result = (previous or {}).get('result') or {}
    unfinished = [item.get('text') for item in (previous_result.get('unresolved') or [])]
    entries = list(case.get('history') or [])
    start = int((visit.get('cursor') or {}).get('before') or 0)
    fresh = [_history_line(entry) for entry in entries[start:]]
    follow_up = _follow_up.project_follow_up(case.get('follow_up'))
    recheck = None
    for entry in entries[start:]:
        if entry.get('event') == 'answer_retired':
            recheck = '记录变化使先前的一条依据需要重新核对'
        if entry.get('event') == 'resolution_basis_retired':
            recheck = '原有处置依据已失效，需要按当前记录重新核对'
    intent = {
        'sequence': len([v for v in ReviewVisitStore(product).for_case(case['id'])
                         if v['opened_at'] <= visit['opened_at']]),
        'reason': dict(visit['reason']),
        'previous_visit_id': visit.get('previous_visit_id'),
        'previous_unfinished': unfinished or None,
        'new_since_last_visit': [line['text'] for line in fresh],
        'follow_up': _arrangement_view(follow_up, case),
        'recheck_reason': recheck,
        'allowed_next_steps': list(VISIT_NEXT_STEPS),
    }
    intent['goal'] = visit_goal(product, case, visit, intent=intent)
    return intent


def visit_goal(product, case: dict[str, Any], visit: dict[str, Any], *,
               intent: dict[str, Any] | None = None) -> str:
    """回访目标文本。上限 300 字，与 `_safety_case_goal` 同一口径。"""
    intent = intent or visit_intent(product, case, visit)
    kind = {'interaction_risk': '药物相互作用风险', 'condition_risk': '患者个体风险',
            'evidence_gap': '依据缺口', 'discrepancy': '记录不一致',
            'source_invalidated': '来源失效'}.get(case.get('case_type'), case.get('case_type'))
    parts = [f"这是同一件{kind}安全事项的第 {intent['sequence']} 次回访。",
             f"本次起因：{intent['reason']['detail']}。"]
    if intent['previous_unfinished']:
        parts.append('上次仍未完成：' + '；'.join(intent['previous_unfinished'][:3]) + '。')
    if intent['new_since_last_visit']:
        parts.append('上次之后新增：' + '；'.join(intent['new_since_last_visit'][:3]) + '。')
    else:
        parts.append(NO_NEW_RECORDS)
    follow = intent['follow_up'] or {}
    if follow.get('present'):
        parts.append(f"已确认的跟进安排：{follow.get('at') or follow.get('note') or '已登记'}"
                     f"（确认状态：{'已确认' if follow.get('confirmed') else '未确认'}）。")
    if intent['recheck_reason']:
        parts.append('需要重新核对：' + intent['recheck_reason'] + '。')
    else:
        parts.append('原有结论仍然有效时直接复用，不要为了重新得到同一结论重复检索。')
    parts.append('请选择本次最值得执行的下一步：' + '／'.join(VISIT_NEXT_STEPS) + '。')
    return ''.join(parts)[:300]
```

同时加 `_previous_visit(product, visit)` 小工具（按 `previous_visit_id` 取，取不到返回 `None`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visits -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/review_visits.py stage0/test_review_visits.py
git commit -m "Intent: a visit says why it is here, not what to prove again"
```

---

### Task 6: 回访任务绑定 visit，契约升到 @2

**Files:**
- Modify: `stage0/care_tasks.py`（`CONTRACTS`、`SAFETY_CASE_CONTRACT`、`create`、`resume`）
- Modify: `stage0/safety_cases.py`（`_ensure_visit_task`）
- Test: `stage0/test_review_visits.py`

**Interfaces:**
- Consumes: `review_visits.visit_goal`（Task 5）
- Produces: `care_task['visit_id']`、`care_task['visit_intent']`
- Produces: `CareTasks.create(..., visit_id=None)`

- [ ] **Step 1: 写失败测试**

```python
    def test_a_visit_task_records_which_visit_it_serves(self):
        """恢复时才能判断"这个任务还是不是那次回访的"。"""
        visit = ...
        task = ...
        self.assertEqual(visit['id'], task['visit_id'])
        self.assertEqual(visit['id'], task['visit_intent']['visit_id'])
        self.assertIn(visit['reason']['detail'], task['goal'])

    def test_an_old_contract_task_is_not_revived_for_a_visit(self):
        """契约升级后，旧的 safety-case@1 任务不能被复活成这次回访的执行者。"""
        # 造一件 contract_version=1 的 running 任务，再调 _ensure_visit_task：
        # 必须新开一件 @2 的，且不抛 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visits.VisitTaskBindingTests -v`
Expected: FAIL —— `KeyError: 'visit_id'`

- [ ] **Step 3: 实现**

`CONTRACTS['safety_case']['version']` → `2`；`SAFETY_CASE_CONTRACT = 'safety-case@2'`。

`CareTasks.create` 签名加 `visit_id=None`；在 `if goal_type == 'safety_case':` 块里：

```python
                task['visit_id'] = visit_id
                task['visit_intent'] = ({'visit_id': visit_id, **visit_intent}
                                        if visit_intent else None)
```

（`visit_intent` 同样作为关键字参数传入，默认 `None`。）

`safety_cases._ensure_visit_task`：

```python
    existing = _visit_task(product, case['id'], visit)
    if existing is not None and existing.get('contract_version') != CONTRACTS['safety_case']['version']:
        # 契约升级：旧任务**不复活**。复活会把上一次语义下的中间状态当成本次起点，
        # 而 resume 会因为它版本对不上直接抛 409——用户看到的是报错，不是回访。
        existing = None
    ...
    running = [t for t in product.objects('care_task')
               if t.get('goal_type') == 'safety_case'
               and t.get('safety_case_id') == case['id']
               and t.get('visit_id') == visit['id']          # ← 收紧到同一次回访
               and t['contract_version'] == CONTRACTS['safety_case']['version']
               and t['status'] not in ('completed', 'cancelled', 'failed')]
    ...
    intent = visits_module.visit_intent(product, case, visit)
    created = tasks.create(f"visit:{visit['id']}:{len(visit.get('focus') or [])}",
                           'safety_case', case['id'], due_at=None,
                           goal=intent['goal'], visit_id=visit['id'], visit_intent=intent)
```

`_execute_safety_case` 在 `_record_visit_outcome` 之前校验归属：

```python
        self._record_visit_outcome(task, store, case, inv)
```

改为在 `_record_visit_outcome` 内部先判：

```python
        if task.get('visit_id') and visit['id'] != task['visit_id']:
            # 这个任务服务的不是当前未结束的这次回访：它的结果不属于这里。
            return
        if visit.get('care_task_id') and visit['care_task_id'] != task['id']:
            return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visits stage0.test_review_visit_flow -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/care_tasks.py stage0/safety_cases.py stage0/test_review_visits.py
git commit -m "Binding: a visit task knows which visit it serves"
```

---

### Task 7: 回访摘要进模型上下文

**Files:**
- Modify: `stage0/care_tasks.py`（`_safety_case_context`）
- Test: `stage0/test_review_visit_flow.py`

**Interfaces:**
- Consumes: Task 4（答案带值）、Task 5（`visit_intent`）、Task 6（`task['visit_id']`）
- Produces: `case_context['visit']`，键为 `visit_id` / `sequence` / `reason` / `started_from` / `previous_result` / `new_since_last_visit` / `reusable_answers` / `retired_answers` / `pending_candidates` / `confirmed_follow_up` / `open_questions` / `allowed_actions` / `budget`

- [ ] **Step 1: 写失败测试**

```python
    def test_the_model_context_carries_the_visit_summary(self):
        context = tasks._safety_case_context(task, case, {"max_steps": 16})
        visit = context['visit']
        self.assertEqual(visit_id, visit['visit_id'])
        self.assertTrue(visit['reason']['detail'])
        self.assertIn('reusable_answers', visit)
        self.assertIn('retired_answers', visit)
        self.assertIn('allowed_actions', visit)

    def test_the_summary_does_not_copy_patient_facts(self):
        """只放引用与状态：摘要里不得出现药单副本或证据正文。"""
        blob = json.dumps(context['visit'], ensure_ascii=False)
        for item in product.memory.current_medications():
            self.assertNotIn(item.get('display_name') or '', blob)

    def test_no_new_records_is_stated_as_an_information_state(self):
        """没有新记录 ≠ 情况稳定。"""
        self.assertIn('系统尚未收到新记录', context['visit']['new_since_last_visit']['statement'])
        self.assertNotIn('稳定', context['visit']['new_since_last_visit']['statement'])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visit_flow -v`
Expected: FAIL —— `KeyError: 'visit'`

- [ ] **Step 3: 实现**

在 `_safety_case_context` 的 `context.update({...})` 里加 `'visit'` 块（用一个新方法 `self._visit_context(task, case)` 保持函数短小）：

```python
    def _visit_context(self, task, case) -> dict[str, Any] | None:
        """本次回访的**引用式**摘要。没有回访就是 None，不凭空造一个。

        只放引用与状态：答案给 request_id 与值，候选给 id 与字段，证据给 ref。
        **不复制**患者事实、药单、证据正文或结论——那些一律现取，摘要才不会
        和权威记录分叉（`review_visits` 模块的同一原则）。
        """
        from . import review_visits as visits_module
        visit_id = task.get('visit_id')
        if not visit_id:
            return None
        visits = visits_module.ReviewVisitStore(self.p)
        try:
            visit = visits.get(visit_id)
        except ProductError:
            return None
        inputs = case.get('required_inputs') or []
        entries = list(case.get('history') or [])
        start = int((visit.get('cursor') or {}).get('before') or 0)
        previous = None
        if visit.get('previous_visit_id'):
            try:
                previous = visits.get(visit['previous_visit_id'])
            except ProductError:
                previous = None
        previous_result = (previous or {}).get('result') or {}
        fresh = [_history_line(entry) for entry in entries[start:]]
        return {
            'visit_id': visit['id'],
            'sequence': len([v for v in visits.for_case(case['id'])
                             if v['opened_at'] <= visit['opened_at']]),
            'reason': dict(visit['reason']),
            'started_from': start,
            'previous_result': {
                'unresolved': previous_result.get('unresolved') or [],
                'focus': (previous or {}).get('focus') or [],
                'next_step': previous_result.get('next_step'),
                'closed_at': (previous or {}).get('closed_at'),
            } if previous else None,
            'new_since_last_visit': {
                'changed_scopes': visits_module.changed_scopes(self.p, case),
                'statement': (visits_module.NO_NEW_RECORDS if not fresh else None),
                'events': [line['text'] for line in fresh],
            },
            'reusable_answers': [
                {'request_id': i['request_id'], 'question': i.get('question'),
                 'value': _answer_value(i), 'source': _answer_source(i),
                 'assessment': _answer_assessment(i),
                 'answered_against': i.get('answered_against')}
                for i in inputs if i.get('status') == 'answered'],
            'retired_answers': [
                {'request_id': i['request_id'], 'question': i.get('question'),
                 'reason': i.get('reopened_reason'), 'scopes': i.get('reopened_scopes')}
                for i in inputs if i.get('answer_invalidated')],
            'pending_candidates': [
                {'candidate_id': c['id'], 'name': c['name'], 'field': c['field'],
                 'before': c.get('before'), 'after': c.get('after'), 'source': c['source']}
                for c in (visit.get('change_candidates') or [])
                if c['status'] == visits_module.CANDIDATE_PENDING],
            'confirmed_follow_up': visits_module._arrangement_view(
                _follow_up.project_follow_up(case.get('follow_up')), case),
            'open_questions': [
                {'request_id': i['request_id'], 'question': i.get('question'),
                 'why_needed': i.get('why_needed'),
                 'for_professional': bool(i.get('for_professional'))}
                for i in inputs if i.get('status') in ('open', ANSWER_UNKNOWN)],
            'allowed_actions': ['reuse_existing_conclusion', 'ask_user_one_fact',
                                'read_material_or_evidence', 'propose_change_candidate',
                                'recheck_on_new_evidence', 'state_what_we_wait_for'],
            'budget': {
                'remaining_task_steps': max(0, task['budget']['limit'] - task['budget']['spent']),
                'runs_so_far': len(task.get('runs') or []),
            },
        }
```

把 `'visit': self._visit_context(task, case),` 加进 `context.update({...})`，并与既有 `new_since_last_run` 并存（后者服务非回访的安全事项调查）。

`_answer_value` / `_answer_source` / `_answer_assessment` 从 `safety_cases` 导入复用（Task 4 已产出），不重写一份。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visit_flow stage0.test_safety_mainline_e2e -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/care_tasks.py stage0/test_review_visit_flow.py
git commit -m "Summary: the visit reaches the model as references, not as a copy"
```

---

### Task 8: 游标只在成功消费时推进

**Files:**
- Modify: `stage0/care_tasks.py`（`_execute_safety_case`）
- Test: `stage0/test_review_visits.py`

**Interfaces:**
- Produces: `CONSUMED_TERMINATIONS = ('checks_completed', 'waiting_input', 'waiting_review')`

- [ ] **Step 1: 写失败测试**

```python
class CursorConsumptionTests(unittest.TestCase):
    def test_a_failed_run_does_not_consume_the_events_it_never_processed(self):
        # 任务已有 case_history_cursor = N；造一次以 unrecoverable_failure 收尾的运行，
        # 期间事项历史新增了 M 条。断言 cursor 仍是 N。
    def test_a_successful_run_consumes_up_to_the_snapshot_not_beyond(self):
        # 运行开始时有 N 条，运行期间新增 M 条；成功收尾后 cursor == N（不是 N+M）。
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visits.CursorConsumptionTests -v`
Expected: FAIL —— cursor 被推进到运行结束时的长度

- [ ] **Step 3: 实现**

在 `_execute_safety_case` 开头（`case = store.get(...)` 之后）取快照：

```python
        # 消费游标在**建上下文那一刻**取：这一轮真正交给模型的是此前的事件。
        # 运行期间新增的事件（重开请求写的 answer_retired 等）不在快照里，
        # 因此不会被误标成"已消费"。
        consumed_at = len(case.get('history') or [])
```

把末尾的 `task['case_history_cursor'] = len(store.get(case['id']).get('history') or [])` 换成：

```python
        # 只在**成功消费**时推进。失败、取消、降级、no_progress 都没把事件处理完，
        # 推进游标等于把没看过的变化标成看过了——下一轮回访就再也看不到它们。
        if termination in CONSUMED_TERMINATIONS:
            task['case_history_cursor'] = min(consumed_at,
                                              len(store.get(case['id']).get('history') or []))
```

模块级常量（放在 `CONTRACTS` 附近）：

```python
#: 只有这些收尾方式算"这一轮把交给它的事件处理完了"。
CONSUMED_TERMINATIONS = ('checks_completed', 'waiting_input', 'waiting_review')
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visits stage0.test_review_visit_flow -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/care_tasks.py stage0/test_review_visits.py
git commit -m "Cursor: a failed run has not consumed what it never read"
```

---

### Task 9: 完成条件与动作空间

回访无事可做时，模型必须能**选择**"复用已有结论并交付"，而不是被迫先规划、再搜索、再提问。

**Files:**
- Modify: `stage0/investigation.py`（`visit_ready_to_deliver`、`model_decisions`、`_forced_stop_typed`）
- Modify: `stage0/agent.py`（respond 可用性、计数、循环退出）
- Test: `stage0/test_review_visit_flow.py`

**Interfaces:**
- Produces: `InvestigationState.visit_ready_to_deliver() -> bool`
- Produces: `InvestigationState.model_decisions: int`

- [ ] **Step 1: 写失败测试**

```python
    def test_a_visit_with_nothing_to_do_completes_without_new_questions(self):
        """场景 A：已有答案仍有效，回访复用信息，不重复询问。"""
        # 第二次回访，期间无任何变化；断言：
        #   termination_reason == 'checks_completed'
        #   没有新的必答问题（missing_inputs 为空或与上次相同）
        #   模型**确实被调用过**（run 的 trace 里有至少一条 plan）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visit_flow -v`
Expected: FAIL —— 循环以 `no_progress` 或 `max_cycles` 收尾

- [ ] **Step 3: 实现**

`investigation.py` —— `InvestigationState` 加字段与方法：

```python
    #: 模型真正做过多少次决策。用于区分"模型选择了交付"与"代码在模型开口前
    #: 就收尾了"——后者交付里没有模型的选择，不能记成"Agent 决定了下一步"。
    model_decisions: int = 0
```

```python
    def visit_ready_to_deliver(self) -> bool:
        """这次回访是不是已经无事可做——可以复用已有结论直接交付。

        四个条件缺一不可：有问题集、有仍有效的答案、本次没有新事件、
        没有未决问题、没有需要重核的依据。任何一项不成立都要继续做。
        """
        visit = (self.case_context or {}).get('visit') or {}
        if not visit:
            return False
        if not visit.get('reusable_answers'):
            return False
        if (visit.get('new_since_last_visit') or {}).get('events'):
            return False
        if visit.get('open_questions'):
            return False
        return not any(item.get('reason') for item in (visit.get('retired_answers') or []))
```

`_forced_stop_typed` 开头加：

```python
        if self.visit_ready_to_deliver() and self.model_decisions >= 1:
            # 回访无事可做：模型已经看过摘要并选择了交付，可以收尾。
            # `model_decisions >= 1` 不能省——规划前那次检查若直接命中，
            # 整个 run 一次模型调用都不会发生，交付沦为代码渲染。
            self.termination_reason = 'checks_completed'
            return self.termination_reason
```

`agent.py` —— `_decide` 里调用规划器前自增：

```python
        inv.model_decisions += 1
        return self.planner.decide(state)
```

`respond` 可用性的两处判定（`:1898` 与 `:1978`）由 `bool(investigation.termination_reason)` 扩展为：

```python
        deliverable = bool(state.investigation.termination_reason) or \
            state.investigation.visit_ready_to_deliver()
```

循环退出处（`:3630`）：

```python
                if inv.termination_reason is None:
                    if inv.visit_ready_to_deliver() and inv.model_decisions >= 1:
                        # 模型明确选择了交付而代码没设终止原因：这是一次正常的
                        # "无事可做"，不是 max_cycles。记成后者会把成功当失败。
                        inv.termination_reason = 'checks_completed'
                    else:
                        inv.finish(state.degraded_reason or 'max_cycles')
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visit_flow stage0.test_review_visits -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/investigation.py stage0/agent.py stage0/test_review_visit_flow.py
git commit -m "Deliver: reuse is an action the model can choose"
```

---

### Task 10: C 分支——带可消费信息才唤醒

用户说"还没做"时，现在**一律**不唤醒（那是为修熔断引入的）。带原因、含新事实的推迟应当能继续。

**Files:**
- Modify: `stage0/safety_cases.py`（`answer` 端点、`apply_input`）
- Test: `stage0/test_review_visits.py`

**Interfaces:**
- Produces: `safety_cases.answer_carries_consumable_information(value) -> bool`

- [ ] **Step 1: 写失败测试**

```python
class DeferredAnswerTests(unittest.TestCase):
    def test_a_deferral_with_a_new_fact_wakes_the_investigation(self):
        # 「还没做，下周一开始」→ 可处理信息 → 唤醒
    def test_a_bare_deferral_does_not_wake_anything(self):
        # 「还没做」→ 不唤醒，且请求**不重复追问**（同一个 request_id 不重新问）
    def test_a_deferral_that_cannot_be_judged_stays_asleep(self):
        # 判不出来时保守取不唤醒
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visits.DeferredAnswerTests -v`
Expected: FAIL —— 带原因的推迟也不唤醒

- [ ] **Step 3: 实现**

`safety_cases.py` 加：

```python
#: 推迟类回答里出现这些成分，说明用户**顺带给出了可核对的新信息**——
#: 那就不再是"纯推迟"，值得跑一轮让 Agent 调整下一步。
_NEW_FACT_PATTERNS = (
    re.compile(r'\d{4}\s*[-/年]\s*\d{1,2}'),          # 2026-09 / 2026年9
    re.compile(r'\d{1,2}\s*月\s*\d{1,2}\s*[日号]'),       # 9月14日
    re.compile(r'\d+(?:\.\d+)?\s*(?:mg|毫克|片|次|天|周|个月)'),
    re.compile(r'(?:下周|下个月|下月|明天|后天|周末|月初|月底)'),
)


def answer_carries_consumable_information(value: Any) -> bool:
    """"还没做"有没有顺带给出**能据以行动的新成分**。

    推迟类回答一律不唤醒曾经是对的（唤醒后没有合法动作，必然熔断）。
    但"还没做，下周一开始"里有一个新时间，Agent 可以据此调整下一步；
    把它和"还没做"同样对待，等于把用户提供的信息丢掉。
    判不出来时返回 False——保守方向与推迟类原有的处理一致。
    """
    text = str(value or '')
    return any(pattern.search(text) for pattern in _NEW_FACT_PATTERNS)
```

`answer` 端点：

```python
            kind = body.get('answer_kind')
            if (kind not in ANSWER_DEFERRED_KINDS
                    or answer_carries_consumable_information(body.get('value'))):
                _wake_investigation(product, case_id, body.get('key'))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visits stage0.test_safety_cases -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/safety_cases.py stage0/test_review_visits.py
git commit -m "Deferred: a reason with a new fact is information, not a brush-off"
```

---

### Task 11: 用量读数

**Files:**
- Modify: `stage0/care_tasks.py`（`_execute_safety_case`）
- Modify: `scripts/review-visit-live-acceptance.py`
- Test: `stage0/test_review_visits.py`

**Interfaces:**
- Produces: `_execute_safety_case` 结束时 `resource_budget` 的 `tokens_actual` / `calls_actual` / `usage_unknown` 已重算
- Produces: 验收报告 `usage` 键来自 `CareTasks.usage(task)`，缺失为 `None`

- [ ] **Step 1: 写失败测试**

```python
    def test_the_safety_case_task_reports_its_actual_usage(self):
        """调用真的发生过，记账就不能读成 0。"""
        # 跑一次 safety_case（脚本化），断言：
        #   task['resource_budget']['calls_actual'] 不为 0（或 usage['calls'] 有值）
        #   task['usage']['measured'] 为真
    def test_usage_that_cannot_be_read_is_none_not_zero(self):
        # 没有 child_run_ids 时 usage['calls'] is None，不是 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visits.UsageReadingTests -v`
Expected: FAIL —— `calls_actual == 0`

- [ ] **Step 3: 实现**

`_execute_safety_case` 里 `resources['child_run_ids'].append(...)` 之后补上与 `_execute_evidence_review` 相同的重算：

```python
        if resources:
            if result['run_id'] not in resources['child_run_ids']:
                resources['child_run_ids'].append(result['run_id'])
            # 与 evidence_review 同一口径：从**任务自己保存的 run 引用**汇总，
            # 不按 run_id 字符串去拼。少了这一步，safety_case 任务的
            # calls_actual 永远是建任务时的初值 0——而调用确实发生过。
            owned = [self.p.memory.workflow_run_get(r) for r in resources['child_run_ids']]
            resources['tokens_actual'] = sum((r or {}).get('budget', {}).get('tokens_actual', 0) for r in owned)
            resources['calls_actual'] = sum((r or {}).get('budget', {}).get('calls_attempted', 0) for r in owned)
            resources['usage_unknown'] = any((r or {}).get('budget', {}).get('usage_unknown', False) for r in owned)
```

`scripts/review-visit-live-acceptance.py` 的 `_usage` 改为读 `CareTasks.usage(task)`，并汇总**恢复运行**（`child_run_ids` 全部，本来就是）：

```python
def _usage(product, task):
    """从任务自己保存的 run 引用汇总用量。

    读不到就是 None（unknown），**不记 0**——0 是一个测量结果，
    "没测到"不是同一个意思。上一次把不可引用数字写进报告的正是这里。
    """
    from stage0.care_tasks import CareTasks
    measured = CareTasks(product).usage(task)
    return {'calls': measured.get('calls'), 'tokens': measured.get('tokens'),
            'refused_calls': measured.get('refused_calls'),
            'runs': measured.get('runs'), 'measured': measured.get('measured'),
            'reason': measured.get('reason'),
            'token_limit': (task.get('resource_budget') or {}).get('token_limit'),
            'call_limit': (task.get('resource_budget') or {}).get('call_limit')}
```

两处调用点（`report['round_1']['usage']` / `round_2`）改为 `_usage(product, task)` / `_usage(product, task_now)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visits -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/care_tasks.py scripts/review-visit-live-acceptance.py stage0/test_review_visits.py
git commit -m "Usage: a call that happened is not reported as zero"
```

---

### Task 12: 前端——回访结果说清实际变化

**Files:**
- Modify: `frontend/src/features/safety/ReviewVisitPage.tsx`
- Modify: `frontend/src/api/types.ts`
- Test: `scripts/review-visit-browser-acceptance.js`

**Interfaces:**
- Consumes: `render_result` 的 `statements` / `actions` / `unresolved` / `next_step`（已存在）
- Produces: 结果区新增「复用」「需要重核」「为什么结束」三块，并渲染每条 `basis.kind`

- [ ] **Step 1: 写失败验收**

在 `scripts/review-visit-browser-acceptance.js` 加断言：结果区出现
`[data-visit-section="reused"]`、`[data-visit-section="recheck"]`、`[data-visit-section="why-ended"]`
三个锚点，且每条陈述带 `data-basis` 属性。

- [ ] **Step 2: 跑验收确认失败**

Run: `node scripts/review-visit-browser-acceptance.js`
Expected: FAIL —— 找不到 `[data-visit-section="reused"]`

- [ ] **Step 3: 实现**

`ReviewVisitPage.tsx` 结果区在既有「上次之后」「已完成」「仍未解决」之后加：

```tsx
<section data-visit-section="reused">
  <h3>复用的已有信息</h3>
  <StatementList title="" lines={result.reused ?? []} empty="本次没有可复用的已有结论" />
</section>

<section data-visit-section="recheck">
  <h3>需要重新核对</h3>
  <StatementList title="" lines={result.recheck ?? []} empty="没有依据需要重新核对" />
</section>

<section data-visit-section="why-ended">
  <h3>本次为什么结束或等待</h3>
  <p className="text-sm text-ink-secondary">{result.end_reason ?? result.next_step ?? '—'}</p>
</section>
```

`StatementList` 已经在同文件里，它每条渲染 `data-basis={line.basis?.kind}`——若还没有就补上，让
`program_check` / `user_report` / `model_explanation` / `record` 在界面上分得开。

`types.ts` 的 `VisitResult` 加 `reused?: Statement[]`、`recheck?: Statement[]`、`end_reason?: string`。

服务端 `review_visits.render_result` 同步产出这三个键（由 `visit_intent` 与 `_history_line` 的既有结果派生，不新增真相源）：
- `reused` ← 本次回访里仍有效、且被 `visit_intent` 列为可复用的答案（`basis.kind='record'`）；
- `recheck` ← `retired_answers`（`basis.kind='program_check'`）；
- `end_reason` ← 由 `status_from_task` 与 `termination_reason` 渲染的一句（如「本次没有新的变化，已有结论仍然有效，因此结束」／「仍在等待您补充：…」）。

- [ ] **Step 4: 跑验收确认通过**

Run: `node scripts/review-visit-browser-acceptance.js`
Expected: PASS（含既有 15 条）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/features/safety/ReviewVisitPage.tsx frontend/src/api/types.ts stage0/review_visits.py scripts/review-visit-browser-acceptance.js
git commit -m "Page: the visit result says what actually changed"
```

---

### Task 13: 第二次回访的完成标准

本轮唯一的完成判据。必须有一条测试直接钉住它。

**Files:**
- Create: `stage0/test_review_visit_change.py`
- Test: 同上

**Interfaces:**
- Consumes: Task 1–12 全部

- [ ] **Step 1: 写失败测试**

```python
class SecondVisitDiffersTests(unittest.TestCase):
    """同一件安全事项第二次回访时，系统利用第一次的结果和期间变化，
    执行有区别、有依据的下一步。"""

    def test_the_second_visit_reuses_the_first_and_focuses_on_what_changed(self):
        # 1. 第一次回访：模型建立一条有依据的结论，回答一个问题，收尾
        # 2. 期间发生一次**相关**变化（改一条用药记录）
        # 3. 第二次回访，断言：
        #    (a) 模型上下文里 reusable_answers 含第一次的那条答案
        #    (b) new_since_last_visit 指向那次变化，不是"最后几条"
        #    (c) 本次没有把第一次的问题重新问一遍
        #        （missing_inputs 不含第一次已回答的 request_id）
        #    (d) 本次的结果里能读出"复用了什么 / 因什么而重新核对"

    def test_a_second_visit_with_no_change_does_not_re_ask_anything(self):
        """场景 A 的对照：不变时不该凭空产生新问题。"""

    def test_unrelated_change_does_not_reopen_the_first_visit_question(self):
        """无关变化不重新询问全部问题（Task 3 的产品级对照）。"""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest stage0.test_review_visit_change -v`
Expected: FAIL（至少一条；记录哪一条先失败，那就是缺口所在）

- [ ] **Step 3: 补上缺的实现**

按失败断言指向的位置回到对应任务的文件补齐。**不得**直接给最终状态赋值制造通过——必须走真实端点与 worker。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest stage0.test_review_visit_change -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add stage0/test_review_visit_change.py stage0/*.py
git commit -m "Second visit: it reuses the first and moves on what changed"
```

---

### Task 14: 离线全量 + 浏览器回归

**Files:**
- Modify: 按失败结果定位

- [ ] **Step 1: 跑全量离线检查**

Run: `python scripts/verify-agent-closeout.py --out output/visit/closeout`
Expected: 60 套件，**无红**（O-1/O-2 已在本轮修掉，上一轮那两条基线红应当转绿）。若有红，逐条定位并修——**不豁免**。

- [ ] **Step 2: 跑共享浏览器回归网**

Run: `node scripts/safety-mainline-browser-acceptance.js`
Expected: 12/12 PASS（这条网没有本轮改动，红了说明破坏主线）

- [ ] **Step 3: 跑回访页浏览器验收**

Run: `node scripts/review-visit-browser-acceptance.js`
Expected: 全部 PASS

- [ ] **Step 4: 提交任何修复**

```bash
git add -A
git commit -m "Closeout: the offline gate is green again"
```

---

### Task 15: 一次有限真实验收

**Files:**
- Modify: `scripts/review-visit-live-acceptance.py`（仅当需要补轨迹埋点）

- [ ] **Step 1: 开跑前打印并核对上限**

Run: `python scripts/review-visit-live-acceptance.py --max-calls 20 --wall-seconds 420 --max-tokens 250000 --max-cycles 6`
Expected: 开跑前打印 `max_calls=20 / wall_seconds=420 / max_tokens=250000 / max_cycles=6`。
沿用已有配置，**不换模型、不追加批次、不扩大预算**。

- [ ] **Step 2: 读结果，回答四项观察**

- 是否使用了上次结果；
- 是否聚焦实际变化；
- 是否根据回答调整行动；
- 是否形成具体的回访交付。

- [ ] **Step 3: 记录用量**

用量从 `CareTasks.usage(task)` 汇总（Task 11），包含同一次回访的恢复运行。
读不到就记 **unknown**，不记 0。

- [ ] **Step 4: 写报告**

更新 `docs/review-visit/REPORT.md`：先写用户行为改善，再写实现与验证；
真实模型若仍未走通，**准确指出停在哪个业务动作**——不得把"模型选了一个风险主题"
记成个性化回访成功。**不为补日志重新发起真实调用。**

- [ ] **Step 5: 提交**

```bash
git add docs/review-visit/REPORT.md scripts/review-visit-live-acceptance.py output 2>/dev/null
git commit -m "Live: what the second visit actually did"
```

---

## Self-Review

**Spec coverage:** §2.1→Task 6；§2.2→Task 5；§2.3→Task 7（依赖 Task 4）；§2.4→Task 8；§2.5→Task 9；§2.6→Task 10（A/B/D/E 已在既有实现中，Task 13 用对照钉住）；§2.7 O-1→Task 1，O-2→Task 2，失效统一与重开范围→Task 3；§2.8→Task 11；§2.9→Task 12；§三验证 1→Task 13，2→Task 13，3→Task 14，4→Task 12+14，5→Task 15。无遗漏。

**类型一致性：** `visit_intent` 在 Task 5 定义、Task 6/7 消费；`_answer_value/_answer_source/_answer_assessment` 在 Task 4 定义于 `safety_cases`、Task 7 导入复用（不重定义）；`CONSUMED_TERMINATIONS`（Task 8）与 `VISIT_NEXT_STEPS`（Task 5）各只定义一次；`claim_support_span`（Task 2）与 `claim_target_field`（Task 1）名字在实现与调用处一致。

**已知风险：** Task 2 修改 `_sync_question_from_claim` 后，`test_parallel_product_acceptance` 的既有用例可能由 fail 转 skip——那是 `require_implemented` 的预期行为，正面用例是 Task 2 Step 1 新增的那条，**不能**只靠否定方向宣告 O-2 已修。
