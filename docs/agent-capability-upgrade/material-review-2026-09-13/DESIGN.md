# material-review@2 设计决策记录（2026-09-13）

**这份文件约束本轮实现，不描述过程。** 它回答六件事：复用谁、适配谁、新增什么、
旧路径留什么、新任务的完成条件是什么、持久状态怎么版本化。

---

## 0. 一句话

把"以用户交付物为目标的证据调查"做成一条独立契约：**代码准备可信上下文并守住
边界，模型只做调查策略**，完成与否由交付要求决定，不由某个固定的药物核查模板决定。

---

## 1. 直接复用（不改语义）

| 设施 | 位置 | 复用的理由 |
|---|---|---|
| `EvidenceStore` | `harness/evidence.py` | 证据真相源唯一。新契约不新增平行证据源，只引用 `evidence_id`。 |
| `ToolExecutor` / `ToolSpec` / `HarnessHooks` / `PERMISSION_ROLES` | `harness/tools.py` | 身份、作用域、参数校验、预算、取消、审计、结果规范化只有一份实现。 |
| `ProgressEventStore` / `NoProgressTracker` / `cancel_event_for` | `harness/progress.py` | 进展事件、无进展判定、取消信号按 run_id 复用。 |
| `TurnBudget` / `budget_scope` / `check_lease` | `turn_budget.py` | 预算与会话租约。新路径不新建预算设施。 |
| `ProductStore.command` 回执/事务 | `product.py` | 幂等回执与 `BEGIN IMMEDIATE`。 |
| `MaterialIndex` | `product.py` | 材料只读适配器：候选、确定性差异、原文定位。 |
| `MemoryStore.snapshot()` / `scope_revision()` | `memory.py` | 权威事实与版本。 |
| `response_safety` 交付前校验 | `response_safety.py` | 不可放宽的安全边界。 |

## 2. 适配复用（换形状，不换语义）

| 设施 | 适配方式 |
|---|---|
| `LLMPlanner` 的**传输层** | `review.capabilities.ReviewPlanner` 继承它，只覆盖 `system_prompt` / `prompt_payload` / `tool_definitions`；重试、429 退避、`tool_choice` 降级、多调用裁剪、用量记账全部沿用。 |
| `harness.retrieval.search` | 作为 `research_evidence` 的**内部实现**被调用，不直接暴露给模型。 |
| `evidence_quality.assess_claim` + `claim_support.assess_support` | 作为 `review.verify` 的确定性比较器，用于"原文摘录"类核查。 |
| `CareTasks` 生命周期 | `material_review` 作为新 `goal_type` 接入同一套 create/resume/cancel/worker 租约/累计预算。 |
| `ddi_engine.detect` | 作为**强制安全检查**执行一次，结果存入报告的独立 `safety` 段，不进入本任务的 findings/questions。 |

## 3. 新增（`stage0/review/`）

按职责边界拆分，不建无用小文件：

| 模块 | 职责 |
|---|---|
| `contract.py` | `material-review@2` 的 `TaskSpec`、`requested_outputs`、`coverage_requirements` 的构造规则。 |
| `state.py` | `Question` / `Finding` / `Assertion` / `InputRequest` / `ExecutionIssue` / `EvidenceRef`、三条独立状态轴、`MaterialReviewState` 与版本化 `restore()`。 |
| `context.py` | 调用模型前的可信上下文准备（版本引用、事实摘要、材料索引、确定性差异、已办/待办、有效证据、剩余预算）。 |
| `capabilities.py` | 领域能力 ToolSpec + 处理器；`ReviewPlanner` 适配器。 |
| `verify.py` | 断言—证据关系：结构化比较 / 原文摘录 / 语义解释三档核查。 |
| `delivery.py` | 交付检查（按 `TaskSpec` 逐项核对）与三条状态轴的取值计算。 |
| `report.py` | 业务语言报告渲染 + 版本间差异说明。 |
| `incremental.py` | 材料版本 → 证据 → 断言/发现 → 问题 → 报告版本的关系与受影响部分重算。 |
| `advance.py` | `advance_task()`：聊天入口、持久 worker、评测入口共用的唯一推进核心。 |

## 4. 旧路径保留

| 旧路径 | 处置 |
|---|---|
| `investigation@1` / `medication-evidence-review@1` | **原样保留**，可恢复、可查看历史结果。`evidence_review` 待办继续由旧执行器处理。 |
| `reconcile_material`（v2）逐项确认流程 | 原样保留。 |
| Harness P3（`batch_read` / `delegate_task`） | 原样保留，默认关。 |
| `test_*` 历史回归 | 原样保留，必须继续通过。 |

**不原地修改旧契约语义**：新任务使用新 `contract_version`；报告头标明版本；旧任务
的 `contract_version` 校验不变。

## 5. 新任务契约的完成条件

`material-review@2` 的完成**不是**"所有问题都有确定答案"，而是：

1. 每个 `coverage_requirement` 都有处置（`covered` / `unreadable` / `unmatched` / `insufficient`），
   **不允许靠忽略相关条目使任务变便宜**；
2. 每个 `requested_output` 都有内容或显式的空态说明；
3. 每条作为结论出现的**事实性断言**关联到仍然有效的证据（`reference_valid`）；
4. 已知差异与反对证据没有被遗漏；
5. 未解决项被明确写出，且**未决问题不得被删除**（只能关闭或标注取代）；
6. 无越权输出（不诊断、不处方、不调整用药）；
7. 交付状态为 `complete` 或 `partial`，二者都允许，"完整"指满足本任务承诺的交付要求。

合法组合：`run=ended / delivery=complete / evidence=conflicting` 是**成功**，
不是失败——"存在冲突，已定位双方来源，需人工确认"本身就是可交付的完整报告。

## 6. 持久状态的版本与迁移

- `MaterialReviewState.version = 'material-review-state@1'`，`contract_version = 'material-review@2'`。
  `restore()` 对未知版本抛 `ValueError('material review migration required')`；不做静默降级。
- 状态是**追加式**的：问题修订增加 `revision` 并保留历史，取消/取代写 `superseded_by`，不删除。
- 报告是**版本化产物**：`material_review_report` 每次修订一个新 `id`，新版本不覆盖旧报告。
- 旧任务（`evidence_review` / `reconcile_material`）的 `investigation` 载荷不受影响，
  仍由旧执行器读写；新执行器只处理 `goal_type == 'material_review'`。
- 无法可靠迁移的旧状态继续走旧执行器，**不把旧评测结果冒充新设计的验收**。
