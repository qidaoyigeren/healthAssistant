# 问题契约：验收结果

实现见 [QUESTION-CONTRACT.md](QUESTION-CONTRACT.md)。

---

## 1. 契约级验证（离线，复用现有入口）

`python -m unittest stage0.test_question_contract`（新增，28 例）覆盖本轮要求的九条：

| 要求 | 用例 | 结果 |
|---|---|---|
| 同一药物的两个不同问题不会被合并 | `test_two_different_questions_about_one_drug_coexist` | 通过 |
| 模型提出的用户事实缺口进入真实补问路径 | `test_a_model_proposed_user_fact_question_enters_the_real_ask_path` | 通过 |
| 问句改写不影响身份、不受原句相等限制 | `test_reworded_question_keeps_its_identity`、`test_the_model_may_ask_in_its_own_words`（E2E） | 通过 |
| 当前事项不被强制扩展成整个药单调查 | `test_the_case_is_not_forced_to_cover_the_whole_medication_list` | 通过 |
| 相同目标在不同已知信息下可合法选择不同第一步 | `test_the_same_goal_takes_a_different_first_step_depending_on_the_question` | 通过 |
| 回答后恢复能利用新增信息、不重复问同一个问题 | `test_answering_resumes_the_same_question_and_uses_the_new_information`（E2E） | 通过 |
| 非空但不满足字段要求的回答不能消除缺口 | `test_a_non_empty_answer_that_misses_the_field_does_not_close_the_gap`（E2E） | 通过 |
| 预算耗尽与无进展有不同且准确的终止原因 | `test_waiting_is_not_reported_as_making_no_progress`、`test_unavailable_evidence_is_not_reported_as_making_no_progress`、`test_an_exhausted_budget_is_reported_as_such` | 通过 |
| 上一轮的关闭条件、权限和必要安全检查没有被削弱 | 上一轮全部用例 + `test_legacy_contract_is_unchanged` 的四个旧契约用例 | 通过 |

另外：`test_only_evidence_questions_create_claims`（不是每条问题都变成证据检索）、
`test_a_professional_question_has_no_closing_tool_and_waits_for_review`、
`test_the_run_does_not_finish_before_the_model_can_ask`（不在模型开口前就判"没什么要问的"）、
`test_the_wrap_up_does_not_invent_a_default_plan`。

**冻结核对**：`verify-agent-closeout` 在冻结树上 `status pass`、`source_unchanged True`、
**771 测试 / 50 套件 / 0 失败**，四项冻结评测按预期（含负对照必须失败）；主线验收
单独跑 **35 用例、6/6 类别**通过（两者相加 806 次测试执行）。

## 2. 浏览器（真实页面走通产品闭环）

```powershell
node scripts/safety-mainline-browser-acceptance.js --out output/browser-safety
```

**10/10 步通过**。这一轮的关键改动：夹具里那条问题**不再预先塞进数据库**，而是由一个
规划器通过 `plan_questions` 提案、经执行器与校验器被采用、再落成页面上的补问——只有措辞
是脚本化的。页面按顺序显示"为何出现、已查到什么、仍不确定什么、现在需要我做什么、
补充之后发生了什么变化"。

## 3. 真实模型（一次，预先限定）

预先声明并在开跑前打印：**1 个任务**、最多两轮（首轮 + 回答后恢复）、
`--max-calls 4`、`--wall-seconds 120`、`--max-cycles 4`。隔离合成库 + **隔离合成资料**
（合成药不可能出现在真实药品库里，所以自带语料）。产物：`output/live-question-contract/live.json`。

| 项 | 值 |
|---|---|
| 事项由代码建立（未花模型调用） | 是 |
| 规划来源 | `llm`（真实模型） |
| 事项上下文送达 | 是 |
| provider 调用 / token / 被拒 | **2 / 17,083 / 0** |
| 墙钟 | 14.8 秒 |
| 终止原因 | `evidence_unavailable` |
| 模型是否自行关闭事项 | 否 |
| 关闭依据核对 | `ok=false`（"关联结论仍显示风险存在"） |

**模型提出了什么**（三條，均为它自己的话）：

| 类型（模型自选） | 目标字段 | 问句 | 为什么 |
|---|---|---|---|
| `reference_lookup` | `dose_unit` | 合成药甲和合成药乙的剂量单位是什么？ | 剂量单位影响相互作用判断的准确性 |
| `reference_lookup` | `route` | ……给药途径是什么？ | 给药途径影响判断的准确性 |
| `reference_lookup` | `schedule` | ……给药频率是什么？ | 给药频率影响判断的准确性 |

**回答之后执行了什么不同的行动：没有。** 因为模型把三条**患者自己的事实**问题标成了
`reference_lookup`（"去查资料"），而这三条只能在隔离语料里检索——语料答不了"这位患者实际
怎么吃"，于是本轮以 `evidence_unavailable` 收尾，没有产生用户可回答的补问，第二轮未发生。

契约是对的：如果它标成 `user_fact`，这条路径会立刻变成可回答的补问、并在回答后恢复
（脚本化验收里已经证明）。**模型没有做出那个选择。**

> **契约已修复，但真实 Agent 能力尚未通过验收。**

## 4. 哪些效果来自程序，哪些来自模型

| 效果 | 来源 |
|---|---|
| 事项建立、必要检查、触发条件判定、关闭条件、权限、状态迁移 | **程序** |
| 问题身份（类型×对象×字段）、修订不得丢未决项、问句安全校验 | **程序** |
| 权威记录读取与校验 | **程序**（`authority_source='code_snapshot_validated'`） |
| "还缺什么、该问谁、为什么" | **模型**（类型选错时程序照实接收，不替它改） |
| 问句措辞 | **模型** |

## 5. 未通过项

1. **真实模型把患者事实问题标成资料检索**（本次 3/3），因此没有触发补问路径。
   这是本轮最关键的未通过项，也是"契约已修复、能力未验收"的具体含义。
2. **`basis_refs` 仍为空**（本次 2 步决策，0 步引用依据）。
3. 上一轮遗留、本轮未改：专业复核路径因无真实医护服务而一律拒绝；
   材料核对交付恒为 `partial`；无后台 worker 时队列不自动推进。
