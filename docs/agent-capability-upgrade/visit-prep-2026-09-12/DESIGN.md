# 设计：自主调查闭环 —— 从"代码替模型规划"到"模型决定查什么" · 2026-09-12

**本轮场景**：用户提供多份健康/用药材料并提出一个就诊相关问题；系统整理事实、发现材料间差异、调查证据、提出必要补问，最终生成**有来源**的就诊准备报告。不扩展诊断、处方、用药调整权限。

---

## 一、审计结论：决策权现状

阅读 `agent.py` 的 `_decide` / `LLMPlanner` / `HybridPlanner` / `PlannerPolicyGuard`、`investigation.py`、`harness/*`、`product.py`、`care_tasks.py` 后的实测结果。

| # | 决策 | 现由谁 | 类别 | 处置 |
|---|---|---|---|---|
| 1 | 选工具、写 query、选证据、改写检索 | LLM `LLMPlanner.propose` | C | 保留 |
| 2 | 参数 schema、权限、写幂等、预算、取消 | `PlannerPolicyGuard` / `turn_budget` / `progress` | A | 保留 |
| 3 | 权威事实、版本、证据哈希与作用域 | `memory` / `EvidenceStore` | B | 保留 |
| 4 | **子问题拆解** | **代码** `itertools.combinations(药名,2)`（`investigation.py:209-219`） | C | 改：交给模型 |
| 5 | **缺口产生** | **代码** 对 goal 做正则（`investigation.py:187-190`） | C | 改：由 #4 派生 |
| 6 | **搜索顺序** | **代码** 固定序列（`investigation.py:326-352`） | C | 改：降为降级策略 |
| 7 | **搜索措辞** | **代码** 写死的三选一后缀（`investigation.py:351`） | C | 改：降为降级策略 |
| 8 | 终止 | 代码 `termination_reason` | A | 保留（模型不宣布完成） |

### 三个必须在代码层修掉的问题

**P1 — 纠错上下文把答案递给模型。** `HybridPlanner.correction_for()` 把代码算好的**完整动作**（含固定搜索词、`evidence_id`、`top_k`）作为 `next_expected_action_hint` 返回（`agent.py:2190`）。模型照抄即通过校验，而 `_trace` 记 `source: "llm"` → **被计成独立自主规划成功**。

**P2 — 代码强制动作没有独立类别。** `_decide` 在存在未持久化警告时直接构造并执行 `record_warnings`（`agent.py:3061-3065`）；它绕过了 `HybridPlanner.decide`，trace 落成 `deterministic`，与"模型选择"和"策略降级"混在一起。

**P3 — 模型看不到材料。** `MedicationCoordinatorAgent` 全文无 material 引用。材料差异由 `product.py:recompute()` 确定性算出、caregiver 在 UI 逐项 `decide()`，模型只在事后收到 `medication_recheck` 事件。**"整理多份材料 → 发现差异"这条链路不存在。**

---

## 二、设计决定

| # | 决定 | 依据 |
|---|---|---|
| 1 | `next_action()` 按职责四分：强制停止 / 模型策略 / 降级策略 / 状态同步 | 任务书 §三；固定搜索词本就该属于降级策略 |
| 2 | 新增只读工具 `plan_questions`，模型声明本轮子问题 | 让"拆成哪些必要子问题"真正归模型（审计 #4） |
| 3 | 新增只读工具 `list_materials` + `read_material_item` | 场景要求多份材料与材料间差异（P3） |
| 4 | `correction_for()` 只返回**约束**，不再返回动作与参数 | 堵 P1；纠错后通过的提案单独标记 |
| 5 | 新增 `source: "system_forced"` 归因类别 | 堵 P2；任务书 §六"分别记录" |
| 6 | 报告改为"五问 + 证据支持检查" | 任务书 §五 |
| 7 | 持久化字段**只增不改**，不 bump `VERSION`/`CONTRACT` | 见下 |

### 决定 7 的理由：不制造迁移悬崖

`InvestigationState.restore()` 对 `VERSION`/`CONTRACT` 不匹配一律抛 `migration required`（`investigation.py:81-89`）。若本轮 bump 契约版本，所有在途 `evidence_review` 待办立即无法恢复。本轮新增的是**可加性字段**（`claims[].source`、`subquestion_source`、`material_refs`），`cls(**raw)` 对缺字段取默认值即可继续加载。因此：**持久化形状不变，只 bump 描述交互协议的 `PROTOCOL_VERSION`**，并新增测试锁定"v1 状态可被 v2 代码恢复"。

---

## 三、协议 v4 规格

### 3.1 职责四分（`investigation.py`）

```
forced_stop()            纯状态检查 + 强制停止条件        A 类
model_policy()           由 LLMPlanner 执行的模型决策       C 类
degraded_next_action()   确定性降级策略（含固定搜索词）     降级
observe()/sync_authority() 事实读取与状态同步              B 类
```

`forced_stop()` 只保留真正不可协商的条件：已设 `termination_reason`｜工具不可恢复失败｜支持与反对证据并存（不得投票或让用户选边）｜覆盖完成且无 open gap｜检索预算耗尽｜连续重复检索无新增。

**`subquestions` 缺口**：`sync_authority` 在权威快照读完后**不再自动生成药对 claims**，而是留下一个 `plan_missing` 类缺口 `subquestions`；它只能由 `plan_questions` 关闭。这样"拆成哪些子问题"从流程上就归模型，且缺口队列仍然可见、可审计。`allowed_tools` 在该缺口打开时暴露 `plan_questions`（与 `memory_read` 并列）。模型始终不调用时，由 `degraded_next_action()` 用代码默认药对补上并标 `subquestion_source: code_default`。

`next_action()` 保留为薄兼容层（返回 `degraded_next_action()`），仅当 `mode == 'degraded'` 时被调用。现有测试对它名的引用继续有效。

**正常路径不再写入 `candidates`。** 该字段只服务降级与纠正提示，而纠正提示本轮已改为只给约束。

### 3.2 子问题归模型（新工具 `plan_questions`）

模型在 authority 快照之后调用一次（证据变化时可再次调用以修订——"计划可以随证据更新"，不要求每轮重写）：

```json
{"decision":"tool","tool":"plan_questions","purpose":"...","gap_id":"authority",
 "expected_observation":"...",
 "arguments":{"questions":[{"statement":"...","entities":["氨氯地平","克拉霉素"]}]}}
```

**代码校验（拒绝，不代填）**：

- 每条 `entities` 必须全部来自「权威药单 ∪ 材料候选名」（`ALLOWED_ENTITIES`）——不得凭空造药名；
- 条数 1..`MAX_CLAIMS`；
- `statement` 非空、长度有界；
- 必须**覆盖权威药单的每个药名**（防止靠漏掉子问题让"完成"变便宜）；
- 空列表或非法实体 → `proposal_errors` 拒绝并给出可执行反馈。

采纳后 `claims`/`gaps` 由模型的子问题派生，每条标 `source: "model"`，`subquestions` 缺口关闭。`proposal_errors` 新增：`plan_questions` 只在 `subquestions` 缺口打开时允许，且 `gap_id` 必须指向它——**该工具不能被用来旁路其他缺口**。

模型不调用它时不会卡死：`degraded_next_action()` 用代码默认（药对穷举）补上并标 `subquestion_source: code_default`——**降级可见，不隐藏**。

### 3.3 堵住纠错泄露（`agent.py`）

`correction_for()` 返回值缩减为：

```python
{'previous_proposal_was_rejected': ..., 'rejection_reasons': [...],
 'allowed_tools_now': [...], 'open_gap_ids': [...], 'termination_ready': bool,
 'instruction': '...只描述约束与如何满足校验，不含任何具体动作或参数...'}
```

删除 `next_expected_action_hint`。被拒绝过的提案在被纠正后通过时，`_trace` 记 `post_correction: True`；评测把它与"未被纠正的独立规划"分开统计。

### 3.4 归因类别（`agent.py`）

`_trace().source` 取值扩为 `{llm, llm_post_correction, system_forced, fallback, deterministic}`：

- `llm` —— 模型提案，未经过纠正，未发生参数代填；
- `llm_post_correction` —— 前一提案被拒后修正通过；
- `system_forced` —— 代码为满足安全不变量而自行构造并执行的动作（P2）；
- `fallback` —— 策略降级（provider/parse 失败或安全拒绝后切确定性规划）；
- `deterministic` —— 未启用模型规划器。

`argument_corrections` 非空时，同一条 trace 额外标 `hydrated_arguments: true`——**代码替模型补了决定性参数就必须单独可见**（任务书 §六）。

---

## 四、材料工具规格

数据全部来自现有 `product.py` 的 `case` 模型，**不新建存储**。

### `list_materials`（只读）

```json
{"materials":[{"case_id":"case:...","document_id":"document:<sha256>","parser_version":"csv-v1",
  "created_at":"...","item_count":9,"pending_count":2,
  "items":[{"item_id":"...","fields":{"name":"...","dose":"...","unit":"...","schedule":"...","date":"..."},
            "locations":{"name":{"line":4,"column":2,"coordinate_system":"csv_field"}},
            "kind":"changed|same|new|possible_duplicate|not_listed|unresolved",
            "issues":["..."],"current":["<权威记录 ref>"]}]}]}
```

`kind` 与 `issues` 是 `recompute()`/`validate()` 的**既有确定性结果**，此处只做搬运。模型据此决定调查哪个分歧。

### `read_material_item`（只读）

```json
{"case_id":"...","item_id":"...","fields":{...},"original_fields":{...},
 "locations":{...},"corrections":[...],"kind":"...","issues":[...],
 "document_id":"document:<sha256>","parser_version":"csv-v1"}
```

`original_fields` 与 `corrections` 让模型能看到 caregiver 已做的更正历史，而不只是当前值。

**注册条件**：`MedicationCoordinatorAgent` 构造时注入 `material_index`（一个暴露 `cases()`/`case(case_id)` 的薄适配器）。未注入 → 不注册 → 离线测试与现有行为不变。两者都走 `ToolExecutor` 的既有权限、超时与审计路径（`kind="read"`, `required_permission="materials:read"`）。

---

## 五、产品入口（不新建界面）

复用**已默认开启**的 `/tasks` → `evidence_review` 待办入口（`CareTasksPage.tsx:124` → `care_tasks.py:280` → `agent.run_open_review`）。本轮改动落在该链路上：

- `evidence_review` 契约新增可选 `material_case_ids`，创建待办时绑定材料；
- `_execute_evidence_review` 把 `material_index` 适配器交给 agent；
- 报告产物 `investigation_report` 结构不变（仍是 `markdown` + `investigation`），新增字段可加性；
- 进度沿用既有 `ProgressEventStore` 真实事件，**不新增伪造百分比**。

助手页自由问答路由（`AGENT_INVESTIGATION_ENABLED`）**保持关闭**：该开关默认关闭是既有产品决定，本轮不改变它，也不把"未开启"说成已交付。

---

## 六、就诊准备报告规格

报告（`CareTasks._review_markdown` 扩展）必答五问：

1. 本次调查解决了什么；
2. 哪些事实有来源支持（逐条给出实际**回读过**的 evidence/材料引用）；
3. 不同材料之间存在哪些差异；
4. 哪些问题仍缺少依据；
5. 就诊时可以向医生或药师确认什么。

**证据支持检查**：事实性结论必须关联**实际回读过**的原文——`read_refs`（标签/DDI 证据）与材料侧新增的 `material_read_refs`（`read_material_item` 走过的 `case_id/item_id`）合并成同一条引用集。只出现在 `list_materials` 索引里、未被 `read_material_item` 读过的条目**不构成引用**，与既有的"'搜到'不等于'已读取并验证'"一致。模型生成的解释性文字经 `evidence_quality.assess_claim` 同族的保守检查；无法验证的解释**降为"待确认问题"或删除**，不留在报告里当作结论。

**边界不变**：`insufficient`/`unknown` 不是"没有风险"；未列药物不代表停药；证据不足是有效结果。

---

## 七、验收规格

### 7.1 任务集（新写，≥12 个）

`stage0/agent_evals/visitprep_dev.json`，schema 沿用 `agent-task@1` 并在 `materials` 之外新增 `material_cases`。覆盖六类：

| 族 | 数量 | 考什么 |
|---|---|---|
| 信息完整 | 2 | 正常收敛、报告五问齐全 |
| 关键事实缺失 | 2 | 必须补问，且只问缺失字段 |
| 多来源冲突 | 2 | 识别分歧、保留两侧、不选边 |
| 新旧版本差异 | 2 | 材料与当前记录剂量/频次不一致 |
| 首次检索无结果 | 2 | 改写查询而非重复 |
| 无关/长材料干扰 | 2 | 不被干扰项带走 |

**成对任务**：同族内两两成对——**用户问题基本一致，只改关键证据**，验证模型是否合理改变调查行为与最终结论。成对任务是本轮"可证明的 Agent 增益"的主要证据来源。

### 7.2 评分规则（先定后跑）

覆盖：目标达成、关键事实覆盖、引用支持率、冲突识别、必要补问、错误结论、无效调用、耗时、成本、降级率。

**不算成功指标**：工具调用次数、路径与固定脚本的相似度。**合法的不同顺序不得判失败**（沿用既有 `score()` 的"无唯一动作顺序"原则）。

### 7.3 三臂与口径

| 臂 | 配置 | 用途 |
|---|---|---|
| A | `AGENT_INVESTIGATION_ENABLED=0` + `HybridPlanner(enabled=False)` | HEAD 的当前固定工作流 |
| B | 协议 v4 模型调查路径 | 本轮改造 |

- **离线替身**跑全部 12+ 任务的 A/B（零真实调用），证明状态机与执行约束；
- **真实模型**跑成对任务全集，硬上限 **400 次规划调用**，逐次记账；
- 延迟分别报：**成功回合**延迟、**全部回合**耗时、超时数、失败数。分母沿用 `latency-baseline.py` 的"可比回合"定义（丢 usage 的回合不可作分母），样本不足时逐条列出，**不用百分位宣称生产稳定性**。

### 7.4 归因要求

所有尝试进入统计。四个类别分别计数：模型选择 / 系统强制操作 / 模型纠错 / 策略降级。安全校验、权限检查、持久化**不计入**降级。

---

## 八、明确不做

1. 不新增多 Agent、角色协作或框架替换（现有 `multi_agent.py` 不动）。
2. 不放松任何医学边界，不为让报告更完整而放宽校验。
3. 不删除安全约束以提高"自主率"。
4. 不改 `AGENT_INVESTIGATION_ENABLED` 的产品默认值。
5. 不为通过验收而改成功定义、扩兜底或反复挑选成功样本。

## 九、已知风险（预先声明）

- `plan_questions` 增加一次模型往返；若模型不调用则回落到代码默认子问题，**多出的延迟可能不换来质量**——这正是本轮要用成对任务测量的东西，不能预设为正收益。
- 材料工具把 caregiver 尚未确认的候选字段暴露给模型；报告的差异陈述必须显示"候选未确认"，不得写成既成事实。
- live 批次受网关超时影响（此前观测 1/3）；超时按既有语义不重试，失败样本如实计入。
- 若 A/B 显示 B 不优于 A，本轮的工程改进（归因、去泄露、材料可见性）仍然保留，结论**允许"不达标"**。
