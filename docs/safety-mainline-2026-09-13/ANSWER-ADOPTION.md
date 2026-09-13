# 把取得的信息变成有依据的问题答案（2026-09-13）

本轮只做一件事：补上"**读到了，但无法采纳**"缺的那条生产路径。
不扩张规划框架、不增加问题分类、不批量跑模型。

---

## 1. 原来缺在哪条生产路径

核对到的事实（都在代码里确认过）：

| # | 说法 | 结论 |
|---|---|---|
| 1 | 读取成功只更新为 `received_unconfirmed` | **成立**（`investigation.py` `_record_question_attempt`） |
| 2 | 存在生产路径把读取内容转化为答案 | **不存在**——`INFO_AVAILABLE` 只由 `settle_question` 写 |
| 3 | `settle_question` 只有测试调用 | **成立**：全仓唯一调用方是 `test_question_contract.py` |
| 4 | claim 得到支持时 question 同步更新 | **不成立**：`_assess` 只关 `evidence_missing` 缺口，不碰 questions |
| 5 | 用户回答后 investigation 问题一致更新 | **不成立**：只写 SafetyCase |
| 6 | 已有有效答案仍被引导换来源 | **成立**：`received_unconfirmed` → `strategy_needs_change` |

所以缺的不是一个字段，而是**从"观察结果"到"某条问题的答案"之间的全部动作**：
读取 → 只有状态标记 → 立刻被建议换来源 → 模型重复规划。

## 2. 信息怎样变成可追溯的答案

新增唯一的采纳入口 `InvestigationState.answer_question(question_id, source, value, ...)`，
对模型暴露为 `answer_question` 工具（`allowed_tools` 在有未决问题时放行）。
采纳**不是**一个"把问题设成已回答"的开关，而是逐条核对：

| 来源 | 服务端核对什么 | 认识论属性 |
|---|---|---|
| `patient_record` | 值必须与**当前权威记录**里对应字段一致 | `authoritative_record`——**程序直接复用**，不需要模型重新批准已确认记录 |
| `evidence` | 引文必须是**本 run 回读过的**原文精确子串（搜到≠读过） | `reference_evidence` |
| `material` | 保留原文定位 | `material_record`（未经核实） |
| `user_answer` | — | `user_reported`（不是临床确认） |
| `professional` | — | `professional_opinion` |

拒绝的理由都是具体的：`record_missing` / `record_differs` / `quote_not_read_back` /
`no_quote` / `source_not_in_scope` / `unknown_question_id` / `empty_answer` /
`question_already_answered`。**对不上就保持未决**，并如实记一次"试过没结果"。

读取结果因此分成五类（`classify_reading`）：`answered` / `content_pending` /
`insufficient` / `conflict` / `professional`。
关键修正：`received_unconfirmed`（读到内容、还没分析）**不再等于** `strategy_needs_change`
（`revision_trigger` 只在 `insufficient` 时才建议换来源）。

## 3. 统一权威与同步

**investigation 是问题状态的权威**；SafetyCase 的 `required_inputs` 只是它的**投影**
（`CareTasks._sync_questions_to_case`），不另存一份 answered。三条同步：

- claim 支持到位 → 同一个 question 更新（`_sync_question_from_claim`，claim_id 由 question_id 派生）；
- 用户回答 → 写回 investigation 的那条问题（`_sync_answers_to_investigation`，性质 `user_reported`）；
- 问题拿到适用依据 → 事项上的请求关闭，已答部分与仍不确定的部分一并投影给页面。

事实确认仍走既有的受控写入路径（`semantic` 参数），**不经过**答案采纳——所以提交答案
不会绕开事实确认和必要安全检查。

## 4. 用户看到什么

详情页新增"**已经查清的部分**"：这条问题**已经拿到**什么、来源属性是什么
（当前权威记录 / 已回读原文 / 用户报告 / 材料记录）以及**仍不能判断**什么。
待补充的问题上同时显示"查到了什么程度"。

浏览器验收新增一条断言：回答之后，页面必须说明"这一答补上了哪一部分、来源属性与仍不确定的部分"，
而不是只显示"任务恢复成功"。

## 5. 验证与真实结果

见 [ANSWER-ADOPTION-ACCEPTANCE.md](ANSWER-ADOPTION-ACCEPTANCE.md)。
