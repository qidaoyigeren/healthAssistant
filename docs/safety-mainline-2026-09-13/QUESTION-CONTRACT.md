# 问题有类型，行动由问题决定（2026-09-13）

本轮只做一件事：**让长期用药安全 Agent 能表达不同类型的信息缺口，并据此选择行动。**
没有新增产品功能，没有扩大模型预算，没有新评测脚本。

---

## 1. 旧设计如何把问题引向错误行动

旧调查契约只有**一种**问题：`_new_claim` 把每条子问题都建成 `evidence_missing`
缺口，含义永远是"去搜这条陈述的支持和反对证据"。于是：

| 位置 | 旧行为 | 后果 |
|---|---|---|
| `investigation.py` `_new_claim` | 每条子问题 → `evidence_missing` | "患者的开始时间是什么"也变成"去资料库搜证据" |
| `investigation.py` `sync_authority` | 目标里出现"剂量/单位/日期"且字段缺失 → **代码**生成 `patient_fact_missing` 追问 | "缺剂量就固定追问"的规则树，模型只能复述 |
| `investigation.py` `proposal_errors` | `ask_clarification` 的问句必须**逐字等于**程序写好的 description | 模型不能用自己的话问，也不能问程序没想到的事 |
| `_new_claim` 身份 | `claim_id = digest(entities)` | 同一药物的"剂量"与"开始时间"问题**共用一个 id** |
| `forced_stop` 完成条件 | 要求 `authority/interaction_evidence/applicability` 三查全绿 | 那是证据核查契约的完成条件，被套在"这件事查清了没有"上 |
| `accept_questions` | 子问题必须覆盖**整个权威药单** | 一件事的范围被强制扩成整个患者 |
| `finish` → `next_action()` | 收尾仍会规划一个默认 `rag_search` | 不同原因（预算/重复/等待/资料不可得）统一写成 `no_progress` |

`output/live-safety-ctx/live.json` 是这个后果的现场记录：模型 `plan_questions` 之后**只能反复
`rag_search`**，`questions_asked: []`，`termination_reason: no_progress`。

## 2. 新契约怎样保留问题类型与选择空间

**问题与结论分开。** 问题有类型，只有确实需要证据支持或反驳的那一类才建 claim：

| 类型 | 含义 | 能推进它的工具 |
|---|---|---|
| `user_fact` | 需要用户补充的事实 | `ask_clarification` |
| `material_read` | 需要读取材料核实 | `read_material_item` |
| `reference_lookup` | 需要检索权威资料 | `rag_search` / `read_evidence` / `ddi_check` |
| `source_conflict` | 两个来源之间的冲突 | 上面几种（收拢两侧） |
| `professional_judgment` | 需要专业人员判断 | **空** —— 没有工具能关它，它就该以未决进入人工复核 |

**身份。** `question_id = hash(类型, 对象, 目标字段)`，措辞、时间、序号都不参与：

- 同一药物的 `dose_unit` 与 `start_date` → 两条身份，可共存；
- 同一问题换说法 → 同一身份，不会不断生成新请求；
- 同一对象换类型 → 两条身份（"问用户"和"查资料"是两件事）。

**模型提出缺口，代码校验边界。** `plan_questions` 现在接受
`statement`（模型自己的话）/ `kind` / `subject_refs` / `target_field` / `why`。
校验的是：类型已知、对象在本作用域存在、目标字段格式、问句不含处方指令、修订不得丢掉
未决问题。**不再**要求逐字复述，**不再**要求覆盖整个药单。

**完成与停止。** `safety_case` 有自己的契约策略：范围是当前事项，完成条件是"没有阻塞性
未决问题且已检索证据都读回"，不是三查全绿。`forced_stop` 现在真的被快照循环调用——旧循环
从不问这个问题，于是"等用户"只能靠撞上 `no_progress` 收场。

**终止原因是分开的**：`waiting_input` / `waiting_review` / `evidence_unavailable` /
`budget_insufficient` / `no_progress` / `checks_completed`，收尾不再规划默认动作。

**程序读权威记录，不冒充、也不浪费一次模型决策。** `sync_authority` 拿的是完整快照，
`safety_case` 下据此记 `authority_read` 与 `authority_source='code_snapshot_validated'`。

**旧契约逐字不变**：`POLICIES` 默认仍是 `evidence_review`（覆盖整个药单、三查完成、
逐字问句），`test_question_contract.py` 最后一组用例专门守住这一点。

## 3. 本轮修掉的两个真缺陷

1. **恢复时带着上一轮的终止原因**。`run_open_review` 从持久状态恢复
   `termination_reason='waiting_input'`，新的一轮在 `while` 条件处**一次都不跑**就原样退出——
   用户补充了信息，系统什么都没发生。这正是"回答后恢复能利用新增信息"要防的事。
   现在：有**未消费的新信息**时清除 `waiting_input` / `no_progress` 这类"停止理由已不成立"
   的原因，并给模型一轮机会（`new_information_pending`）。
2. **"有内容"被当成"答到了"**。`apply_input` 现在按目标字段做**格式**校验（开始时间要是
   日期、剂量单位要有单位…），不符合的回答**不消除缺口**，只如实记下收到了什么、还差什么。

另有一条相关修正：`new_since_last_run` 改成**持久化游标**（`case_history_cursor`），
不再每次把最后几条历史重复标成"新增"。

## 4. 用户能看到什么变化

- 事项页上出现的问题，是**模型提出来的**（经 `plan_questions` → 执行器 → 校验器 → 采用），
  不是程序写好的句子，也不是预先塞进数据库的行；
- 每条问题带着它的**类型**与**为什么需要**；
- 回答之后，同一件事、同一条问题继续；答得不合格会明确说还差什么；
- "不知道"结束追问但不消除不确定性。

## 5. 验收

见 [QUESTION-CONTRACT-ACCEPTANCE.md](QUESTION-CONTRACT-ACCEPTANCE.md)。
