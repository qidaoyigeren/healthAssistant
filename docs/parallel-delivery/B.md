# 任务 B — 长期跟进 runtime（followup runtime）

**分支**：`codex/followup-runtime` ｜ **worktree**：`D:\py\HealthAssistant.worktrees\followup-runtime`
**接口**：[CONTRACT.md §4](./CONTRACT.md) ｜ **所有权**：[OWNERSHIP.md §1](./OWNERSHIP.md)

---

## 目标

把 `follow_up` 从一条**只写一次、不可改、不可取消、无人执行**的声明，
变成有确认、有状态、有调度、有错误表达的长期跟进。

## 你拥有的文件

- `stage0/safety_cases.py`、`stage0/care_tasks.py`、`stage0/server.py`
- `stage0/memory.py`、`stage0/safety_checks.py`
- `stage0/followup_runtime.py`（如需新增）
- `stage0/test_followup_runtime.py`（新增）

其余文件一律只读冻结。

## 当前状态（起点，都是要改的）

- `follow_up` 只在 `accepted_monitoring` 处置分支写入一次（`safety_cases.py:925`）。
- **没有修改端点，没有取消端点。**`derive_status()` 从不清除它——
  事项可以从 `monitoring` 走到 `needs_recheck` 而 `follow_up` 原样留着。
- `confirmed` 是**派生**的：`bool(at or condition)`（`safety_cases.py:203-225`）。
- `at` 是**未经解析的调用方字符串**，前端送毫秒 + `Z`，测试送 `+00:00`，没有时区校验。
- `condition` 是**自由文本，从不求值**。
- **没有调度状态机**：没有 due 计算、没有触发、没有 worker 扫描 `monitoring` 事项。
  `care_task.due_at` 是只写不读的（校验了时区，然后没人读它）。

## 冻结接口

四个动作（详见 [CONTRACT.md §4](./CONTRACT.md)）：

- `POST /v1/safety-cases/{case_id}/follow-up`，`action:"schedule"` / `action:"cancel"`
- `POST /v1/safety-cases/{case_id}/follow-up/confirmation`
- 三者都沿用 `key`（幂等）+ `expected_revision`（CAS），返回 **CaseView**。

`follow_up` 对象兼容式扩展：既有 6 键（`kind`/`at`/`condition`/`owner`/`note`/`recorded_at`）
保留，新增 `confirmed_at`/`confirmed_by`/`confirmation_ref`/`revision`/`schedule_state`/
`last_triggered_at`/`last_trigger_reason`/`care_task_id`/`blocked_reason`。

四条硬约束：

1. **有时间或条件 ≠ 已确认。** 只给 `at` ⇒ `confirmed: false`。
   `confirmed: true` 必须来自确认记录，且 `confirmed_at`/`confirmation_ref` 非空。
   存量老记录（`confirmed=true` 但无确认信息）**按 false 读**。
2. **`condition` 只接受白名单结构**，未知 `kind` → `422`。不得静默降级。
3. **`at` 必须带时区**，naive → `422`，响应统一规范化成 `+00:00` 秒精度。
4. **`kind` 与 `schedule_state` 是两件事**，不合并。

## 验收标准

- `schedule_state` 能真实前进 `scheduled → due → triggered`；失败进 `blocked` 且带
  `blocked_reason`。**"永远停在 scheduled"不算实现。**
- 只给时间不给确认 ⇒ `confirmed: false`（这条要有专门的测试）。
- 未知 condition kind ⇒ 422；naive 时间 ⇒ 422。
- `cancel` 不清空历史字段，只用状态表达取消。
- `stage0/test_followup_runtime.py` 离线可跑，不调用外部模型。

## 需要越界时

走 [CONTRACT.md §7](./CONTRACT.md)：在本文档追加"接口需求"一节，不要直接改别人的文件。

---

# 交付（B，2026-09-13）

## 一、改了什么

| 文件 | 状态 | 内容 |
|---|---|---|
| `stage0/followup_runtime.py` | 新增 | 校验/规范化、安排与确认语义、触发身份、调度状态推进、`OutboxWorker` 周期里的消费循环 |
| `stage0/test_followup_runtime.py` | 新增 | 78 个用例，离线可跑 |
| `stage0/memory.py` | 修改 | `follow_up_runs` 表 + `_ensure_follow_up_schema`（additive only） |
| `stage0/safety_cases.py` | 修改 | 三个动作的 store 方法、三个端点、`case_view` 的只读投影、`_normalise_follow_up` 不再派生确认 |
| `stage0/server.py` | 修改 | `drain_once()` 增加跟进周期（10 行） |

**没有改** `stage0/care_tasks.py` 与 `stage0/safety_checks.py`：它们已有的能力
（`safety_case` 契约的 `create`/`resume`、`necessary_checks` 队列）**原样复用**即可，
不需要改动。所有权给了改动权，不等于必须改动。

## 二、接口（按 CONTRACT §4 冻结形状实现）

### 2.1 三个端点

```
POST /v1/safety-cases/{case_id}/follow-up                 action: schedule | cancel
POST /v1/safety-cases/{case_id}/follow-up/confirmation
```

三者都走 `key`（幂等）+ `expected_revision`（CAS），返回 **CaseView**。未知
`action` → `422`（不是静默无操作）。失败沿用既有信封与 `ProductError.status`：
CAS 不符/终态/无安排可确认 → `409`；naive 时间 / 未知 condition kind → `422`。

### 2.2 `follow_up` 字段

既有 6 键原样保留，新增 `confirmed_at`/`confirmed_by`/`confirmation_ref`/`revision`/
`schedule_state`/`last_triggered_at`/`last_trigger_reason`/`care_task_id`/`blocked_reason`。
`kind` 与 `schedule_state` 是两件事，未合并。

### 2.3 与 worker 的接口（这是本轮的核心）

```
OutboxWorker.drain_once()
  ├─ claim outbox tasks
  ├─ mark_overdue_review_cases()
  ├─ drain_resume_tasks()
  ├─ run_necessary_checks()          ← 确定性检查
  └─ run_follow_ups()                ← 新增，紧随其后
```

放在必要检查**之后**是刻意的：相关变化触发的确定性检查总是先跑完，之后才轮到
消费这一版模型调查。`run_follow_ups` 内部还会再挡一道——只要还有
`status IN ('open','running')` 的必要检查，任何一版调查都不消费（记为 `deferred`）。

**没有新增调度框架，没有新增后台模型循环**：租约、重试、失败可见性的列形状与口径
沿用 `necessary_checks` / `dependency_tasks` / `resume_tasks`；消费入口就是那一个既有的
worker 周期。

## 三、语义决策（契约没写死、由实现选定的部分）

1. **`confirmed` 只能来自确认端点。** `_normalise_follow_up` 与 `build_arrangement`
   产出的 `confirmed` 恒为 `false`；`confirmed_by` 取认证主体，请求体自称不被读取。
2. **确认不推进"安排 revision"。** 确认改变的是"谁承诺了"，不是"安排是什么"。
   推进它会让一条刚确认的安排作废自己已经排好的触发。改期与取消**才**推进它。
3. **条件按"持久信号是否存在"求值**，不执行自然语言、不问模型：
   - `conclusion_recorded` → 存在 `status='current'` 且 ref 匹配的结论；
   - `necessary_check` → 存在 `status='done'` 且 ref（可选 `check_id`）匹配的检查行；
   - `medication_change` / `fact_change` → 存在对应 `trigger_kind` 且 ref 匹配的行，
     复用既有 `TRIGGER_MEDICATION_SET` / `TRIGGER_CONDITION_FACTS` 词汇。
   ref 匹配按**版本号之前**的头部比较，所以 `...@1` 与 `...@v1` 指同一条记录。
   含义是"该条件已被满足"，因此**安排时条件若已成立，下一次扫描即触发**——这是
   刻意的，不是缺陷（"等它被记录"而它已经记录了）。
4. **`kind='review_at'` 但没给时间 ⇒ 降级成 `arrangement`**（停在 `unscheduled`）；
   **未知的 kind 词 ⇒ `422`**。前者是"没有可排的东西"，后者是调用方拼错了。
5. **取消允许从 `unscheduled` 出发**；已 `triggered` 或已 `cancelled` 再取消 → `409`。
6. **逾期不顺延。** 到期后失败进 `blocked` 并带 `blocked_reason`，三次尝试后该触发行
   判 `failed` 保持可见，**不会**静默改期到下一轮。恢复要靠重新安排或 provider 恢复。

## 四、测试

```bash
cd D:/py/HealthAssistant.worktrees/followup-runtime
D:/py/HealthAssistant/.venv/Scripts/python.exe -m unittest stage0.test_followup_runtime -q
```

**本项目没有 pytest**（无 `conftest.py`/`pytest.ini`/`pyproject.toml`，`requirements*.txt`
里也没有），所以 [OWNERSHIP.md §3](./OWNERSHIP.md) 给出的 `python -m pytest stage0 -q`
在本仓库跑不起来。CI 同款是：

```bash
D:/py/HealthAssistant/.venv/Scripts/python.exe scripts/verify-agent-closeout.py --out output/parallel/B/<新目录>
```

（`--out` 目录必须不存在；它会逐个 `python -m unittest stage0.test_<x> -q`。）

`stage0/test_followup_runtime.py` 共 85 个用例，全部离线（`MemoryStore(llm_enabled=False)`），
**不调用真实模型、不使用 sleep**：时间由显式 `now=` 参数与显式时间戳控制。

覆盖验收清单逐条对应：

| 验收项 | 用例 |
|---|---|
| 未到期不执行 | `test_an_arrangement_that_is_not_due_yet_does_not_run` |
| 到期触发 | `test_a_due_arrangement_triggers_an_investigation` |
| 可控时间（不 sleep） | `test_the_trigger_time_is_controllable_not_wall_clock_guessed` |
| 重复扫描不重复 | `test_scanning_twice_does_not_start_two_investigations` |
| 重启恢复 | `test_a_restarted_process_recovers_a_claimed_but_unfinished_trigger` |
| 改期使旧安排失效 | `test_rescheduling_invalidates_a_trigger_that_was_already_queued` |
| 取消使旧安排失效 | `test_cancelling_an_arrangement_stops_it_from_ever_triggering` |
| 相关变化触发 | `test_a_relevant_record_change_triggers_the_arrangement` |
| 无关变化不触发 | `test_an_unrelated_record_change_does_not_trigger_the_arrangement` |
| 必要检查先于调查 | `test_a_pending_necessary_check_defers_the_investigation` + worker 端到端 |
| provider 不可用保留任务与安全结果 | `test_a_failed_investigation_leaves_the_arrangement_and_the_safety_result` |
| 只给时间不给确认 ⇒ `confirmed: false` | `test_a_time_alone_does_not_confirm_an_arrangement` 等 6 条 |
| 存量记录按 false 读 | `test_a_legacy_confirmed_record_reads_as_unconfirmed_through_the_case_view` |
| 未知 condition kind ⇒ 422 | 纯函数 + `test_an_unknown_condition_kind_is_rejected_with_422` |
| naive 时间 ⇒ 422 | 纯函数 + `test_a_naive_time_is_rejected_with_422` |
| **真的接进 worker** | `FollowUpWorkerWiringTests`（走 `create_app` + `worker.drain_once()`） |

`AssessmentProjectionTests` 用**变异检查**验证过不是空转：把 `case_view` 改成给每条
答案补一个默认 `assessment`（§3.4 明令禁止）会让其中 2 条失败，还原后恢复。
`test_a_stale_trigger_that_comes_back_is_voided_not_counted_as_a_failure` 同样用变异
检查验证过（把"作废"改回"失败"即失败）。

### 自查发现并修掉的三处缺陷

写完之后我按"**这条安排失败时会怎样**"和"**这个条件真的能匹配上吗**"重新读了两遍，
发现三处缺陷。常规路径下测试全绿——它们不是测试写少了，是设计没想完：

1. **重试耗尽后安排会退回"已排队"的样子。** `scan_follow_ups` 每次扫描都把安排盖成
   `due`，而对应的触发行已经 `failed`、永远不会再执行。结果是一条**没人会做**的安排
   显示成"已排队"——正是 `kind='arrangement'` 静默降级同一类的谎。修复：入队时如实
   回报那一行的真实状态，据此决定 `due`/`triggered`/`blocked`。
   用例：`test_an_exhausted_trigger_does_not_look_merely_queued`（修前实测 `'blocked' != 'due'`）。
2. **等待也消耗重试次数。** 认领时 `attempts+1`（沿用既有队列口径），但本队列会
   "等必要检查跑完"再放回去——三次延迟就把一次都没试过的触发判死。修复：认领不计数，
   只在**真的失败**时递增。
   用例：`test_waiting_for_a_check_does_not_spend_the_retry_allowance`。
3. **`necessary_check` 条件只查了用药类触发。** 查询被写死成
   `WHERE trigger_kind='medication_set'`，于是"某项必要检查完成"在**患者事实**类检查上
   永远不触发。修复：该条件跨两种触发类型匹配。
   用例：`test_a_necessary_check_condition_matches_checks_of_either_trigger_kind`（修前实测 `1 != 0`）。

顺带收紧了一处：worker 每 0.2 秒扫一次，`_stamp` 现在只在**真有变化**时写，所以没有
变化的扫描不会推进事项 `revision`、也不会往历史里刷一条什么都没说的记录
（`test_a_sweep_that_changes_nothing_does_not_churn_the_case`）。

### 全量离线验收的实际结果

```
scripts/verify-agent-closeout.py
```

**56 个套件、恰好 2 处失败，其余全绿**，且这两处都是 §6.1 记录的契约强制回退：

| 套件 | 用例 | 断言 |
|---|---|---|
| `test_safety_cases` | `test_monitoring_with_an_explicit_schedule_keeps_it` | `:241 assertTrue(follow_up['confirmed'])` |
| `safety-mainline-acceptance`（`test_safety_mainline_e2e`） | `test_a_routine_sync_does_not_erase_a_monitoring_arrangement` | `:339 assertTrue(synced["follow_up"]["confirmed"])` |

两处都是 `AssertionError: False is not true`——即修复后 `confirmed` 不再被"给了一个
时间"置真。`test_followup_runtime` 在同一轮里 85 个用例全过。

这两处**不是回归**：它们断言的是契约明文要求修掉的缺陷，而修复后原断言必然为假。
D 与集成 Agent 应按 §6.1 的补丁处理，不要按契约违反处理。

## 五、部署要求

1. **迁移是 additive 的**：`follow_up_runs` 建表 + 索引，不改任何既有表/列，历史行
   一律不动。首次启动由 `MemoryStore` 自动执行，无需停机窗口。
2. **worker 必须在跑**：跟进只在 `OutboxWorker` 周期里推进。`worker_thread=False`
   （测试/脚本）时必须显式调 `drain_once()`。**没有 worker 就没有后台执行**——
   这一点必须体现在对外文案上（本模块不承诺"后台已在执行"）。
3. **只做应用内跟进**：不发短信、邮件或任何外部通知。
4. **无需新配置**。可选 `FOLLOW_UP_LEASE_TTL_SECONDS`（默认 300，与
   `NECESSARY_CHECK_LEASE_TTL_SECONDS` 同口径）。
5. **不宣称 exactly-once**：触发身份（`case_id` + 安排 revision + 触发实例）落
   `UNIQUE` 约束，重复扫描与重启命中同一行；用户看不到重复结果，但这是**幂等业务
   写入 + 重放**，不是物理意义上的恰好一次。
6. 默认 `MAX_JOBS=8` / `MAX_ATTEMPTS=3`；失败重试沿用既有退避（5s 起、上限 300s、20% 抖动）。

## 六、接口需求（CONTRACT §7 范围外变更）

### 6.1 两个基线用例断言的正是要被修掉的缺陷（**阻塞，需集成 Agent 处理**）

`_normalise_follow_up` 不再派生 `confirmed` 之后，两个**不属于我**的文件里的用例会失败。
它们断言的是 §4.5 明文要求修掉的行为：

| 文件:行 | 现状断言 | §4.5 要求 |
|---|---|---|
| `stage0/test_safety_cases.py:241` | `assertTrue(follow_up['confirmed'])` | 只给 `at` ⇒ `confirmed: false` |
| `stage0/test_safety_mainline_e2e.py:339` | `assertTrue(synced["follow_up"]["confirmed"])` | 同上 |

- **精确接口需要**：把这两处断言改为 `assertFalse(...)`，并补一条断言
  `confirmed_at`/`confirmation_ref` 为 `None`。测试名
  `test_monitoring_with_an_explicit_schedule_keeps_it`（`test_safety_cases.py:235`）本身
  也需要改名——它锁的语义已经变了。
- **建议补丁**：
  ```diff
  --- a/stage0/test_safety_cases.py
  +++ b/stage0/test_safety_cases.py
  -        self.assertTrue(follow_up['confirmed'])
  +        # §4.5：给了时间不等于有人确认过。
  +        self.assertFalse(follow_up['confirmed'])
  +        self.assertIsNone(follow_up['confirmation_ref'])
  ```
  ```diff
  --- a/stage0/test_safety_mainline_e2e.py
  +++ b/stage0/test_safety_mainline_e2e.py
  -        self.assertTrue(synced["follow_up"]["confirmed"])
  +        self.assertFalse(synced["follow_up"]["confirmed"])
  ```
- **影响面**：B（本分支）、D（§4.7 明确要求 D 专门验收这条回退）。C 的前端
  `labels.ts:142` 已经按 `!confirmed` 分支渲染"这是一项待确认的安排"，无需改动。
- **为什么不能在自己的所有权内绕开**：这两个文件不在 B 的所有权清单里
  （[OWNERSHIP.md §1](./OWNERSHIP.md)），直接改就是擅自扩权。**我没有改它们**，
  因此本分支的全量离线验收会带 **2 处失败**，且这 2 处是契约要求的结果、不是回归。
  D 与集成 Agent 不应把它当成契约违反。

### 6.2 A 的答案依赖接口（**已定义调用边界，等 A 交付**）

按 CONTRACT §3.6，B 是"投影方"，只触发与搬运：

```python
# stage0/followup_runtime.py
def recheck_answer_dependencies(product, case, *, changed_refs=(), reason=None) -> dict
```

- B 在 `on_event` 触发命中时调用它，`changed_refs=[condition['ref']]`。
- 期望 A 在 `stage0/answer_grounding.py` 提供
  `recheck_dependencies(product, case, *, changed_refs, reason) -> dict`，由 A 判定
  **哪一条**答案受影响（并把受影响答案的 `assessment.status` 置 `stale`）。
- A 未交付时本函数如实返回 `{'status': 'unavailable', 'reason': ...}` 并**不写任何
  东西**。B 不复制一套答案判定规则，也不把所有已答问题一律重开。
- **纯时间触发不调用它**：时间到了没有可指认的记录变化，去提请核对等于换一种写法
  把所有答案重开一遍。
- 调用结果记入 `follow_up_runs.result_json.answer_recheck`，所以交接是否真的发生
  可以事后查证，而不是只写在文档里。
- 按 CONTRACT §3.6，B 侧 `case_view` 对 `assessment` **原样投影**：有就带、没有就
  不补默认值（§3.4 的"缺失即未核实"由消费方 C 呈现）。
