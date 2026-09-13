# 任务 D — 独立验收（independent acceptance）

**分支**：`codex/independent-acceptance` ｜ **worktree**：`D:\py\HealthAssistant.worktrees\independent-acceptance`
**接口**：[CONTRACT.md](./CONTRACT.md) ｜ **所有权**：[OWNERSHIP.md §1](./OWNERSHIP.md)

---

## 目标

按**冻结接口**独立验收 A/B/C 三方交付，重点是把"看起来完成了"和"确实完成了"分开。

## 你拥有的文件

- `stage0/test_parallel_product_acceptance.py`（新增）
- `scripts/parallel-product-browser-acceptance.js`（新增，确有需要时）

其余文件一律只读冻结——**你没有修复权，只报告。**

## 验收设计的三条原则

1. **接口级而非实现级**：按 [CONTRACT.md](./CONTRACT.md) 的字段名与枚举断言，
   不 import 实现者的私有函数。
2. **反例优先**：每条契约至少有一个"违反时应当失败"的用例。
3. **区分"没做"与"做了但错"**：两者报告口径不同，不要合并成一个失败。

## 必须专门覆盖的点

### 答案可信性（[CONTRACT.md §3](./CONTRACT.md)）

- 无 `assessment` 的答案 ⇒ 消费方按**未核实**，**不得**默认 `verified`。
- `status` 四值是否真的都出现过，还是实现只会吐一个值。
- `verified` 的 `reason` 是否能指到一次真实核对动作，还是无信息量的套话。
- **`verified` 被当成"整体用药安全"** ——任何这类文案或字段语义都是缺陷。

### 长期跟进（[CONTRACT.md §4](./CONTRACT.md)）

- **只给 `at` 不给确认 ⇒ `confirmed` 必须为 `false`。**（本次最关键的反例）
- 存量老记录（`confirmed=true` 但无 `confirmed_at`/`confirmation_ref`）⇒ 读作 `false`。
- 未知 `condition.kind` ⇒ `422`，**不得**静默降级成 `arrangement`。
  同理检查：降级成"永不触发"也是缺陷。
- naive（无时区）`at` ⇒ `422`。
- `schedule_state` 是否真能前进，还是永远停在 `scheduled`。
  后者**不算实现**，要如实报告。
- `cancel` 之后历史字段是否仍在。
- 三个端点是否真的走 `key` 幂等 + `expected_revision` CAS（重放与冲突各一发）。

### 环境

- 验收全程不得触碰 `stage0/memory.db`。用 `STAGE0_DB_PATH` 指向
  `output/parallel/independent-acceptance/` 下的临时库。
- 后端端口 8104，前端端口 5204（不要占用原工作区的 8000/5173）。

## 边界

- **不运行真实模型。**全部离线。
- 验收发现的问题写进本文档，**不直接改 A/B/C 的文件**。
- 需要跨模块改动时走 [CONTRACT.md §7](./CONTRACT.md)。

## 报告要求

每个验收点给三样东西：**断言了什么**、**实际观察到的**、**判定**（通过 / 未通过 / 未测到）。
"未测到"是合法的结论，不要用"通过"去填。

---
---

# D 的验收报告（并行阶段）

**基线**：`01d208d`（契约提交，即 `30f5dd4` + 契约文档）
**工件**：`stage0/test_parallel_product_acceptance.py`（30 个用例）
**基线报告**：`output/parallel/independent-acceptance/baseline-report.json`

> **本阶段的结论不是"产品通过"。** D 在并行阶段交付的是**可重复执行的验收**
> 和**它在基线上观察到的事实**。最终结论由集成阶段合并 A/B/C 之后跑这份套件得出。

## D-1. 一句话摘要

在基线上：**9 项通过、8 项未通过、13 项未测到**（共 30 项）。
8 项未通过**全部**是基线自身的缺陷——6 项是 [CONTRACT.md §4.3/§4.4/§4.5](./CONTRACT.md)
明确列为"要修掉的缺陷"的现状，2 项是一条与冻结接口无关的基线观察。
13 项未测到是 A 的 `assessment` 与 B 的三个端点/runtime 尚未落地，**不是任何一方的回归**。

## D-2. 已可执行的验收项（基线报告里已有判定）

这些用例**现在就能跑**，且判定不依赖 A/B/C 是否合并。

| 判定 | 用例 | 断言了什么 |
|---|---|---|
| 通过 | `test_the_host_refuses_to_open_the_default_patient_database` | 宿主按路径拒绝打开 `stage0/memory.db` |
| 通过 | `test_every_case_runs_in_its_own_database_under_the_task_output_dir` | 每个用例一个库，且都在本任务 `output/parallel/` 下 |
| 通过 | `test_a_read_back_quote_is_recorded_verbatim_alongside_its_answer` | **前提确认**：基线确实会让一段真实回读的引文支撑一句无关的答案（`quote`/`value`/`source`/`provenance` 逐项核对） |
| 通过 | `test_a_user_answer_is_kept_with_its_source_and_does_not_close_the_case` | 用户回答以 `user_reported`/`user_answer` 留在那条问题上，事项不因此关闭 |
| 通过 | `test_a_record_change_reopens_the_question_it_invalidates` | 记录变化后，针对旧版本的回答被重新打开，且 `answer_retired` 进历史 |
| 通过 | `test_an_answer_against_an_old_version_does_not_close_the_case` | 旧版本上的回答不批准新状态；`closure-evidence` 拒绝关闭并给出理由 |
| 通过 | `test_answering_one_question_keeps_the_answer_its_source_and_the_others` | 答一条之后：值、来源属性、**另一条未决问题**都还在 |
| 通过 | `test_the_case_records_which_revision_each_basis_was_read_against` | 事项记录它依据的输入版本；旧版本结论不被当成现行依据 |
| 通过 | `test_a_case_reopened_by_a_new_record_goes_back_for_recheck` | 记录变化 → 事项回到未解决并给出下一步 |
| 未通过 | 见 D-4 的 B-1…B-6、O-1、O-2 | 基线缺陷，**不是回归** |

## D-3. 等待合并后执行的验收项

这 13 项在基线上是"未测到"（功能根本不存在，用 `skipTest` 表达，理由以「基线缺失」开头）。
A/B 合并之后它们会真正开始断言；**只有它们全部通过，本类别才算通过**
（报告里 `passed` 只在全通过时为真，"未测到"不折算成通过）。

**等 A（`assessment`，5 项）**

| 用例 | 断言了什么 |
|---|---|
| `test_a_real_quote_does_not_make_an_unrelated_answer_verified` | 真实回读但与答案无关的引文 ⇒ `status != verified` |
| `test_a_user_answer_is_never_verified` | `user_reported` ⇒ 永不 `verified` |
| `test_a_professional_opinion_is_never_verified` | `professional_opinion` ⇒ 永不 `verified` |
| `test_a_retired_answer_is_not_still_verified` | 依赖版本已变 ⇒ 旧答案不带 `verified` |
| `test_the_status_is_not_a_single_constant` | `status` 不是常量（防"永远不 verified"式虚假通过） |

**等 B（长期跟进，8 项）**

| 用例 | 断言了什么 |
|---|---|
| `test_the_schedule_endpoint_honours_idempotency_and_cas` | `key` 重放不产生新修订；`expected_revision` 不匹配 ⇒ 409 |
| `test_cancelling_keeps_the_arrangement_history` | `cancel` 后 `at`/`owner`/`note`/`kind` 仍在，只有状态变 |
| `test_a_confirmation_needs_an_arrangement_and_an_authenticated_actor` | 无安排 ⇒ 409；`confirmed_by` 来自认证上下文而非请求体 |
| `test_a_due_arrangement_advances_and_does_not_repeat_on_a_rescan` | `scheduled → due/triggered` 真的前进；重扫与**重启**都不重复触发 |
| `test_a_future_arrangement_does_not_fire_early` | 未到期不触发 |
| `test_cancelling_stops_the_arrangement_from_advancing_again` | 取消后不再被扫描推进 |
| `test_rescheduling_replaces_the_old_due_time` | 改期替换旧到期时间，旧的时刻不再触发 |
| `test_a_failed_follow_up_run_leaves_the_case_unresolved` | 跟进失败 ⇒ 事项仍 `UNSETTLED`、无 `resolution_basis`、有下一步；`blocked` 必须带 `blocked_reason` |

**关于"到期"的可控时钟**：用例不伪造系统时间，而是安排一个**过去**的时刻（已到期）
与一个**远期**时刻（未到期）来区分两条分支，再驱动真实 `worker.drain_once()`。
这样"时钟"完全可控，且不需要 patch 生产的时间源。

## D-4. 基线问题逐条

每条给五样：触发步骤 / 预期行为 / 实际行为 / 证据位置 / 处理方。

### B-1 `confirmed` 由 `at` 或 `condition` 派生（CONTRACT.md §4.5）

- **触发**：`POST /v1/safety-cases/{id}/disposition`，`disposition=accepted_monitoring`，
  `follow_up={"kind":"review_at","at":"2999-01-01T00:00:00+00:00"}`（**不给任何确认**）。
- **预期**：`follow_up.confirmed is False`，`schedule_state` 仍可为 `scheduled`。
- **实际**：`confirmed: true`、`confirmed_at: null`、`confirmation_ref: null`。
- **证据**：`baseline-report.json` → `a_time_is_not_a_confirmation` →
  `test_a_time_alone_does_not_confirm_an_arrangement`；
  代码 [safety_cases.py:219](../../stage0/safety_cases.py#L219)。
- **处理方**：**B**（`stage0/safety_cases.py`）。

### B-2 存量记录 `confirmed=true` 且无确认信息被原样读出（CONTRACT.md §4.5）

- **触发**：在隔离库里把事项的 `follow_up` 置成
  `{confirmed: true, confirmed_at: null, confirmation_ref: null}`（模拟老记录），再 `GET` 该事项。
- **预期**：读作 `confirmed: false`（契约明说这是一次**有意的、可见的行为回退**）。
- **实际**：原样读出 `confirmed: true`。
- **证据**：`test_a_legacy_confirmed_record_without_a_confirmation_reads_as_unconfirmed`。
  **注意**：这条用例为造"老记录"向隔离库种了一条历史数据——种的是**必须被读成 false
  的损坏状态**，不是制造成功。
- **处理方**：**B**。

### B-3 未知 `condition.kind` 被静默接受（CONTRACT.md §4.3）

- **触发**：`follow_up={"kind":"on_event","condition":{"kind":"made_up_trigger","ref":"…"}}`。
- **预期**：`422`。**不得**静默降级成 `arrangement` 或"永不触发"——那会让一条永远
  不会生效的安排看起来完全正常。
- **实际**：`200`，安排被接受。
- **证据**：`test_an_unknown_condition_kind_is_refused_not_silently_downgraded`。
- **处理方**：**B**。

### B-4 自由文本 `condition` 被接受（CONTRACT.md §4.3）

- **触发**：`follow_up={"kind":"on_event","condition":"等医生说了算"}`。
- **预期**：`422`（非结构化字符串）。**实际**：`200`。
- **证据**：`test_a_free_text_condition_is_refused`。**处理方**：**B**。

### B-5 naive 时间被接受（CONTRACT.md §4.4）

- **触发**：`follow_up={"kind":"review_at","at":"2999-01-01T00:00:00"}`（无时区）。
- **预期**：`422`（对齐既有 `care_task.due_at` 的口径）。**实际**：`200`。
- **证据**：`test_a_time_without_a_timezone_is_refused`。**处理方**：**B**。

### B-6 带时区的时间未被规范化（CONTRACT.md §4.4）

- **触发**：`at="2999-01-01T00:00:00.000Z"`（前端 `Date.toISOString()` 的形状）。
- **预期**：接受，并在响应里规范化成 `+00:00` 秒精度（与 `utc_now()` 同款）。
- **实际**：原样回显 `2999-01-01T00:00:00.000Z`。
- **证据**：`test_a_timezone_aware_time_is_accepted_and_normalised`。**处理方**：**B**。

### O-1 / O-2 实体名重合被当成"这条问题已有依据"（**基线观察，不在 A/B/C 的冻结交付内**）

这两条单列一类 `baseline_observation_outside_the_frozen_scope`，**请不要读成任何一方的回归**。

- **触发**（全程走产品路径，脚本化规划器、合成语料，无模型无网络）：
  1. 调查里声明一条问题：`statement="合成药乙目前的服用频次是什么？"`、
     `information_target=general_reference`、`strategy=general_reference`；
  2. `rag_search` 命中一段与问题**只共享实体名「合成药乙」**、内容讲的却是合用出血风险的
     材料；`read_evidence` 回读它；
  3. 不再有别的动作。
- **预期**：材料被**读过**不等于它**支持**这条问题（`investigation.py:_source_supports`
  的注释本身就这么写）。问题应保持未决。
- **实际**：
  - `question.information_state = "available"`、`status = "answered"`、
    `answered_by = "evidence"`、`evidence_refs = ["ev-…"]`；
  - 但 `question["answers"]` **不存在**——`case_view` 的 `answered_parts` 因此也没有内容。
    界面读到的是"这条已经查清了"，却指不出查清它的那条答案。
- **证据**：用例 `test_claim_support_is_not_inferred_from_a_shared_entity_name`、
  `test_a_question_read_as_available_can_name_its_answer`；
  触发点在 [investigation.py:1698-1711](../../stage0/investigation.py#L1698-L1711)
  （`_sync_question_from_claim` → `settle_question(..., answered_by='evidence')`），
  判定来自 `stage0/evidence_quality.py:assess_claim`。
- **处理方**：`stage0/investigation.py` 属 **A**；`stage0/evidence_quality.py` **不在**
  四方的所有权清单内（共享冻结），需要改动时走 [CONTRACT.md §7](./CONTRACT.md)，
  由集成 Agent 统一处理。
- **与本轮冻结接口的关系**：A 的 `assessment` 落地之后，第 2 条用例会自动从"未测到"
  变成可判定（要么 `answers` 有内容，要么这条问题不该是 `available`）。**集成阶段请重点复看它。**

## D-5. 环境变量与命令

不需要任何凭据、不需要 `.env`、**不需要真实模型**。全程离线。

```bash
cd D:/py/HealthAssistant.worktrees/independent-acceptance

# 跑全套并写报告（推荐——报告里区分 通过 / 未通过 / 未测到）
PYTHONIOENCODING=utf-8 MEMORY_ENABLE_LLM=0 \
  D:/py/HealthAssistant/.venv/Scripts/python.exe \
  -m stage0.test_parallel_product_acceptance \
  --report output/parallel/independent-acceptance/baseline-report.json

# 只跑某一类
PYTHONIOENCODING=utf-8 MEMORY_ENABLE_LLM=0 \
  D:/py/HealthAssistant/.venv/Scripts/python.exe -m unittest \
  stage0.test_parallel_product_acceptance.CitationTests -v
```

- `MEMORY_ENABLE_LLM=0` 只是双保险：套件自己把规划器换成脚本化的 `proposal_provider`，
  不读任何凭据、不发任何网络请求。
- 数据库：每个用例在自己的 `output/parallel/independent-acceptance/cases/<用例名>/host/memory.db`
  下新建并清理。**不设 `STAGE0_DB_PATH` 也安全**——`_Host` 永远显式传 `db_path`，
  并且有一条护栏用例断言它拒绝指向 `stage0/memory.db`。
- 端口：**不监听任何端口**（`fastapi.testclient` 进程内直连，`worker_thread=False`）。
  8104/5204 实际上没有被占用。
- 基线耗时约 36–45 秒。

### 集成阶段必须知道的一件事

`scripts/verify-agent-closeout.py` 会 glob `stage0/test_*.py` 并逐个以 `unittest` 运行、
要求退出码为 0。**本套件在基线上是红的（8 项未通过）**，因此：

- 合并 A/B 之前，请把本文件排除出 closeout glob，或接受 closeout 为红；
- 合并 A/B 之后，**期望值**是：`failed = 0`（O-1/O-2 若被判定为范围外，需显式移出
  `CATEGORY_OF` 或随 A 的修复一并转绿），`not_implemented = 0`，`passed = 30`。
- 若届时 `not_implemented` 仍非 0，说明某项交付**根本没落地**——报告里逐条名了。

**基线期望值**（用于判断"还是基线"还是"出现了新问题"）：
`{'passed': 9, 'failed': 8, 'not_implemented': 13}`。

## D-6. 是否需要真实浏览器

**视觉部分需要，但本轮没有交付浏览器脚本。** 理由与边界如下：

- 本轮的 30 项全部在后端/传输层判定，**不需要浏览器**，也都已经能跑。
- §3.4 要求的"5 个视觉状态"（4 个 `status` + 未核实）与 §4.7 要求的
  "已安排未确认 / 已确认"的**视觉可区分性**，只能在真实浏览器里判定，属 C 的交付面。
- D 的 worktree 里没有 `node_modules`，本轮**无法运行**任何浏览器脚本。
  交付一个跑不起来、也无法自证的脚本，等于把未经验证的东西当成证据——所以没有交付。

**集成阶段需要补的浏览器验收（清单，不是脚本）**：

1. 无 `assessment` 的答案渲染成"未核实"，**不是** verified，且与四个 `status` 在视觉上可区分（5 态）。
2. 含 `verified` 的文案**不得**出现"安全""已确认无误""可以放心"这类整体性结论。
3. `follow_up.confirmed=false` 且 `schedule_state=scheduled` 时，显示"已安排未确认"这个中间态，
   与"已确认"可区分。
4. `schedule_state ∈ {blocked, cancelled}`、`last_trigger_reason`、`blocked_reason` 在页面上可见。
5. 三个动作入口（改期/取消/确认）发出的请求体与 §4.6 一致，且 409 有明确反馈。
6. 端口 5204（`STAGE0_DEV_API_TARGET="http://127.0.0.1:8104"`），**不要占用 5173**。

## D-7. 接口需求（CONTRACT.md §7）

**无。** 本轮没有需要修改他人文件的地方：所有验收都从已冻结的端点与投影进入，
没有 import 任何实现者的私有函数，也没有需要 A/B/C 增补的字段。

O-1/O-2 若要修，涉及 `stage0/evidence_quality.py`（共享冻结），按 §7 交由集成 Agent 处理；
本文档不擅自扩权。

## D-8. 与最终结论的关系

- 本阶段的产出是**验收能力**与**基线事实**，不是产品通过。
- 集成阶段合并 A/B/C 后，用 D-5 的同一条命令重跑，
  由 `baseline-report.json` 的 `categories[*].passed` 逐类给出结论。
- 任何"未测到"都不折算为通过；任何"未通过"都要能落到 D-4 的某一条
  或某位所有者头上，否则就是本套件自己的问题，应由 D 修。
