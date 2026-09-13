# 计划采纳—行动执行—反馈修订：验收结果

实现与本轮更正经 [QUESTION-CONTRACT.md](QUESTION-CONTRACT.md)、
[QUESTION-CONTRACT-CORRECTION.md](QUESTION-CONTRACT-CORRECTION.md)。

---

## 1. 离线契约验证（本轮要求的九条，逐条对应用例）

`python -m unittest stage0.test_question_contract`（26 例）

| 要求 | 用例 |
|---|---|
| 一次计划采纳后，首次规划缺口关闭 | `test_one_adoption_is_a_complete_state_transition` |
| 无新信息时不能重复消费首次规划 | `test_the_same_plan_cannot_be_replayed_without_a_reason` |
| 模型收到真实采纳结果和可用问题 ID | `test_the_model_sees_the_real_adoption_outcome_and_ids`、`test_the_tool_result_shows_the_real_adoption_not_a_blanket_success`（走真实循环） |
| 拒绝的计划不会在工具历史中显示为成功采纳 | `test_a_rejected_plan_is_not_reported_as_adopted`（走真实循环） |
| 同一问题从查材料转向问用户，ID 和历史保持 | `test_changing_the_source_keeps_the_same_question_and_its_history` |
| 错误来源选择可以修正，不触发"删除未决问题" | 同上 + `test_a_source_change_is_a_legal_revision_trigger` |
| 没有搜索时不会得到"搜索后资料不可得"的结论 | `test_no_search_means_no_evidence_unavailable_verdict` |
| 暂时无法取得资料的问题不会冒充已回答 | `test_an_unavailable_source_is_not_an_answer` |
| 关闭条件、权限、作用域和必要安全检查保持有效 | 上一轮全部用例 + `LegacyContractUnchangedTests`（旧契约逐字不变） |

外加：来源能力匹配（`test_general_reference_cannot_prove_what_this_patient_actually_takes`）、
引用必须**真实存在且在作用域内**（`test_a_reference_must_really_exist_in_scope`，
不再按 `memory:` 前缀放行）。

**冻结核对**：`verify-agent-closeout` → `status pass`、`source_unchanged True`、
**769 测试 / 50 套件 / 0 失败**、四项评测按预期（含负对照必须失败）；
主线验收单独跑 **37 用例**。

## 2. 浏览器（真实页面）

```powershell
node scripts/safety-mainline-browser-acceptance.js --out output/browser-safety
```

**11/11 步**，含新增的一条：页面必须说明"这条问题要弄清哪一类信息、这一次从哪里取"。
问题依旧由 `plan_questions` 经执行器与校验器产出，不是预先塞库。

## 3. 真实模型（一次，预先限定）

声明并在开跑前打印：**1 个任务**、最多两轮、`--max-calls 4`、`--wall-seconds 120`、
`--max-cycles 4`；隔离合成库 + 隔离合成资料。产物：`output/live-plan-adoption/live.json`。

| 项 | 值 |
|---|---|
| 事项由代码建立（未花模型调用） | 是 |
| 计划采纳 | **成功**：模型声明 1 条问题，`patient_actual_state` / `patient_record` |
| 采纳后是否进入**实际行动** | **是**：`memory_read` 挂在 `q:7319a902a970` 上 |
| 该问题的信息状态 | `received_unconfirmed`（读到了记录，答案未确认） |
| 之后的选择 | 又提了两次 `plan_questions`，`gap_id` 指向**那条问题**而不是 `subquestions` → 被拒 |
| 终止原因 | `no_progress`（`degraded_reason: no_progress:repeated_proposal`） |
| provider 调用 / token / 被拒 | 4 / 28,096 / 0 |
| 墙钟 | 16.8 秒 |
| 模型是否自行关闭事项 | 否；关闭依据核对 `ok=false` |

**读法**：环路的**前三步通了**——计划被采纳、缺口关闭、模型随后做了实际动作，
且这次尝试被如实记在**那条问题上**（`received_unconfirmed`）。第四步没通：
读到记录没答上来之后，模型**重复规划**而不是把来源换成 `ask_user`
（`revision_trigger()` 当时正是 `strategy_needs_change`，换来源会被接受）。

失败位置保留在 `runs[].decisions`（`plan_questions` / `memory_read` /
`plan_questions` / `plan_questions`）与 `rejections`（本轮为空——两次重复提案被
`progress` 守卫拦下，未走校验器）。**没有换模型、没有扩预算、没有追加批次。**

## 4. 真实批次暴露并修掉的两个工程缺陷

| 缺陷 | 证据 | 修法 |
|---|---|---|
| 成功读到记录、却报 `evidence_unavailable` | 第一轮真跑的 `termination_reason` 是 `evidence_unavailable`，而问题是 `patient_record` 且读**成功** | `source_exhausted()` 成为唯一判据：只有"尝试过且没拿到"才叫资料不可得；读到了内容不算 |
| 同一误判也出现在 `_refine_typed_reason` | 收尾路径把 `no_progress` 再标一次 | 同上，一个规则一处实现 |

## 5. 哪些只通过了离线验证

- **换来源**：`strategy_needs_change` → 换成 `ask_user` → 同一 question_id →
  用户回答 → 恢复，全部由脚本化规划器验证（`test_changing_the_source_...`）。
  真实模型这一次**没有走通**这一步。
- 关闭条件、权限、作用域、必要安全检查：上一轮的用例全部保留并仍然通过。
