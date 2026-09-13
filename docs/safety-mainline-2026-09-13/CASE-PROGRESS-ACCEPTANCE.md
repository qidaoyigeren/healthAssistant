# 事项持续推进：验收结果

实现与设计见 [CASE-PROGRESS.md](CASE-PROGRESS.md)。

---

## 1. 边界验证（本轮新增的契约用例）

命令：`python -m unittest stage0.test_safety_cases stage0.test_safety_checks stage0.test_safety_mainline_e2e`

| 要求验证的边界 | 用例 | 结果 |
|---|---|---|
| current 的风险警告**不能**单独支持关闭 | `test_a_live_risk_cannot_close_the_case_however_current_it_is` | 通过（409，事项保持 `open`） |
| 触发条件已被消除时**可以**关闭，且依据可追溯 | `test_an_eliminated_trigger_closes_the_case_with_recorded_evidence` | 通过 |
| 仍有未决问题时即使触发条件消除也不能关 | `test_an_open_question_blocks_closing_even_with_an_eliminated_trigger` | 通过 |
| 不相关的专业复核决定不能用于关闭 | `test_an_applied_decision_for_another_case_cannot_close_this_one` | 通过 |
| 动作语义不允许完成的复核不能关闭 | `test_a_review_action_that_does_not_permit_closure_cannot_close` | 通过 |
| 模拟工作台的决定不得包装成专业确认 | `test_a_simulated_review_cannot_be_dressed_up_as_professional_confirmation` | 通过 |
| 决定作出后事实移动 → 不再适用 | `test_a_review_whose_facts_moved_cannot_approve_the_new_state` | 通过 |
| 操作者必须有权限 | `test_closing_requires_the_actor_to_have_the_role` | 通过（403） |
| 身份来自认证上下文，不读请求体 | `test_the_recorded_actor_is_the_authenticated_principal`、`test_the_request_body_cannot_claim_a_reviewer_identity` | 通过 |
| 空回答不能冒充缺口已解决 | `test_an_empty_answer_closes_nothing`、`test_an_empty_answer_over_the_api_closes_nothing` | 通过 |
| 「不知道」结束追问但不消除不确定性 | `test_saying_i_do_not_know_stops_asking_but_keeps_the_uncertainty`、`test_an_unknown_answer_does_not_unblock_closing`、`test_saying_i_do_not_know_over_the_api_routes_to_alternative_evidence` | 通过 |
| 用户回答不能替代专业依据 | `test_a_user_relayed_doctor_opinion_is_not_a_closing_basis` | 通过（409） |
| 旧答案失效后能够重新核对，且不生成新问题 | `test_an_answer_stops_applying_when_the_facts_move_and_the_question_reopens`、`test_re_answering_after_a_fact_change_keeps_the_same_request_id` | 通过 |
| 重复提交/恢复不重复推进、不覆盖新状态 | `test_two_investigate_requests_do_not_start_two_investigations`、`test_the_check_runs_once_per_world_state_and_not_per_event`、`test_re_asking_the_same_gap_does_not_add_a_second_request` | 通过 |
| 持续跟进 ≠ 等待专业人员，也不表示风险消失 | `test_monitoring_is_its_own_state_not_a_wait_for_a_professional`、`test_a_routine_sync_does_not_erase_a_monitoring_arrangement` | 通过 |
| 模型不能自己编一个复查周期 | `test_a_model_cannot_invent_a_monitoring_cycle` | 通过 |

**另外三处本轮修掉的真缺陷**（都由新用例锁住）：

- 回到曾经检查过的世界状态时，必要检查被去重吞掉 → `test_returning_to_a_previously_checked_state_is_checked_again`；
- 检测器返回列表时被静默当成"无检出" → `test_a_bare_list_detector_is_understood_not_silently_empty`（上一轮修，本轮回归）；
- 事实被撤回后无法重新上报（版本号撞车）→ 由重新核对路径用例覆盖。

## 2. 连贯产品场景

`test_safety_mainline_e2e.py` 覆盖八步：用药变化 → 必要检查建事项 → Agent 拿到事项上下文
发现缺口 → 提出可回答的问题 → 用户提交回答 → 重启后同一事项/问题/回答 → Agent 用新信息
选择不同下一步 → 后续事实变化使受影响部分重新进入复核。

**浏览器验收**（真实 Chromium 走真实页面，后端是隔离合成库、不调模型）：

```powershell
node scripts/safety-mainline-browser-acceptance.js --out output/browser-safety
```

结果 **10/10 步通过**，包括：页面显示"为何出现这件事"、"已经查到了什么"、需要回答的问题
及其**为什么需要**；提交回答后问题不再要求回答，并且页面自己说出了变化：

> 状态变化：等待您补充 → 待调查（已具备依据，可以进行调查或复核）
> 收到补充，并关闭了对应的那条问题。

截图与 `acceptance.json` 在 `output/browser-safety/`。

## 2.5 离线全量与前端

冻结树上的权威结果（`source_unchanged: True`）：

```
python scripts/verify-agent-closeout.py --out output/closeout-case-final
→ status pass | 743 tests / 49 suites / 0 failed
→ evals: baseline-replay ✓  gap-replay ✓  gap-tools ✓  gap-replay-negative ✓（负对照按预期失败）
→ mainline: 31 tests，六个类别全部通过
   necessary_checks_completed 4 · cases_created_and_updated 3 · investigation_made_progress 7
   waiting_and_disposition_states_correct 15 · model_and_program_separated 3
   requests_failures_and_cost_traceable 4
```

前端：`npx tsc -b --noEmit` 与 `npm run build` 均 exit 0。

## 3. 真实模型（有限验收，如实报告）

预先限定：**1 个任务**、`--max-calls 6`、`--wall-seconds 180`、`--max-cycles 6`。
产物：`output/live-safety-ctx/live.json`。

| 项 | 值 |
|---|---|
| 事项由代码建立（**未花模型调用**） | 是 |
| 规划来源 | `llm`（真实模型，不是脚本规划器） |
| 事项上下文是否送达执行循环 | **是**（`case_context_supplied: true`，2 条相关用药） |
| provider 调用 / token / 被拒 | 6 / 42,121 / 0 |
| 墙钟 | 23.7 秒 |
| 终止原因 | `no_progress` |
| 事项最终状态 | `execution_failed`（**未解决**） |
| 提出的补问 | 0 条 |
| 模型是否自行关闭事项 | **否** |
| 关闭依据核对 | `ok=false`，理由"关联结论仍显示风险存在，不能关闭" |
| 决策里引用了 `basis_refs` 的条数 | **0 / 6** |

**读法（不美化）**：真实模型这次**没有产出有效进展**——没有提出可用的问题，调查被
`no_progress` 有界停止，事项诚实地停在"未解决"。它确实用上了新加的结构化字段
（每步都填了 `expected_observation` 与 `expected_change`），但**没有引用任何依据**
（`basis_refs` 全空）——所以"下一步有依据"这一条，模型这次没有做到。

同时注意：调用数正好等于声明的上限（6），所以这次终止**也可能是预算先到**，而不是
模型原地打转；单次运行分不清这两者，不作更强的结论。

**不声称**：n=1、单次、一个合成事项。它与既往轮次一致：*开放式的模型自主调查在真实
批次上尚未被验收。* 本轮因此不把任何安全保证建立在模型之上。

## 4. 哪些效果来自程序，哪些来自模型

| 效果 | 来源 |
|---|---|
| 事项建立、身份去重、用药分期 | **程序**（确定性） |
| 必要检查及其实施、去重、失败可见 | **程序** |
| 触发条件是否已消除 | **程序**（依赖索引 × 当前权威记录） |
| 关闭是否成立、依据是否适用、谁有权 | **程序**（`closure_evidence` + `_build_basis`） |
| 状态迁移与历史（含"为什么"） | **程序** |
| 补问身份稳定、回答只关它命中的那条 | **程序** |
| 识别最关键的未决信息、选择读什么/问什么 | **模型**（不可用时如实标注未推进） |
| 提出面向用户的问题措辞与理由 | **模型**（不可用时事项停在待调查/等待补充） |
| 用户回答是否**真的回答了**那个问题 | 模型可提示，**不由模型定论**；空值/不知道由程序分类 |
| 风险在临床上是否成立、是否调整用药 | **必须由用户或专业人员确认** |

## 5. 仍有的产品边界

1. **没有连接真实医护服务**：`professional_review_applied` 这条关闭路径在结构上可用，
   但当前所有复核记录都来自本地模拟工作台，因此**一律被拒绝**（并说明原因）。这不是
   缺陷，是不把模拟数据包装成专业确认的代价。
2. **开放式调查的模型产出仍为零**（本次 0 条补问、0 条依据引用）。不阻断主线：检查、
   事项与处置依据全部由代码保证。
3. **长期跟进仍需要后台 worker**：`necessary_checks`、`dependency_tasks`、`outbox_tasks`
   在没人跑服务时不会自动推进；`monitoring` 的跟进安排也只在应用内待办中体现，不发送
   外部通知。
