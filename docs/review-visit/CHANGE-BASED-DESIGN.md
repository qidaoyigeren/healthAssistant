# 基于变化的回访决策（2026-09-14）

**基线**：`feat/review-visit` @ `2ee30e9`（回访单元已交付的那一版）
**上一份记录**：[REPORT.md](REPORT.md)

本轮要让**第二次回访**与第一次不同：系统记得上次做过什么，本次只围绕仍需要处理的
变化与缺口继续。完成标准是一句话——**同一件安全事项第二次回访时，系统能够利用
第一次的结果和期间变化，执行有区别、有依据的下一步。**

---

## 一、核实结果：当前调用链的六个事实

这六条是设计的事实前提，都已在本仓库中复现或读到。

### 1. 回访目标仍是通用的"核实风险是否成立"

`care_tasks._safety_case_goal(case)` 结尾固定为「请核实该风险在当前记录下是否成立、
依据是什么、还缺什么信息，并说明下一步。」而 `safety_cases._ensure_visit_task`
调用 `tasks.create(..., 'safety_case', case['id'])` 时**不传 goal**，因此每一次回访
都套这段同一句话。回访与普通调查在目标上没有任何区别。

### 2. 回访任务没有绑定 visit

绑定是单向的：`visit.care_task_id` 指向任务，而 `care_task` 只有
`safety_case_id`，没有 `visit_id`。恢复时无法判断"这个任务还是不是那次回访的"。

### 3. 模型看不到本次回访的原因、上次的结果、未消费的变化

`care_tasks._safety_case_context` 目前给出三样：

- `investigation_context`（事项、结论、未决问题）——正确；
- `previous_investigation`（**上一轮 run**，不是上一次回访）——错位；
- `new_since_last_run`（游标驱动）——机制正确；
- `budget`。

**没有**：本次回访 `reason`、上一次回访的 `result`/`focus`、待确认候选、
已确认的跟进安排、允许的动作、`previous_visit_id`。

### 4. 第二次回访从空白开始，上一轮的答案无法复用

`_ensure_visit_task` 遇到 `completed`/`failed`/`cancelled` 的任务会**新建一件**，
`investigation` 从 `None` 起。唯一的延续是 `case.required_inputs`，而
`investigation_context` 只把它投影成 `{request_id, question, answer_kind}`——
**没有答案的值、没有 assessment、没有来源**。模型看不到上次答了什么，也就
谈不上"复用"。

这条同时说明了为什么要求八（O-1/O-2）是前置条件：答案复用坏掉时，
第二次回访只能把第一次重新做一遍。

### 5. 游标会吞掉没处理过的事件

`_execute_safety_case` 末尾**无条件**执行
`task['case_history_cursor'] = len(store.get(case['id'])['history'])`。
失败、取消、降级时同样推进；而且这里 re-read 了 case，**本轮运行期间新增的
历史**（如 `answer_retired`）也会一并被标成已消费。两者都违反第四节的要求。

### 6. 两套失效入口各说各话

`safety_cases.retire_stale_answers` 只重开 `required_inputs` 里的**请求**；
investigation 里那条答案的 `assessment` 仍然是 `verified`。请求说"未决"、
答案说"可靠"，两个状态同时成立。

另外它按 `answered_against != 当前 revisions` 判断，**任何**范围变化都重开
**全部**已回答请求——无关变化会重新询问所有问题。

---

## 二、设计

### 2.1 数据模型：回访与任务互相绑定

`care_task` 新增两个字段（仅 `safety_case` 契约使用）：

```
task['visit_id']     = visit['id']    # 这次执行属于哪一次回访
task['visit_intent'] = {...}          # 本次执行意图，建任务时落盘（见 §2.2）
```

契约版本：`CONTRACTS['safety_case']['version']` → `2`，
`SAFETY_CASE_CONTRACT` → `'safety-case@2'`。

**升级路径必须显式处理。** `CareTasks.resume` 在
`task['contract_version'] != contract['version']` 时抛
`ProductError('任务契约已更新，请创建新的待办', 409)`。`_ensure_visit_task` 必须
先识别契约版本不同的在跑任务，**不复活它**，改为新开一件并把原因写进回访历史——
不能把 409 抛给用户。

复用判据从"同一 case 有一件在跑"收紧为"**同一 visit** 有一件在跑"：

```
running = [t for t in ... if t.get('goal_type') == 'safety_case'
           and t.get('safety_case_id') == case['id']
           and t.get('visit_id') == visit['id']         # ← 新增
           and t['status'] not in ('completed','cancelled','failed')]
```

`_execute_safety_case` 在写回访结果前校验 `task['visit_id']` 仍指向
`visit.care_task_id == task['id']` 的那次回访；对不上就**不写**。
这是要求三"防止其他回访或旧任务的结果覆盖当前回访"的落点。

### 2.2 回访目标由事实推导

新增 `review_visits.visit_intent(product, case, visit) -> dict`，四路输入：

1. 实际触发原因（`visit['reason']`，服务端按事实判定）；
2. 上次未完成事项（上次回访结果的 `unresolved` 与 `focus`）；
3. 上次之后实际新增的相关事件（游标之后的历史）；
4. 已确认的跟进安排（`case.follow_up`）。

产出一段 goal 文本 + 结构化意图。goal 文本替掉回访路径上的
`_safety_case_goal`（该函数保留给**非回访**的直接调查，行为不变）：

> 「这是同一件{kind}安全事项的第 N 次回访。本次起因：{reason.detail}。
> 上次仍未完成：{…}。上次之后新增：{…}。已确认的安排：{…}。
> 请判断当前仍需要处理什么，并选择本次最值得执行的下一步——直接复用已有结论
> 交付／问一个具体事实／读材料或证据／提出待确认变更／按新证据重核某判断／
> 说明还需等待什么。不要为了重新得到同一结论重复检索。」

截断到 300 字（与既有 `_safety_case_goal` 同一口径）。

**仅在原风险依据失效或出现相关新证据时**，意图里带 `recheck_reason`，
那时才进重新核对。没有 `recheck_reason` 时"重复检索同一结论"没有合法理由。

### 2.3 真正进模型的回访摘要

`_safety_case_context` 增加 `visit` 块。**只放引用与状态**，不复制患者事实、
药单、证据或结论——与 `review_visits` 模块既有的"只存引用与叙述"原则一致。

| 键 | 内容 |
|---|---|
| `visit_id` / `previous_visit_id` / `sequence` | 本次回访身份与第几次 |
| `reason` | 本次起因（服务端按事实判定） |
| `started_from` | 本次起点（上次结束游标） |
| `previous_result` | 上次回访的 `unresolved` / `focus` / `next_step` |
| `new_since_last_visit` | `changed_scopes` + 上次回访**结束游标之后**的事件 |
| `reusable_answers` | 仍有效答案：`request_id` + 答案值 + `assessment` + 来源 + `answered_against` |
| `retired_answers` | 已失效答案及原因 |
| `pending_candidates` | 待确认变更候选 |
| `confirmed_follow_up` | 已确认的跟进安排 |
| `open_questions` | 本次仍需解决的问题 |
| `allowed_actions` / `budget` | 权限边界与剩余预算 |

**三种状态分开表达，不合流：**

- `changed_scopes` 非空 → **记录确实发生变化**；
- `pending_candidates` 非空 → **用户报告了变化，尚未确认**；
- 两者都空且 `events` 空 → 使用既有的 `NO_NEW_RECORDS` 那一句原话
  （「系统尚未收到新记录；这不等于情况没有变化，也不表示风险已经解除。」）。
  "没有新记录"**不等于**患者情况稳定。

同时补全 `investigation_context.questions.answered`：带上答案值、`assessment`、
来源、`answered_against`。这是 §一.4 那条断裂的实际修复，也是"第二次回访能复用"
的前提。

### 2.4 游标只在成功消费时推进

`_execute_safety_case`：

1. 在**建上下文那一刻**取历史长度快照 `consumed_at`；
2. 运行结束后，仅当 `termination_reason ∈ {checks_completed, waiting_input,
   waiting_review}` 时推进，且推到的位置是
   `min(consumed_at, 当前历史长度)`；
3. 失败、取消、降级、`no_progress`（`unrecoverable_failure` 等）一律**不动游标**。

第 2 步的 `min` 是关键：本轮运行期间新增的历史（`retire_stale_answers` 写的
`answer_retired` 等）不在快照内，因此不会被误标为已消费。

### 2.5 动作空间与完成条件

**约束（读代码得到，必须照顾）**：`run_open_review` 的循环里，`respond` 只在
`investigation.termination_reason` 已非空时才对模型开放
（[agent.py:1978](../../stage0/agent.py#L1978)）；循环内 `forced_stop()` 在
**规划之前**先检查一次（[agent.py:3543](../../stage0/agent.py#L3543)），
命中就直接 `break`，**一个模型调用都不发生**；循环结束后 `run_open_review`
**没有**单独的模型交付回合，报告由代码渲染。

因此：

1. `InvestigationState` 新增派生判定 `visit_ready_to_deliver()`——回访摘要表明
   「上次问过的都已答且有依据、本次无新事件、无未决问题、无 `recheck_reason`」。
2. **让 `respond` 在此时对模型开放**：`respond_available` 与 decision enum 的
   判定由 `bool(termination_reason)` 扩展为
   `bool(termination_reason) or visit_ready_to_deliver()`。模型于是可以**第一轮
   就选择"复用已有结论并交付"**，不必先规划、再搜索、再提问。
3. `_forced_stop_typed` 的回访分支同样按 `visit_ready_to_deliver()` 返回
   `checks_completed`，但**要求模型已经做过至少一次决策**（新增字段
   `model_decisions`，在 `_decide` 调用规划器前自增）。这条防止循环在规划前
   那一次检查里就收尾、导致**零模型调用**——那样就没有"模型选择了下一步"
   这回事，交付沦为代码渲染。
4. 循环退出时若 `termination_reason` 仍为空，但
   `visit_ready_to_deliver()` 成立且 `model_decisions >= 1`，按
   `checks_completed` 收尾（现在的代码在这里会落成 `max_cycles`，
   把一次正常的"无事可做"记成失败）。

配合 §2.2 的"不要重复检索"指令，已有有效结论时模型可以解释和跟进其相关问题，
而不是为了重新得到同一结论反复搜索。

### 2.6 五种回答的后续行为

| 回答 | 后续行为 |
|---|---|
| **A 提供了需要的事实** | 保存来源，按既有确认机制记 `candidate`（§3.5：不因用户陈述升级为 verified），利用新信息继续当前问题 |
| **B 情况发生变化** | 生成可核对的候选；**确认前权威记录一个字节不动**；确认后走 `record_input` 并重排队必要检查 |
| **C 尚未完成某项行动** | 保留未完成状态及其原因。**带可处理信息的**（原因里含新事实、新时间、新来源）→ 唤醒并允许调整下一步；**纯推迟的** → 不唤醒、不空转 |
| **D 不清楚** | 保留不确定性，转其他可用来源；来源穷尽则明确阻塞。**不反复换措辞追问** |
| **E 暂不回答** | 保持 `declined`，与"不知道"分开；刷新页面与 worker 扫描都**不**立即重复追问 |

C 的分岔是本轮新增：现在推迟类一律不唤醒（那是为修熔断引入的），需要一个
**"这条回答是否携带可消费信息"**的判定，而不是只用唤醒/不唤醒表达全部业务逻辑。

判定规则（代码可实现、有断言）：

- 回答值或原因文本里出现**可核对的新成分**——新的时间/日期、新的量值、
  明说的新来源——→ 唤醒，并把这段文本作为 `new_since_last_visit` 里的一条
  用户报告事件带进上下文；
- 只有推迟表态、没有任何新成分 → **不唤醒**，请求保持未决且**不重复追问**；
- 判不出时**保守取不唤醒**（与推迟类原有的熔断修法方向一致）。

### 2.7 两处边界修复

#### O-1：共享实体名不足以支持断言

`claim_support.assess_support` 现在只查两件事：实体名在片段里、断言里的**数字**
在片段里。「服药频次是什么」这句没有数字，主词「合成药甲」又出现在一段讲
出血风险的文字里 → 判成 `supported_by_span`。**谓语从未被检查。**

修法（词表优先 + 内容词兜底）：

1. `target_field` → 属性词表（`schedule`→频次/频率/每日/…，`dose`→剂量/用量/…，
   `route`→途径/口服/…，`start_at`→开始/起始/…）。片段里至少要出现一个属性词；
2. 词表覆盖不到该字段时退回："断言去掉实体名与停用词后，至少一个内容词出现在
   片段里"；
3. 两者都不满足 → `no_supporting_span`，问题保持未决，转其他来源。

既有实体检查与数字事实检查**保留**。

#### O-2：`available` 必须能指认是什么回答了它

`investigation._sync_question_from_claim` 把问题写成 `information_state=available`、
`answered_by='evidence'`，**却从不写答案元素**。于是 `available` 配 `answers == []`
——界面只能显示"已回答"，说不出是什么回答了它。

修法：回读证据时把**命中支持的片段**记在 claim 上；`_sync_question_from_claim`
settle 时据此写出**答案元素**（`source_ref` + `quote` + 既有 assessment）。
不新造状态判定，复用现有的 assessment。

#### 失效统一与重开范围

`_sync_questions_to_case` 由单向改为**双向**：请求被 `retire_stale_answers` 重开时，
反向把 investigation 里对应答案的 `assessment` 标成 `stale` 并重开问题。
两处状态不再各说各话。

同时给请求记录 `dependency_scopes`（这条答案**真正依赖**的范围）。
`revisions()` 只有两个范围：`medications` 与 `semantic`。解析规则：

- 答案来源是 `patient_record` → 按被解析引用的种类取（用药引用 → `medications`，
  事实引用 → `semantic`）；
- 答案来源是 `user_reported` → 看这条问题**关于什么**：`subject_refs` 指向用药
  → `medications`；指向事实 → `semantic`；
- 解析不出来的（没有引用也没有对象）→ **保留范围为空并按旧口径处理**
  （记录变了就重开）。方向与模块原有注释一致：重新打开是保守方向，
  宁可再问一次。

`retire_stale_answers` 只比 `dependency_scopes` 覆盖到的那些范围。
无关的变化不再重开全部已回答问题。

不建立第三套状态判定——两处都用既有的 assessment 与 `answered_against`。

### 2.8 用量读数

`_execute_safety_case` 补上 `_execute_evidence_review` 已有的重算
（`tokens_actual` / `calls_actual` / `usage_unknown`）。验收脚本改读
`CareTasks.usage(task)`，并汇总同一次回访的**恢复运行**（`child_run_ids` 全部）。

**缺失一律记 `None`（unknown），不记 0。** 0 是一个测量结果，"没测到"不是同一个
意思。

### 2.9 前端

只修改现有回访页 `frontend/src/features/safety/ReviewVisitPage.tsx`。结果区增加：

- 本次新增确认了什么；
- 哪些已有信息被复用；
- 哪些行动仍未完成；
- 哪些判断需要重新核对；
- 本次为什么结束或等待；
- 下一次继续处理什么。

每条关键内容关联实际记录、回答或证据，并显示来源属性
（`program_check` / `user_report` / `model_explanation` / `record`）——
模型的解释与程序的检查结果必须看得出是两种东西。

**不新增全站页面。**

---

## 三、验证

1. **离线三场景**，走真实 HTTP 端点 + 真实 worker（扩
   `stage0/test_review_visit_flow.py`）：已有答案仍有效／出现相关变化／行动尚未完成。
   使用真实任务与工具入口，**不直接写最终状态制造通过**。
2. **本轮完成标准的专项测试**（新增）：同一事项两次回访——第一次留下结论，
   期间发生一次变化，第二次必须（a）复用上次结果、（b）聚焦实际变化、
   （c）给出有区别且有依据的下一步。
3. **O-1/O-2 两条基线红转绿**；跑全量 60 套件确认没有新红。
4. **浏览器验收**扩到新的结果区（扩 `scripts/review-visit-browser-acceptance.js`）。
5. **一次真实验收**：20 calls / 420s / 250k tokens，跑前打印上限。
   不追加批次、不换模型、不扩大预算。补日志不得重新发起真实调用，
   优先读已有运行轨迹。

真实验收观察四项：是否使用了上次结果、是否聚焦实际变化、是否根据回答调整行动、
是否形成具体的回访交付。

---

## 四、边界与不做的事

- 不做诊断、不自动调整用药、不提供专业医疗服务；
- 不改答案可信性的判定机制，不改风险关闭机制；
- 不新增评分平台，不扩大历史评测集合；
- **不新增第二套事项／患者档案／调度系统**：回访仍只存引用与叙述，
  调度仍是既有 worker 周期；
- 不新增全站页面；
- 候选变更的写入通道仍只覆盖 `dose`/`schedule`/`route`/`start_at`。

## 五、已知未实现的护栏（沿用上一轮，本轮不解决）

- "不得凭模型自行生成临床阈值或检查周期"仍只有结构性约束，
  没有对**问题措辞**的机械检查。
- `assessment` 在真实批次里的分布仍未测。
