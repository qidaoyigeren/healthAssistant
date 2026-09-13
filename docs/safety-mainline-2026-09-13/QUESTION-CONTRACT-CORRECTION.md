# 更正说明：上一轮验收报告的结论过强

**原始记录未被覆盖。** 本文只补充说明，不修改
[QUESTION-CONTRACT-ACCEPTANCE.md](QUESTION-CONTRACT-ACCEPTANCE.md) 与
`output/live-question-contract/live.json`（那是本轮的原始记录，
`output/live-plan-adoption/live.json` 是本轮的对应产物）。

---

## 被更正的说法

上一轮验收报告写了：

> **契约已修复**，但真实 Agent 能力尚未通过验收。

第一半**下早了**。它当时成立的范围只有"问题能不能带类型地声明出来"；
把它读成"计划采纳这条链路已经修好"是不对的。本轮在代码里核到七处断点，
其中三处直接推翻了这句话。

## 更正依据（本轮核对到的实际断点）

| 断点 | 位置 | 事实 |
|---|---|---|
| 采纳后首次规划缺口**从不关闭** | `adopt_questions` 只加 `question_open` 缺口 | `revision_trigger()` 永远返回 `'first_plan'`，`allowed_tools` 一直放着 `plan_questions` |
| 工具列表因此**持续邀请**重复规划 | `allowed_tools` 见 `revision_trigger() is not None` 即放行 | 上一轮 `live.json` 的轨迹正是"同一提案连着提" |
| handler **在校验之前**返回 `accepted: True` | `_plan_questions_handler` 纯回显 | 采纳/拒绝发生在 `observe`，模型看不到真实结果 |
| 工具结果不含真实 question_id 与拒绝原因 | 同上 | 模型想知道 ID 只能再调一次 `plan_questions` |
| 换来源 = 换问题 | `question_id_for` 把 `kind` 哈希进身份 | 改来源被判成"删掉未决问题" |
| **没有检索**却报"检索后资料不可得" | `_refine_typed_reason` 把 `no_progress` 改写成 `evidence_unavailable` | 上一轮 `live.json` 是 `cycles: 1`、检索 0 次 |
| "资料不可得"被当成**已回答** | `is_question_answered` 含 `QUESTION_STATUS_UNAVAILABLE` | 完成条件与修订判断一起误判 |

## 更正后的读法

上一轮那句结论应当读成：

> **问题可以被带类型地声明出来，但"计划采纳 → 行动执行 → 反馈修订"这条环路当时并未接通。**
> 真实 Agent 能力尚未通过验收。

两句话的共同部分（模型能力未验收）没有变化，所以上一轮的失败证据仍然成立、
仍然有效，只是**归因**被更正了：那一轮的部分失败来自**工程缺陷**，
不能全部记在模型头上。

## 本轮区分三类证据的做法

按本轮要求，工程缺陷、来源能力限制与模型决策错误分别给证据，不再合并成
"剩下全是模型问题"：

- **工程缺陷**：上表七条，每条都有对应代码位置与本轮新写的用例；
- **来源能力限制**：`STRATEGY_CAPABILITY` 明确写死"一般药品资料不能回答
  `patient_actual_state`"，选错时给出具体反馈；
- **模型决策错误**：真实批次里模型把患者事实标成 `reference_lookup`
  （上一轮），以及本轮读到了记录却没答上来时**重复规划**而不是换来源
  ——两次都保留了具体的失败位置（`output/*/live.json` 的 `runs[].decisions`
  与 `rejections`）。
