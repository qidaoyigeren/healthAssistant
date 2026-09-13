# 答案采纳：验收结果

实现见 [ANSWER-ADOPTION.md](ANSWER-ADOPTION.md)。

---

## 1. 两条互补的产品路径（离线，走**真实执行循环**）

`stage0/test_safety_mainline_e2e.py::AgentInvestigationTests`

| 路径 | 用例 | 验证了什么 |
|---|---|---|
| 一：已有答案，不再追问 | `test_path_one_an_existing_answer_is_reused_and_the_user_is_not_asked` | 模型声明 `patient_record` 问题 → 调 `answer_question` 复用**当前权威记录**的字段 → 采纳，来源属性 `authoritative_record`，**没有**产生给用户的补问 |
| 二：确实缺信息，补问后继续 | `test_path_two_a_real_gap_is_asked_and_the_answer_reaches_the_question` | 补问 → 用户提交 → 同一 question_id 上出现回答，属性 `user_reported`，且问题**没有**变成"已有依据" |

两条都从**运行轨迹里的工具结果**读，不直接调用 `settle_question`、不直接改数据库。

## 2. 契约级边界（`stage0/test_question_contract.py::AnswerAdoptionTests`）

| 边界 | 用例 |
|---|---|
| 非空但无关的工具结果不能回答问题 | `test_an_unrelated_non_empty_tool_result_cannot_answer_a_question` |
| 来源存在但不支持答案时不能采纳 | `test_a_source_that_exists_but_does_not_support_the_answer_is_refused` |
| 引用不存在/越权不能当来源 | 同上（`source_not_in_scope`）+ `test_a_reference_must_really_exist_in_scope` |
| 证据类答案必须有回读过的原文片段 | `test_an_evidence_answer_needs_a_quote_from_what_was_actually_read` |
| 用户报告带自己的来源属性 | `test_a_user_report_is_recorded_with_its_own_provenance` |
| 部分回答保留已知部分并写明剩余缺口 | `test_a_partial_answer_keeps_the_question_open_and_says_what_is_missing` |
| 重复提交不重复采纳 | `test_repeating_the_same_answer_does_not_adopt_twice` |
| 旧版本答案不能批准新状态 | 沿用上一轮 `retire_stale_answers` 的全部用例 |
| 不相关问题不共用答案 | 身份 =（信息目标×对象×字段），`test_the_two_targets_stay_two_questions` |
| 用户事实已确认 ≠ 风险判断成立 | 答案采纳只改 question；关闭条件仍由 `closure_evidence` 决定（上一轮全部用例） |
| 答案采纳与事项关闭独立 | 路径一采纳后事项仍是 `open`，`model_closed_the_case` 为假 |
| 无新信息时不重复检索和提问 | `test_the_same_plan_cannot_be_replayed_without_a_reason` |
| 读到内容不等于必须换来源 | `test_a_record_read_that_did_not_answer_is_not_evidence_unavailable` |

## 3. 冻结核对

```
python scripts/verify-agent-closeout.py --out output/closeout-answer-adoption
→ status pass | source_unchanged True | 780 tests / 50 suites / 0 failed
→ evals: 四项全部按预期（含负对照必须失败）
→ mainline: 39 tests，六个类别全部通过
```

浏览器：**12/12 步**（新增一条——回答之后页面必须说明"这一答补上了哪一部分、
来源属性是什么、还剩什么不确定"，而不是只显示"任务恢复成功"）。

## 4. 真实模型（一次，预先限定）

声明并在开跑前打印：**1 个任务**、最多两轮、`--max-calls 5`、`--wall-seconds 150`、
`--max-cycles 5`；隔离合成库 + 隔离合成资料。产物：`output/live-answer-adoption/live.json`。

**原始真实调用结果（修复前的代码状态与此相同——本轮没有为跑通而改模型侧行为）：**

| 项 | 值 |
|---|---|
| 模型声明的问题 | 1 条：`general_reference` / `general_reference` |
| 实际动作 | `rag_search` × 2，两次提案完全相同 |
| **采纳动作** | **0 次**（`adoptions: []`） |
| 该问题的信息状态 | `attempted_no_result` |
| 终止原因 | `no_progress`（`degraded_reason: no_progress:repeated_proposal`） |
| provider 调用 / token / 被拒 | 3 / 23,446 / 0 |
| 墙钟 | 11.0 秒 |
| 模型是否自行关闭事项 | 否 |

**离线另行确认**：`answer_question` 当时**确实在模型可见的工具清单里**
（`['answer_question', 'ddi_check', 'memory_read', 'memory_write', 'rag_catalog', 'rag_search']`），
所以这次没有采纳是**模型没有选择它**，不是工具没给。

读法：模型检索了两次、没有回读原文、也没有把任何信息落成答案，
**没有产生有效答案，也没有明确缩小缺口**。

> **契约与生产路径已补齐并通过离线验证；本轮 Agent 用户价值尚未通过验收。**

## 5. 区分：原始调用结果 vs 修复后的验证结果

- **原始真实调用结果**：上表。没有采纳动作，没有答案。
- **修复后（离线）的验证结果**：路径一/路径二与全部边界都由脚本化规划器跑通，
  证明的是**工程机制**，不计入模型自主成功。

两者不混写：本轮的代码修复不是从这次失败里"猜"出来的——采纳路径此前**根本不存在**
（`settle_question` 无生产调用方），补它是补缺失的生产路径，不是为这次失败调参。

## 6. 未通过项

1. 真实模型仍未把取得的信息变成答案（本次 0 次采纳）。
2. 上一轮遗留、本轮未改：专业复核路径因无真实医护服务而一律拒绝；
   材料核对交付恒为 `partial`；无后台 worker 时队列不自动推进。
