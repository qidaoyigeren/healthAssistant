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
