# material-review@2 完成条件、字段级比较与问题分流（2026-09-13，第二轮）

上一轮（[DESIGN.md](DESIGN.md) / [RESULT.md](RESULT.md)）把"基础材料覆盖"从模型手里
拿走，交给运行器；这一轮修的是它留下来的六件事，**不新建另一套任务系统**：

1. 完整交付依赖"至少发生一次模型参与"；
2. 任意开放问题都可能让报告保持 partial；
3. 用户必答问题、系统必要核对项、模型可选建议缺乏明确区分；
4. 材料存在剂型/规格等属性时，行级"一致"可能掩盖字段未确认；
5. 本应查资料的问题容易变成"请用户确认"；
6. TaskSpec 有 user_goal，但交付要求仍主要来自固定输出与条目覆盖。

**没有增加任何 gap 白名单**：下面每一条都是可推导的关系。

---

## 1. 交付要求是一个对象，不是一个布尔

`DeliveryRequirement`（[contract.py](../../../stage0/review/contract.py)）：
`requirement_id / origin / kind / subject_refs / question_refs / required /
acceptance_rule / status / evidence_refs / reason / revision`。

三类来源互不冒充：

| origin | 谁提的 | 能不能被模型降级 |
|---|---|---|
| `user` | 用户在这次任务里**明确**要求的 | 不能。`set_requirement_status` 直接拒绝把它改成 `required=false` |
| `contract` | 契约自身必需的（指定范围、安全与权限） | 不能。安全一项永远 required |
| `model_proposed` | 模型顺带提出的可选调查 | 本来就不阻塞 |

**任务理解来自明确的入口与结构化参数**（`POST /v1/care-tasks` 的 `requested`），
不是关键词解析。本轮只支持三种明确要求，各有一条 `acceptance_rule`：

| kind | 满足条件 |
|---|---|
| `list_differences` | 指定范围全部有处置，**且**每个差异/缺项都落进了报告 |
| `confirm_field` | 该字段的每个对象都比出了**确定的结论**（equal 或 different）；`expect=equal` 时还要求全部 equal |
| `answer_from_source` | 有一条**引用读回原文**的结论回答了它；失败也行，但要写清失败原因 |

这一条是 §二 那句"「列出差异和缺项」可以通过准确报告缺项完成；「确认规格完全一致」
不能因写了「规格未知」就算完成"的落点：同一个缺规格的材料，前者满足、后者不满足。

---

## 2. 完成判定：只有"必需要求都满足"这一条出口

`check_delivery` 的交付状态**只**由必需要求决定：

* 全部 `satisfied` → `complete`；
* 否则 → `partial`，并把**具体哪一项没满足、卡在什么信息来源上**写出来。

删掉了"必须有模型参与才能 complete"（上一轮的无条件要求）。取而代之的是：任务
**明确要求**语义解释或来源调查时，那条要求没满足就仍然不是完整交付——不需要一条
额外的规则去堵它。模型是否参与继续作为**独立归因字段**展示，并且不宣称"模型已经
分析"。

分工也重新划清：**合约必要项**（`contract:scope`）要求指定范围**真的比出了结果**，
`unreadable` / `unmatched` / `insufficient` 是合格处置但**不是**"核对完了"——这堵住
了"把所有 pending 改写成 insufficient 换 complete"。`list_differences` 则不同：它要的
是"该列的缺项列出来了"，缺一项不妨碍它满足。**同一份材料，两种要求，两种结果。**

状态与下一动作的对应：

| 情形 | 状态 |
|---|---|
| 必需要求满足、可选问题还开着 | `ended + complete`，可选项另列 |
| 必需要求缺事实且没有别的可推进 | `waiting_input + partial` |
| 必需要求需要取证、授权资料可用 | 继续调查（`awaiting_evidence`），不因建了补问就提前收尾 |
| 取证失败或资料不足 | 保留已有结果，写明未满足的要求与原因 |
| 用户取消 | 保留结果，不再推进或发布新的调查版本 |

---

## 3. 字段级比较：行级 `kind` 是派生的，不是判据

`FieldComparison`（[fields.py](../../../stage0/review/fields.py)）：双方的值、来源与
版本、规范化结果、`comparison_status`、原因、关联要求。状态六种：
`equal / different / missing_left / missing_right / not_comparable / invalid_value`。

* **双方都缺 ≠ 双方一致** → `missing_left`，理由是"没有可比较的信息"；
* **材料有值、记录没有** → `missing_right`，理由是"当前记录里没有这一项"；
* 单位/格式用 `normalize_field` 整理；`0.5` 对 `0.5g` 这种一侧没写单位的，
  是 `not_comparable`（说不出是不是同一个量），**不**报成"不同"；
* 日期不同只报不同，不推断"新记录取代旧记录"；
* **不做临床等效性推断**；
* 原始值与规范化值同时保留。

**行级摘要由字段结果派生**（`summarize`），并且分两层：**核心字段**
（剂量、频次、日期、给药途径）比不出结果才意味着"这条还没核对完"；剂型、规格、
单位在记录里可能**根本没有那一列**，它们照常比较、照常报告，但不把整条材料拖成
未完成——是否阻塞取决于本次的 `DeliveryRequirement`。

真实批次里正是这一层出过问题：一行写着"与当前记录一致"，末尾却挂着一句"请核实
剂型、规格"——因为那两格从来没比过。现在它是逐字段结果表里的一行
"规格：当前记录里没有这一项"。

---

## 4. 问题按**来源**分流

`Question.direction` ∈ `selected_material / authoritative_record /
reference_evidence / user_input / professional_review`。它约束的是**答案该从哪来**，
不规定用哪个工具、按什么顺序。

运行器拦的是四类真实发生过的错配：

1. **材料上已经写明的信息回头再问用户** —— `submit_question` 带
   `direction=user_input` 而该对象在材料里已经比出结论时，直接拒绝
   （`material_already_answers_this`），并指向 `base_coverage`。
2. **"说明书对此怎么说"变成"请用户确认说明书结论"** —— `reference_evidence` 或
   `professional_review` 方向的问题，挂在上面的补充请求**用户答了也不关闭**
   （`answer_input_requests` 返回 `recorded_only`）；请求没挂问题时，按**同一对象**上
   有没有这类未决问题来判断。
3. **"用户实际如何使用"被资料顶替** —— `_evaluate_answer` 对 `user_input` 方向的
   问题给 `awaiting_user`，证据再充分也不判它已回答。
4. **专业判断被用户随口确认"升级成已验证事实"** —— 同上，`professional_review`
   只能留待人工确认。

`request_information` 必须说清：缺的**具体事实**（`missing_fact`）、挡住**哪一项
必需要求**（`blocks_requirement_id`）、为什么材料给不了
（`why_material_insufficient`）、回答会被记成什么来源（`purpose`）。

**"挡住哪一项必需要求"是推导出来的，不是白名单。** `requirements_blocked_by_missing`
的两条规则：要求问的正是这些字段且对象对得上；或者 `list_differences` / 指定范围
依赖这批字段。而且只算**这份材料补一句就能比出结果**的字段——"当前记录里根本没有
这一项"（`missing_right`）被排除在外：用户说明材料上写了什么，并不会让记录长出那一列。
（真实批次暴露过这个区别：宽泛地按"未决字段"关联，会把"确认规格"并进"材料缺单位"
那条请求里，于是规格永远等不到答案。）

---

## 5. 可选问题不阻塞、也不让任务一直转

* 模型提出的问题必须有归属：`serves_requirement_id` 指向一条现有要求，或自己标成
  `optional=true`（系统会给它建一条 `model_proposed` 要求）。两者都没有 → 拒绝。
* `_independent_work` **不把可选问题算作可推进的工作**；
* `_finish` 只在存在**挡住必需要求**的补充请求时才进 `waiting_input`；
* 页面上"需要您补充"与"可选的进一步调查"分列——以前它们同属一张 pending 列表，
  于是"我还可以再查一件事"读起来和"你必须回答我"一模一样。

---

## 6. 运行数据从真实 run 引用读

上一轮的评分脚本按 `care-task:<id>:<n>` **自己拼** run_id，差一位，于是调用数与
token 一律读成 0。本轮统一到产品口径：`CareTasks.usage(task)` 只读任务自己保存的
`resource_budget.child_run_ids`，并且

* 读不到 → `measured: false`，`calls`/`tokens` 为 `None`（页面写 unknown）；
* 供应商没返回用量 → 同样 unknown；
* **不显示 0**——0 是一个测量结果，"没测到"不是。

旧契约 `evidence_review` 的累加口径**原样保留**（它在自己的契约版本下已有历史数据，
换算法会让同一字段在两版之间不可比）；新契约另有 `usage`，两者并存。

---

## 7. 没动的东西

`EvidenceStore`、`ToolExecutor`/权限/钩子、`NoProgressTracker`/取消、`TurnBudget`、
`ProductStore` 回执与事务、`MaterialIndex`、`response_safety`、`LLMPlanner` 传输层、
旧契约（`investigation@1` / `medication-evidence-review@1` / `evidence_review`）、
`reconcile_material` —— 全部原样。改动集中在 `stage0/review/` 与
`MaterialReviewCard`，加上 `care_tasks.record_input`/`create` 的参数与 `usage`。
