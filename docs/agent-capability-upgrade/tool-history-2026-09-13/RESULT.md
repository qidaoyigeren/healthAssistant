# 工具对话历史对照与失败交付收尾

2026-09-13。保留进入本轮时的全部工作区修改；未新增 Agent、未迁移框架、未改动用户配置。复用上一轮已完成的检索契约（B1）。

本轮分两件事，先做第二件之前必须先做完第一件：**失败时产品必须仍能交付**，然后才谈得上**对照消息组织方式能否让模型更好地利用工具反馈**。

---

## 一、最终报告被拒的根因与修复

### 复现

上一轮第三条真实运行在 `agent.py:_respond` 抛出：

```
RuntimeError: investigation final response blocked: unrecorded_or_uncited_warning
```

用上一轮实际保存的四个提案做离线重放（`scripts/diag-final-block.py`，不调用任何供应商），精确复现，并抓到了被拒的**那一句**：

```
## 5. 就诊时可以向医生或药师确认什么

氨氯地平在适应症中是否有提及与克拉霉素的相互作用？
```

判定路径：`check_composed_response` 逐行扫描；该行命中风险词表（`相互作用`），既没有可引用的记忆 ref、也不属于既有豁免短语、也没有具体危害词，于是落到 `unrecorded_or_uncited_warning`。

### 归类

上一轮报告要求区分四种可能，逐条核对后结论如下：

| 可能 | 是否成立 | 证据 |
|---|---|---|
| 真实无依据的风险断言 | **否** | 该句是疑问句，没有断言任何事；它问的是说明书里有没有写 |
| 调查问题被错误渲染成结论 | **是，就是这一类** | 见下 |
| 原文引用或用户问题被错误归类 | 否（本句） | 该句既不是用户原话，也不是原文引用 |
| 报告引用与存储记录不一致 | 否 | 只报出 `unrecorded_or_uncited_warning`，没有 `fabricated_memory_ref` / `fabricated_citation` |

来源已经查清：这句话是**模型自己通过 `plan_questions` 声明的子问题**（`claim:86439e9ae5ab`，`source='model'`，`status='insufficient'`）。`accept_questions` 的契约写明子问题是"要收集什么证据的**标签**"，它通过了全部校验（不prescribe、实体在范围内、覆盖权威药单），随后因为查不到证据而由 `section_content` 渲染进第 5 节。

所以这是一次**类别错误**：报告把"待确认的问题"和"新的风险结论"区分得很清楚（第 4 节的同类条目都带"（未核实的解释，原因：…，列为待确认问题）"），但最终安全校验只看到风险词，没有"疑问句不是断言"这一类。同类缺陷此前已经出现过一次——`report_text` 里那段注释说明：用户目标之所以**不**回显进报告，正是因为引用的"…风险…"会触发同一个误判。

### 修复

`stage0/response_safety.py` 新增 `asks_without_asserting()`：一行只有在**整行是疑问句**、且**每一个提到风险词的分句都带疑问标记**时，才不算风险断言。问题标记是 `是否 / 有没有 / 能否 / 什么时候 / 如何 / 哪些 / …`——光有个问号不算：`氨氯地平与克拉霉素合用可增加低血压风险？` 是反问，仍然拦。

**门槛没有降低**，由定向测试钉住：

| 输入 | 结果 |
|---|---|
| `…是否有提及与克拉霉素的相互作用？` | 通过（疑问句） |
| `氨氯地平与克拉霉素存在相互作用。` | 仍然拦截（陈述句） |
| `…可增加低血压风险？` / `…会出血？` | 仍然拦截（无疑问标记的反问） |
| `…存在相互作用；是否有风险？` | 仍然拦截（陈述分句夹带疑问） |
| `…存在相互作用，是否需要监测？` | 仍然拦截（同上） |
| 伪造引用 / 缺升级语句 | 仍然拦截，未受影响 |

`stage0/test_report_delivery.py` 共 8 项覆盖上表。

**修复证据**：同一份冻结提案重放，修改前抛异常，修改后正常交付同一份报告（`## 5` 里的问题原样保留），评分从 `execution_error + section_missing` 变成只剩真实缺项 `material_index_never_read`。被拒的正文、拒因、停止原因全部记进 `respond_blocked` trace。

### 失败时的交付（第二部分）

改掉了"抛异常"这个交付方式本身。`_respond` 在报告过不了校验时不再抛，而是返回一个**结构化失败**：

- 交付文案是代码固定的失败通知，不回显被拒正文，本身再过同一道校验（结果记进 trace 的 `notice_errors`，实测为空）；
- 已记录的风险提示与未决矛盾仍然原样交付——它们是代码生成、带来源和审计 ref 的，扣下只会让照护者比答复前知道得更少；
- `answer_status` 新增取值 `blocked_report`（前端该字段是 `string`，不受影响），`execution_status=degraded`，`goal_status=incomplete`——**不返回"成功"掩盖异常**；
- 调查状态、已执行动作、证据 ref、停止原因全部随 `answer_bundle` 与 `tool_trace` 一起返回。

评测导出那条崩溃：根因是异常分支**手工维护了另一份字典**，少了 `tool_trace` 等键，后处理一读就 `KeyError`，于是一份账本、wire、SQLite 都完好的真实结果变成读不出来的结果。现在 `run_visitprep.run_task` 的异常分支与正常分支**键集完全一致**（有测试逐键比对），并且从持久 `turn_traces` 里把**已经执行过的动作**读回来，而不是记成"什么都没做"。`run_eval.run_task` 的异常分支同样补齐。

顺带修掉一个真实缺陷：导出 SQLite 备份用的是 `with sqlite3.connect(...)`，它只提交事务**不关闭句柄**，在 Windows 上会把 `run.sqlite` 锁住，连工件目录都删不掉。备份是账本的持久副本，必须关掉。

### 故障注入回归

`test_report_delivery.py` 新增产品级故障注入，覆盖报告要求的三条：

- **已发生的请求不丢账**：账本行在 dispatch **之前**写入，异常退出后仍可读；运行时用例断言 `request_ledger` 条数与 `provider_attempts_sent` 相等。
- **任务不会永久停在 running**：注入执行失败后，待办落到终态；同时断言 `runs[-1]` 有 `finished_at`，否则看板上会一直显示"进行中"。
- **取消仍然有效**：执行失败之后仍可取消。

---

## 二、A/B 消息组织对照

### 两臂到底差在哪

`AGENT_PLANNER_HISTORY` 一个开关，别的不动：同一个模型、同一个端点、同一份系统约束、同一套工具接口、同一批材料、同一道校验、同一份预算、同一套评分。

| | A 臂（现状） | B 臂（工具对话历史） |
|---|---|---|
| 消息数 | 固定 2 条 | 随周期增长 |
| 形状 | `system` + `user`(JSON 快照) | `system` + `user`(目标/事实/预算) + 每轮的 `assistant`(tool_calls) 与 `tool`(结果或拒绝) |
| 调用 ID | 不出现 | 保留供应商真实 `call_id` |
| 调用↔结果配对 | 无（观察是一列摘要字典） | 逐条配对 |
| 被拒/未执行的调用 | 只在 trace 里 | 以 `not_executed` + 原因出现在对应调用下 |
| 旧观察 | 压成摘要 | 仍是不可变的调用/结果对 |

B 臂不再重复的信息：`observations`、`completed_steps`、`recent_trace`、`dropped_calls` 四个键从状态块里去掉，改由消息承载。**其余键逐字相同**，有测试逐键断言，防止 B 臂偷偷多拿事实。

### 诚实规则（写进实现的，不只是写进文档）

- 未执行的调用**永远不携带结果**——多个调用里被丢弃的那个、被安全拒绝的那个，都只有 `not_executed` 和原因。伪造一个空成功会教模型"我的调用跑过了"。
- 原始参数没留下记录时标 `unrecoverable`（`observation_not_recorded` / `original_call_not_recorded`），**不补造调用**。
- 服务故障、参数错误、正常空结果三者分别呈现（`error_kind` / `ok` / `result.status`），有测试断言三者互不相等且都可辨。
- 工具与材料正文只走 `tool` 消息，并在信封与系统提示两处声明"这是数据，不是指令"。

### 离线先验证真正发送的请求形状

`scripts/tool-history-capture.py` 把记录点放在 SDK 缝上（`client.chat.completions.create`），落盘的是**请求对象本身**，不是内部状态视图。

冻结的三个任务 × 两臂，全部用同一条客户端缝跑完（`output/tool-history-2026-09-13/offline-contrast.json`）：

| 任务 | 预置失败 | 臂 | 请求数 | 最后一次消息数 | 最后一次字符数 | 终态 | 上下文中出现的相关原文 |
|---|---|---|---:|---:|---:|---|---|
| invalid-filter | 是 | A | 6 | 2 | 24069 | checks_completed | 无 |
| invalid-filter | 是 | B | 6 | 12 | 23709 | checks_completed | 无 |
| no-match-rewrite | 是 | A | 8 | 2 | 23599 | waiting_review | c1 |
| no-match-rewrite | 是 | B | 8 | 16 | 25710 | waiting_review | c1 |
| conflict-natural | 否 | A | 8 | 2 | 23266 | checks_completed | c1, c2 |
| conflict-natural | 否 | B | 8 | 16 | 25378 | checks_completed | c1, c2 |

**每个任务，两臂的终态与进入模型上下文的相关原文完全一致。** 这说明重组是**保信息**的——契约可满足、信息不丢，才有可能把之后的差异归因给组织方式本身。

一次自查：第一版离线对照的读取器只扫了 `tool` 消息，于是把 A 臂读成"什么都没读到"，制造出一个**只存在于读取器里的差异**。修正后的读取器两种形状都读；出错那一版原样存为 `offline-contrast-reader-bug.json`，没有覆盖。

### 预置失败与自然任务分开

`invalid-filter`、`no-match-rewrite` 的措辞里明写"请先尝试该请求"，等于把一个已知会失败的请求塞给模型——**之后的修正是在纠正我们布置的状态，不是模型自行探索**。`conflict-natural` 不写任何检索建议，只说照护者的目标。两者在 FREEZE、任务定义（`preset_failure`）与结果表里始终分开记录。

---

## 三、控制变量

两臂的模型、端点、系统约束、工具接口、材料、校验、预算、评分口径完全一致，唯一差异是 `AGENT_PLANNER_HISTORY`。冻结记录见 [FREEZE.json](FREEZE.json)（`only_difference`、`held_equal`）。

- **两臂拿到同样的事实。** B 臂的状态块 = A 臂 payload 去掉四个历史键（`observations`/`completed_steps`/`recent_trace`/`dropped_calls`），**其余键逐字相同**；有测试逐键断言（`SameFactsAcrossArmsTests`）。工具目录与协议文本也逐字相同，B 臂没有多拿到一个工具、一条证据、一个正确查询。
- **检索接口固定为 B1**，两臂一致；未同时切换 B2 复合取证工具（`AGENT_EVIDENCE_INTERFACE=B1`）。
- **消息长度与 token 实际开销**（真实批量，按 wire 请求实测）：

| task | arm | 请求数 | 首次消息数 | 末次消息数 | 末次字符数 | 末次估算 token | 整回合估算 token |
|---|---|---:|---:|---:|---:|---:|---:|
| invalid-filter | A | 5 | 2 | 2 | 20062 | 6687 | 25147 |
| invalid-filter | B | 7 | 2 | 16 | 24689 | 8229 | 37990 |
| no-match-rewrite | A | 6 | 2 | 2 | 22005 | 7335 | 33100 |
| no-match-rewrite | B | 5 | 2 | 13 | 16271 | 5423 | 21849 |
| conflict-natural | A | 1 | 2 | 2 | 7632 | 2544 | 2544 |
| conflict-natural | B | 8 | 2 | 18 | 22516 | 7505 | 43162 |

  B 臂第一次请求与 A 臂基本相同，之后每轮追加一对消息；**整回合开销在三个任务上两个方向都出现过**（-34%、+51%、+1596%——最后一行是 A 臂被服务故障截断，不构成比较）。token 为字符数粗估，不是账单；本轮无计费凭据，货币成本未知。**格式本身的开销是真实的，没有被隐藏。**

- **任务措辞的检查**：`invalid-filter` 与 `no-match-rewrite` 的措辞里明写"请先尝试该请求"，即把已知会失败的调用交给模型。这两个任务里的任何修正**都不算模型自行探索**——它们纠正的是我们布置的状态。FREEZE、任务定义（`preset_failure=true`）与分析表都单独标注。`conflict-natural` 不给任何检索建议，是自然任务。两类不混在一起统计。

---

## 四、小规模反馈利用验证

三个代表任务，两个臂，交错运行。质量、终态、自主性、耗时、请求数：

| task | 预置失败 | arm | 请求数 | 终态 | 报告合格 | 相关原文 | 自主性 | complete | 耗时(s) |
|---|---|---|---:|---|---|---:|---|---|---:|
| invalid-filter | 是 | A | 5 | no_progress | 否 | 0 | 否 | 否 | 13.2 |
| invalid-filter | 是 | B | 7 | no_progress | 是 | 0 | 否 | 否 | 16.8 |
| no-match-rewrite | 是 | A | 6 | budget_insufficient | 否 | 0 | 是 | 否 | 16.3 |
| no-match-rewrite | 是 | B | 5 | no_progress | 否 | 0 | 否 | 否 | 17.7 |
| conflict-natural | 否 | A | 1 | budget_insufficient | 否 | 0 | 否 | 否 | 62.7 |
| conflict-natural | 否 | B | 8 | checks_completed | 否 | 2 | 否 | 否 | 20.7 |

预算耗尽、无进展停止、完整完成分别统计：`budget_insufficient` 2 次、`no_progress` 3 次、`checks_completed` 1 次。**没有把其中任何一种改写成另一种。**

### 反馈利用逐项观察

| task | arm | 看到检索反馈后 | 重复原动作 | 换成别的动作 | 取得相关原文 | 判定 |
|---|---|---|---|---|---|---|
| invalid-filter | A | 是 | 是（2 次） | 无 | 否 | 原样重复失败动作 |
| invalid-filter | B | 是 | 否 | list_materials、memory_read、read_material_item | 否 | 换了动作但没拿到原文 |
| no-match-rewrite | A | 是 | 是（1 次） | rag_search | 否 | 换了动作但没拿到原文 |
| no-match-rewrite | B | 否（一次检索都没发起） | — | — | — | 无可利用的反馈 |
| conflict-natural | A | 否（一次检索都没发起） | — | — | — | 无可利用的反馈 |
| conflict-natural | B | 是 | 否 | memory_write、rag_search、read_evidence | **是** | 换了动作并取得相关原文 |

### 一个有效的反馈利用案例（`conflict-natural`，B 臂）

这是本轮唯一一个把四项都走通的真实案例，四步都能在 wire 里逐条核对：

1. 模型对 `authority` 缺口提出 `memory_read(query="current_medications")`；
2. 校验拒绝（该缺口只接受完整快照读）。B 臂把这一次拒绝如实放进历史——**未执行，且不带任何结果**：
   ```json
   {"tool": "memory_read", "status": "not_executed", "reason": "safety_rejected",
    "errors": ["authority_requires_full_memory_read"]}
   ```
3. **模型的下一次请求把参数改成了 `query="snapshot"`**，随即执行成功（`wire-002.json`，`call_id 01a098a1608db64c1d444ada3f4738f7`）；
4. 之后 `plan_questions` → `rag_search`（`found`）→ `read_evidence`，把两条相关原文读进了上下文，终态 `checks_completed`。

同一份历史里，一次多调用响应里的第二个 `rag_search` 被单动作契约丢弃，它以 `{"status":"not_executed","reason":"one_action_per_cycle"}` 且**无结果**的形式出现；模型没有重发它，而是继续推进。这正是"未执行的调用不得伪造成功结果"这条规则想要的行为。

**必须同时说清楚的三件事**：

- 这是**一条**运行，不是一次比较。它的 A 臂配对运行被服务故障毁掉了（只发出 1 次请求、62.7 秒后 `budget_insufficient`），所以**不能**据此声称"B 臂比 A 臂好"。
- 这条运行**没有达到**预声明门槛：报告未读材料索引（`material_index_never_read`），报告质量不合格。
- `invalid-filter` 上确有行为差异（A 原样重复失败检索 2 次，B 改写查询后改换工具），但两臂都没拿到原文。

### 其他值得记录的行为

`no-match-rewrite` 的 B 臂在 5 次请求里只调用 `memory_read`（含同一参数重复）与一次 `read_material_item`，**一次检索都没发起**，因此它根本没有看到检索反馈，最终被既有的无进展护栏停下。这既不是"利用反馈"，也不是"不利用反馈"——是模型没走到那一步。已有的护栏按原样生效，本轮没有为它放宽任何东西。

**脚本规划器的离线基线**只证明协议与执行正确：三个任务在两臂下终态与进入上下文的相关原文完全一致（见上文离线对照表），不计为模型能力证据。

---

## 五、真实实验的投入与停止

- 离线协议、异常与恢复测试**全部完成并通过**之后，才冻结真实批次。
- 使用已有明确授权的模型配置（`siliconflow` / `Qwen/Qwen2.5-7B-Instruct`），每任务 10 次请求、总计硬上限 60 次，**重试计入额度**；SDK 与供应商重试均为 0。实际花费 **32 / 60**。
- 六次运行交错执行（`invalid-filter A/B → no-match-rewrite A/B → conflict-natural A/B`）。
- 服务故障原样保留：`conflict-natural` A 臂 1 次服务故障（超时），该行未被替换、未被删除，仍留在分母里。未出现审计丢失、重复副作用或安全回归，停止条件未被触发。
- **门槛未达。** 预声明门槛是"至少两个代表任务同时满足相关原文、报告质量、终态、无策略兜底、安全 enforced"。实际 6 次运行里 **0 次**满足全部条件（`conflict-natural` B 只差报告质量一项）。因此：**不追加提示词版本、不反复抽样、不扩大回归。**

---

## 六、接入与交付判断

- B 臂实现已保留为**可配置**（`AGENT_PLANNER_HISTORY=tool_history`），并可按任务持久入口运行；现有权限、预算、版本、无进展、证据校验全部沿用，未新增 Agent、未迁移框架。
- **默认仍是 A 臂（现状快照）。** 依据是结果而不是协议是否更标准：本轮没有达到"改善任务结果"的门槛，所以不切换默认。协议更接近供应商原生形状，本身不构成切换理由。
- 异常交付（第一部分）是独立于 A/B 的可靠性修复，**应当保留并合入**：它把一个会把整个回合变成不可读异常的路径，改成了一次准确、完整、不冒充成功的失败交付。
- **尚未验证、且需要外部条件才能验证的事项**（不以增加规则或扩大重试代替）：
  1. 本轮 n=1/格，且两条 A 臂运行被服务故障或 `no_progress` 截断。要回答"B 臂是否更好"，需要更大的样本，而这需要先解决端点稳定性——`conflict-natural` A 臂一次超时就吃掉了整个 180 秒预算。**这是当前最该先解决的外部条件。**
  2. `material_index_never_read` 在两臂都反复出现。它是评分口径与真实行为之间的缺口还是真实缺项，本轮没有单独取证，未作结论。
  3. 恢复(care-task resume)路径下的 B 臂历史重建只在单元测试里验证过（持久 trace 复用 / 不可恢复显式标注），**没有**在真实恢复流程里跑过。

### 本轮工程偏差（如实记录，不覆盖）

| 事项 | 处理 |
|---|---|
| 冻结后、首次远程调用前修改了运行器的停止条件（服务故障计数原先读一个不存在的键，两次连续故障不会停） | 归档旧 FREEZE 为 `FREEZE-archived-1.json` 并重新冻结，理由写入 `refreeze_reason`；模型/任务/预算/评分口径未变 |
| 第一版离线对照的读取器只扫 `tool` 消息，把 A 臂读成"什么都没读到" | 修正读取器；出错版本原样存为 `offline-contrast-reader-bug.json` |
| B 臂把检索原文截到 600 字，A 臂截到 200 字（且标记是否计入预算不一致） | 批次结束后修正为与 A 臂逐字一致并加测试。**对本批次无影响**：全部材料 chunk 文本 ≤29 字，两个上限都没有生效 |
| `run_visitprep` 的 SQLite 备份用 `with sqlite3.connect` 只提交不关闭，Windows 下锁住 `run.sqlite` | 显式 `close()` |

---

## 复现入口

- `scripts/diag-final-block.py`：上一轮冻结提案的离线重放，抓出被拒正文与判定路径；不调用供应商。
- `scripts/tool-history-capture.py`（`--frozen`）：SDK 缝上的 A/B 请求形状捕获与离线对照；无远程调用。
- `scripts/tool-history-controlled.py`：冻结、离线与真实批次（同一冻结的脚本已拒绝重复运行）。
- `scripts/analyze-tool-history.py`：只读重建逐任务质量、终态、请求数、反馈利用判定；保留未采样行。
- 回归：`output/closeout-toolhistory-4/engineering.json`（status pass，43 suite / 629 项）。
- 原始工件在 `output/tool-history-2026-09-13/`（本地忽略目录），不含复制出的密钥文件。

本轮没有降低任何安全门槛，没有替换或删除失败样本，没有为消除失败而调整任务措辞或预算。
