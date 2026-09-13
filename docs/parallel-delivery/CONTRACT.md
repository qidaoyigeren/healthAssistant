# 并行交付接口冻结（CONTRACT）

**基线 commit**：`30f5dd4`（`integration/baseline-2026-09-13`）
**状态**：已冻结。A/B/C/D 四路任务在各自 worktree 内按本文件实现。
**偏离本文件的任何接口改动都必须走 §7 的范围外变更流程。**

读者：四个并行任务的执行者，以及最后的集成 Agent。

---

## 0. 怎么用这份文件

- 本文件只冻结**跨任务边界**的东西：字段名、枚举取值、端点形状、时间与错误约定。
- 各任务内部的实现细节、模块划分、私有函数名**不在冻结范围**，各自决定。
- 冻结的原则是**兼容式扩展**：只新增可选字段，不改动、不复用、不重新解释既有字段的语义。
  既有字段一旦被重新解释，D 的历史数据验收和 C 的既有页面会同时失效。
- 文中标注 `已存在` 的是当前代码里已经在跑的；标注 `新增` 的是本文件要求新增的。

**注意**：本文件描述的若干"当前行为"本身就是要被修掉的缺陷（§3.4、§4.5）。
那里写的是**目标语义**，不是既有实现。实现者要改代码去满足它，而不是照抄现状。

---

## 1. 已核实的当前 API 形状（作为扩展的地基）

### 1.1 错误信封（全局统一，沿用）

失败响应的形状是固定的：

```json
{"error": {"code": "...", "category": "...", "message": "...", "trace_id": "...", "details": {}}}
```

- `category` ∈ `validation | safety | provider | internal`（文档口径）。
- **已知偏差**：幂等键的失败记录里 `error.category` 还会出现 `retryable`、`effect_unknown`、`safety`。
  这是既有的存量行为，本文件**不改动它**，但 D 验收时不要把它当成"契约违反"。
- `trace_id` 来自请求头 `X-Trace-Id` 或随机生成，并在每个响应上以同名响应头回显。
- **第二个失败形状**：`GET /v1/events/{key}` 在任务失败时返回 **500**
  `{event_key, status:"failed", error_class, error}`，**没有** `error` 信封、**没有** `trace_id`。
  这是既有的、被文档承认的例外。新增端点一律使用 §1.1 的信封，不要仿效它。

### 1.2 并发与幂等（沿用，勿自创）

所有写入走 `ProductStore.command(key, payload, execute)`：

- 请求体必带 `key`（幂等键），同一 `key` 配不同内容 → `409`。
- 乐观并发用 `expected_revision`（int）对 `revision` 做 CAS；
  不匹配 → `409`（现有文案："事项已被其他操作更新，请刷新"）。
- 新增的写端点必须沿用这两个机制，不要引入第二套并发控制。

### 1.3 时间

- 服务端生成的时间一律 `utc_now()`：**带时区的 UTC ISO-8601，秒精度，`+00:00` 后缀**。
- 解析用 `memory.py:_as_utc`（接受 `Z`，把 naive 当 UTC）。
- **`follow_up.at` 目前是唯一的例外**——它是不经解析的调用方字符串
  （前端送 `Date.toISOString()`，即毫秒 + `Z`；测试送 `+00:00`）。
  §4.4 要求把它收敛掉。

### 1.4 引用格式

版本化引用沿用既有形状：`<layer>:<kind>:<id>@<version>`，例如
`memory:conclusion:123@1`、`memory:medication:45@2`。

### 1.5 现状里两处已知的键名漂移（新增字段不要跟着漂）

1. 事项问题项上 `question_strategy`（由 `require_input` 写）和 `strategy`
   （由 `care_tasks._sync_questions_to_case` 每次同步覆盖）**同时存在**，指的是同一件事；
   TS 只声明了 `question_strategy`。
2. `answered_parts` 目前是 `answers` 的**未过滤拷贝**，所以后端实际下发的键比
   `frontend/src/api/types.ts` 声明的多。**新增的 `assessment` 会自动出现在 DTO 里**，
   但 TS 类型不会自动跟上——这是 C 的具体任务（§3.6）。

---

## 2. 文件所有权与共享冻结文件

见 [OWNERSHIP.md](./OWNERSHIP.md)。要点：**每个文件只有一个所有者**；
不属于你的文件一律视为只读冻结，需要改动走 §7。

---

## 3. 接口一：答案可信性（assessment）

**生产方 A ｜ 投影方 B ｜ 展示方 C ｜ 验收方 D**

### 3.1 挂载位置

沿用当前 `question.answers` 及 `answered_parts` 投影。
`assessment` 是**答案元素上的一个新增可选键**，不新开集合、不改集合层级。

当前一条答案元素的既有键（`investigation.py:1655-1662`，11 个）：

```
value, field, source, provenance, source_ref, quote, origin, answer_ref, at, version, still_uncertain
```

（`care_tasks.py:733-737` 写入的用户答案只有其中 7 个：缺 `quote`、`source_ref`、`at`、`version`。）

**A 不得改动以上任何一个既有键的名称或语义。**

### 3.2 assessment 形状（新增）

```json
"assessment": {
  "status": "verified",
  "reason": "引用片段与已读回的证据原文逐字一致",
  "source_ref": "evidence:8842",
  "locator": "第 3 段第 2 行",
  "dependency_refs": ["memory:medication:45@2"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `status` | `verified \| candidate \| stale \| unsupported` | 是 | 见 §3.3 |
| `reason` | `string` | 是 | **具体**原因，非空。不得写"ok"/"通过"这类无信息量的话 |
| `source_ref` | `string \| null` | 是（可为 null） | 来源引用。与答案既有 `source_ref` 同源时取同一值 |
| `locator` | `string \| null` | 是（可为 null） | 字段路径或片段位置；定位不到就 null，**不要编造** |
| `dependency_refs` | `string[]` | 是（可为空数组） | 版本化依赖引用，见 §1.4 |

### 3.3 status 语义（冻结）

- `verified`：**在约定范围内**，该答案的依据已核对。
- `candidate`：有候选依据，但核对未完成。
- `stale`：曾有依据，但依赖的版本已变化，需要重新核对。
- `unsupported`：没有可支撑该答案的依据。

> **`verified` 的含义边界（必须写进实现与 UI 文案）**
>
> `verified` **仅**表示"约定范围内的答案依据已核对"。
> 它**绝不**表示整体用药安全，也**绝不**表示已经完成专业医疗判断。
> 任何把这四个字渲染成"安全""已确认无误"的 UI 文案都是缺陷。

### 3.4 缺失即未核实（冻结，这是硬约束）

- 历史记录里**没有** `assessment` 时，消费方一律按**未核实**显示。
- **不得默认 `verified`。** 不得用 `candidate`/`unsupported` 之外的任何东西去"填补"缺失。
- C 在 UI 上必须能区分"未核实（无 assessment）"和四种 `status` 中的任意一种——
  即 5 个视觉状态，不是 4 个。

### 3.5 与既有概念的对齐（不要另造词汇）

当前已有 `provenance`（5 值）和 `still_uncertain[]`。`assessment` 与它们**并存**，不替代：

| 既有 `provenance`（由 `source` 派生） | 典型 assessment.status |
|---|---|
| `authoritative_record` | `verified` / `stale` |
| `reference_evidence` | `verified` / `candidate` |
| `material_record` | `verified` / `candidate` |
| `user_reported` | `candidate`（**不得**仅因用户陈述就 `verified`） |
| `professional_opinion` | `candidate` |

此外 `answered_against`（事项请求级，`safety_cases.py:712`）与答案级 `version`
已经是版本快照，A 生成 `dependency_refs` 时应**复用**它们，不要新建第三套版本机制。

### 3.6 各方的具体交付边界

**A（生产）**
- 在 A 拥有的文件内产出 `assessment`；写入 `question['answers'][-1]` 及既有投影路径。
- 必须覆盖：无依据、依据存在但未核对、依据核对通过、依赖版本变化后失效 四种情形。
- `verified` 的判定必须基于**实际核对动作**（读回的原文比对），不得基于模型自述。

**B（投影）**
- 原样投影，**不加工、不猜测、不补默认值**。
- 依赖变化时协调重新核对：`retire_stale_answers` 已存在的重开机制是协调点，
  重开时对应答案的 `assessment.status` 应变为 `stale`（**由 A 产生，B 只触发与搬运**）。
- 投影时若答案无 `assessment`，**不得**添加任何默认值。

**C（展示）**
- `frontend/src/api/types.ts` 的 `SafetyRequiredInputDto.answered_parts` 元素类型
  （当前 `types.ts:664-670`，只声明了 `value, field, source, provenance, still_uncertain` 5 个键）
  需要补上 `assessment`（其余后端已下发但未声明的键按需补，非必须）。
- 展示 5 个状态（4 个 `status` + 未核实），文案必须体现 §3.3 的含义边界。

**D（验收）**
- 按 §3.2 逐字段比对；重点验收 §3.4（缺失→未核实，不得默认 verified）与 §3.3 的含义边界文案。

---

## 4. 接口二：长期跟进（follow_up）

**生产/调度方 B ｜ 展示方 C ｜ 验收方 D**

### 4.1 挂载位置

`follow_up` 目前挂在**安全事项**对象上，经 `case_view()` 以顶层键 `'follow_up'` 下发
（`safety_cases.py:1124`），并随 `POST /v1/safety-cases/{case_id}/disposition` 写入。
**保持不变。**不要把它搬到 care_task 上。

### 4.2 目标形状（兼容式扩展）

既有键原样保留：`kind`、`at`、`condition`、`owner`、`note`、`recorded_at`。

```json
"follow_up": {
  "kind": "review_at",
  "at": "2026-10-01T00:00:00+00:00",
  "condition": null,
  "owner": "caregiver",
  "note": null,
  "recorded_at": "2026-09-13T08:00:00+00:00",

  "confirmed": false,
  "confirmed_at": null,
  "confirmed_by": null,
  "confirmation_ref": null,

  "revision": 1,
  "schedule_state": "scheduled",
  "last_triggered_at": null,
  "last_trigger_reason": null,
  "care_task_id": null,
  "blocked_reason": null
}
```

| 字段 | 类型 | 状态 | 说明 |
|---|---|---|---|
| `kind` | `review_at \| on_event \| arrangement` | 已存在 | 取值不变 |
| `at` | `string \| null` | 已存在 | **语义收紧**：见 §4.4，必须带时区 |
| `condition` | `object \| null` | 已存在 | **语义收紧**：见 §4.3，白名单结构 |
| `owner` | `string` | 已存在 | 不变 |
| `note` | `string \| null` | 已存在 | 不变 |
| `recorded_at` | `string` | 已存在 | 安排的产生时间 |
| `confirmed` | `bool` | 已存在 | **语义变更**：见 §4.5 |
| `confirmed_at` | `string \| null` | 新增 | 确认记录的时间 |
| `confirmed_by` | `string \| null` | 新增 | 确认人 |
| `confirmation_ref` | `string \| null` | 新增 | 指向**实际确认记录**的引用 |
| `revision` | `int` | 新增 | 安排 revision |
| `schedule_state` | `scheduled \| due \| triggered \| blocked \| cancelled \| unscheduled` | 新增 | 调度状态 |
| `last_triggered_at` | `string \| null` | 新增 | 最近触发时间 |
| `last_trigger_reason` | `string \| null` | 新增 | 最近触发原因 |
| `care_task_id` | `string \| null` | 新增 | 关联的 care task |
| `blocked_reason` | `string \| null` | 新增 | 错误或阻塞说明 |

> **`kind` 与 `schedule_state` 是两件事，不要合并。**
> `kind` 是"安排的种类"（按时间 / 按事件 / 仅备忘）；
> `schedule_state` 是"这条安排现在走到哪了"。`kind='arrangement'` 的安排可以永久
> 停在 `unscheduled`，这是合法的。

### 4.3 `condition` 白名单结构（新增，冻结）

**不得**执行自然语言或任意表达式。只接受白名单结构，复用既有触发器词汇：

```json
"condition": {"kind": "conclusion_recorded", "ref": "memory:conclusion:123@1"}
```

| `condition.kind` | 必填键 | 含义 |
|---|---|---|
| `conclusion_recorded` | `ref`, 可选 `conclusion_kind` | 某结论被记录 |
| `necessary_check` | `ref`, 可选 `check_id` | 某项必要检查完成 |
| `medication_change` | `ref` | 用药集合变化 |
| `fact_change` | `ref` | 患者事实变化 |

- 后两类对应既有 `TRIGGER_MEDICATION_SET` / `TRIGGER_CONDITION_FACTS`（`safety_checks.py:32-33`）。
- **未知 `kind` → `422`**，并且**不得**静默降级成 `arrangement` 或"永不触发"。
  静默降级会让一条永远不会触发的安排看起来是正常的。
- 非结构化字符串（即当前的自由文本）→ `422`。

### 4.4 `at` 的时区（收紧，冻结）

- 接受：带偏移量的 ISO-8601（`+00:00` 或 `Z`）与带在响应里一律规范化成
  `utc_now()` 同款的 `+00:00` 秒精度形式。
- 无时区的 naive 字符串 → **`422`**。（既有 `care_task.due_at` 已经是这个口径：
  "待办日期必须包含时区"，`care_tasks.py:201-206`——`follow_up.at` 对齐它。）
- 前端 `Date.toISOString()` 的毫秒 + `Z` 形式**可以接受**，服务端负责规范化后存储。

### 4.5 `confirmed` 必须来自实际确认记录（语义变更，冻结）

> **有时间或条件不等于已经确认。**

当前实现是 `confirmed = bool(at or condition)`（`safety_cases.py:203-225`），
即"只要调用方给了时间就自动算已确认"。**这是要修掉的缺陷，不是契约。**

目标语义：

- `confirmed` 只能由**实际确认记录**产生（§4.6 的确认端点）。
- 仅有 `at` 或 `condition` ⇒ `confirmed: false`，且 `schedule_state` 仍可为 `scheduled`。
  **"已安排"与"已确认"是两件事。**
- `confirmed: true` 时，`confirmed_at`、`confirmation_ref` 必须非空；
  三者不一致的记录视为损坏，投影时按 `confirmed: false` 处理。
- 存量数据（老记录里 `confirmed=true` 但没有 `confirmed_at`/`confirmation_ref`）
  **一律按 `confirmed: false` 读**。这是一次有意的、可见的行为回退，D 要专门验收它。

### 4.6 修改 / 取消 / 确认端点（新增）

当前 `follow_up` 是**只写一次、不可改、不可取消**的：唯一的写入点是
`accepted_monitoring` 处置分支（`safety_cases.py:925`），且 `derive_status()` 从不清除它。
要支持"长期跟进"就必须补这三个动作。**沿用 §1.2 的 `key` + `expected_revision`。**

#### 4.6.1 安排 / 改期

```
POST /v1/safety-cases/{case_id}/follow-up
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `key` | `string` | 是 | 幂等键 |
| `expected_revision` | `int` | 是 | 事项 revision 的 CAS |
| `action` | `"schedule"` | 是 | |
| `kind` | `review_at \| on_event \| arrangement` | 是 | |
| `at` | `string \| null` | `kind=review_at` 时必填 | 带时区，见 §4.4 |
| `condition` | `object \| null` | `kind=on_event` 时必填 | 白名单，见 §4.3 |
| `owner` | `string \| null` | 否 | 缺省 `caregiver` |
| `note` | `string \| null` | 否 | |

- **不接受 `confirmed` 字段。**送了也忽略，且**不得**因此把 `confirmed` 置真。
- 成功 → `200`，返回 **CaseView**（沿用处置端点的返回形状）。
- 已 `resolved` 的事项 → `409`（终态不可再安排）。
- `expected_revision` 不匹配 → `409`。

#### 4.6.2 取消

```
POST /v1/safety-cases/{case_id}/follow-up
```

同一路径，`action: "cancel"`：

| 字段 | 类型 | 必填 |
|---|---|---|
| `key` | `string` | 是 |
| `expected_revision` | `int` | 是 |
| `action` | `"cancel"` | 是 |
| `reason` | `string \| null` | 否 |

- 成功 → `200` + CaseView，`follow_up.schedule_state = "cancelled"`。
- 取消**不清空** `at`/`condition`/`owner`/`note`——历史要留着，靠状态表达"已取消"。

#### 4.6.3 确认

```
POST /v1/safety-cases/{case_id}/follow-up/confirmation
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `key` | `string` | 是 | 幂等键 |
| `expected_revision` | `int` | 是 | CAS |
| `note` | `string \| null` | 否 | 确认说明 |

- 产生一条**确认记录**，并使 `confirmed: true`、写入 `confirmed_at` / `confirmed_by`
  / `confirmation_ref`。
- 前置条件：存在一条已安排（`schedule_state ∈ {scheduled, due}`）的安排；
  否则 `409`。**没有安排就没有可确认的东西。**
- 成功 → `200` + CaseView。
- `confirmed_by` 取认证主体，**不接受请求体自称**（与既有 `_actor(request)` 口径一致）。

三个动作都要写 `history`（既有 `history[]` 机制，事件名由 B 定）。

### 4.7 各方的具体交付边界

**B（生产/调度/runtime）**
- 落 `follow_up_runtime.py`（新增文件，B 所有）承载调度状态推进。
- 实现 §4.3 白名单校验、§4.4 时区规范化、§4.5 确认语义、§4.6 三个端点。
- `care_task_id` 关联、`last_triggered_at`/`last_trigger_reason`、`blocked_reason` 由 runtime 写入。
- 必须能让 `schedule_state` 从 `scheduled → due → triggered` 前进，并在失败时进 `blocked`
  且带 `blocked_reason`；**不得**用"永远停在 scheduled"来伪装成功。

**C（展示）**
- 展示 `schedule_state`、`confirmed`（含"已安排未确认"这个中间态）、`last_triggered_at`
  / `last_trigger_reason`、`blocked_reason`。
- 注意 `follow_up.at` 的既有渲染 `labels.ts:148` 硬编码标注 `（UTC）` 并直接
  `slice(0,16)`——在 §4.4 规范化之后这个做法要么修正要么加注释说明。
- 提供 §4.6 三个动作的入口（改期 / 取消 / 确认）。

**D（验收）**
- 重点验收 §4.5：**只给时间不给确认 ⇒ 必须 `confirmed: false`**；存量记录按 false 读。
- 验收 §4.3：未知 `kind` 必须 422，不得静默降级。
- 验收 §4.4：naive 时间必须 422。

---

## 5. 环境约定（每个 worktree 独立）

见 [OWNERSHIP.md](./OWNERSHIP.md) 的"环境与端口"表。三条硬规则：

1. **绝不连接或修改真实患者数据库** `stage0/memory.db`（原工作区，204800 字节，2026-09-05）。
   每个 worktree 用独立的 `STAGE0_DB_PATH` 指向自己 `output/` 下的临时库。
2. 每个 worktree 用独立的测试输出目录，都在各自 `output/parallel/<任务>/` 下。
3. 浏览器开发服务用各自的端口，**不得占用 5173 / 8000**（原工作区在用）。

---

## 6. 明确不在本次范围

- 不运行真实模型（四个任务的实现与离线测试均不调用外部 LLM）。
- 不改 `stage0/read_models.py`、`stage0/product.py`、`stage0/agent.py` 等共享文件。
- 不引入第二套并发控制、第二套版本机制、第二套错误信封。
- 不把 `follow_up` 搬到 care_task 上，不合并 `kind` 与 `schedule_state`。

---

## 7. 范围外变更流程（冻结）

必须修改不属于自己的文件时：

1. **不要直接改。** 在 `docs/parallel-delivery/<你的任务>.md` 里追加一节"接口需求"。
2. 写清四件事：
   - **精确接口需要**：要什么字段/函数/行为，为什么现有形状不够；
   - **建议补丁**：diff 或最小代码片段，指明文件与行；
   - **影响面**：谁会受影响（A/B/C/D 哪几方、哪个端点、哪份 DTO）；
   - **为什么不能在自己的所有权内绕开。**
3. 由**最终集成 Agent** 统一处理，不在并行阶段落地。

理由：四个任务共享同一个基线，任何一方擅自扩权都会让另外三方基于不同的假设开发。
