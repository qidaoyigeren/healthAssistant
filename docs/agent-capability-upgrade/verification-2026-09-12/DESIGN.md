# 设计：可信验收与调查闭环修复 · 2026-09-12

**本轮场景**：对已有的"就诊准备 / 自主调查闭环"做一次**可信性**修复。不改场景、不加能力，只解决一件事——
**现有验收与状态机里有若干检查永远不会失败、若干约束从未被执行、若干账目在异常路径上丢失。**

**一句话目标**：让"通过"这个结论重新具备可证伪性；让批次调用上限成为真上限；让"计划修订"与"解释须有证据"两条闭环真正闭合。

---

## 一、审计结论：本轮复核到的缺陷

全部在改代码前**逐条复现**（读码 + 实测）。已修复的部分不重复实现。

### A. 评分口径（`stage0/agent_evals/run_visitprep.py`）

| # | 缺陷 | 证据 |
|---|---|---|
| A1 | 只有一个 `passed`；报告质量、终态、是否自主、降级后果、执行失败/未采样全部混在一起。`not_sampled` 被记录却从不进入 `summary`。 | `run_visitprep.py:170-184`、`:407-411` |
| A2 | `allowed_terminal_reasons` 被 **12/12** 个任务声明，`run_visitprep.evaluate()` **从不读取**它（只有无关的 `run_eval.py:64` 在读）。预算耗尽与 provider 失败可仅凭报告内容"通过"。 | `visitprep_dev.json` 12 处；`Grep` 全仓确认 |
| A3 | **`must_report_conflict`（12/12）与每一条 `required_report_sections`（12/12）都是不可达检查。** `report_text()` 无条件拼接全部五个标题，而评分只做标题子串匹配 → 恒为真。**实测**：零 claim、`termination_reason='unrecoverable_failure'` 之下五个标题依然全部存在。 | `investigation.py:671-701`；实测见下 |
| A4 | 引用正确性只做"回读过"（read-back），不校验证据是否**支持**该断言。 | `investigation.py:612-635`；`evidence_quality.py:69-103` |
| A5 | 没有负向对照。一个从不失败的评分器不构成证据。 | 全仓无 |

A3 的实测（改代码前跑的）：

```
'不同材料之间的差异'            present=True
'2. 有来源支持的事实'           present=True
'3. 不同材料之间的差异'         present=True
'4. 仍缺少依据的问题'           present=True
'5. 就诊时可以向医生或药师确认什么' present=True

with zero claims/gaps, headings still present = True
```

即：12 个任务 × 2 类检查 = **24 个恒真的检查**。

### B. 调用上限（`run_visitprep.py` + `turn_budget.py`）

| # | 缺陷 | 证据 |
|---|---|---|
| B1 | 批次上限只在**任务之间**判断，且 `spent` 是任务**结束后**从 trace 累加的 → 单个任务可以越界；上限**没有**进入真实请求入口。 | `run_visitprep.py:386-393` |
| B2 | 异常退出时 `run_task` 返回的 outcome **不含** `planner_calls`/`planner_steps` → 已经真实发生的调用记录被丢掉。 | `run_visitprep.py:347-349` |
| B3 | `exhausted()` 用 `calls_attempted - refunded >= call_budget`，而 refund 上限是一个 call_budget → **尝试数可达 2×cap**。批次上限若按"计费调用"记数，就不是硬上限。 | `turn_budget.py:181-188`、`:263`；既有测试锁定 `<= 2*2`（`test_planner_reliability.py:552`） |
| B4 | 三个包装器在**没有 budget session** 时短路直连 provider（fail-open）。 | `turn_budget.py:338-341`、`:350-355`、`:371-374` |

**已经正确、本轮不动的部分**：每次发送前 `session.call` 都会 `exhausted()` 检查并**先写持久预留再 dispatch**（`turn_budget.py:236-249`），每次重试都是独立的 `session.call` → 每次重试都各自取额。`llm_attempts` 行写在发送**之前**，进程死也留痕。这一层是本轮要**复用**的基础，不是要重写的东西。

### C. 材料与计划状态（`investigation.py`、`product.py`）

| # | 缺陷 | 证据 |
|---|---|---|
| C1 | **材料药名许可分支是死代码。** `material_refs` 写入的是**字符串**（`"case/item"`），而 `allowed_entities()` 按**字典**读取并取 `entry['candidate']['fields']['name']`；真实索引形状是 `item['fields']['name']`。类型与形状**两处都不符**。唯一覆盖它的测试**手工拼了一个字典**绕开真实数据流。 | `investigation.py:387-389` vs `:193-199`；`product.py:397`；`test_agent_visit_prep.py:332-338` |
| C2 | **残留错误永久阻止完成。** `plan:*` 缺口一经创建**永不解析**；而 `forced_stop()` 的 `checks_completed` 要求"无任何 open 缺口" → 一次被拒后成功的规划会让该轮**只能**以 `budget_insufficient`/`no_progress` 收尾。 | `investigation.py:419-420`、`:528-530` |
| C3 | `MAX_PLAN_ATTEMPTS` 计的是 **distinct gap id 数**（`plan:<错误签名>`），重复同样的错误**永远触发不了**上限；反之三种不同单错会误触发。 | `investigation.py:421`；`gap()` 去重 `:285-290` |
| C4 | **完全没有计划修订。** `plan_questions` 的闸门是首次规划的 `subquestions` 缺口，而 `accept_questions` 成功时正好把它解析掉 → 接受之后**不可能**再修订，与工具自身描述"证据变化时可再次调用以修订"直接矛盾。无修订历史。 | `investigation.py:54-56`、`:716-719`、`:280-282`；`default_tools.py:179` |
| C5 | `accept_questions` 清 `claims` 但**不清** `assessments` → 按 `digest(entities)` 复用同一 claim_id 时**静默继承**旧评估。 | `investigation.py:276` vs `:207` |
| C6 | `accept_questions` 做的是 `self.claims = []` **整体清空** → 一次修订可以**直接丢掉**尚未解决的实体集，其证据缺口随之消失。这正是"通过删除未解决问题伪造完成"。 | `investigation.py:276` |

### D. 解释与证据（`investigation.py`、`evidence_quality.py`）

| # | 缺陷 | 证据 |
|---|---|---|
| D1 | `verify_statements()` 只判"引用是否回读过"，不判"证据是否支持断言"。 | `investigation.py:612-635` |
| D2 | 第 2 节标题是"**有来源支持的事实**"，谓词却是 `status != 'insufficient'` → `contradicted` 的断言也渲染在"支持的事实"之下。 | `investigation.py:676-677` |
| D3 | 模型提出的**子问题**会变成 claim，一旦被判 supported 就以**事实**身份出现在第 2 节。调查问题被当作事实输出。 | `investigation.py:277-278`、`:676-684` |
| D4 | 没有日期/剂量/材料字段的**对应字段核对**；`dose` 只用于开缺口，从未比对。 | `investigation.py:336-337`；全仓无 dose 比对 |
| D5 | 第 5 节的问题列表只由**降级路径**写入 → 模型自己提的澄清问题从不出现。 | `investigation.py:566`（唯一写入点） |

---

## 二、设计决定

| # | 决定 | 依据 |
|---|---|---|
| 1 | 评分协议抽成独立模块 `stage0/agent_evals/scoring.py`（`visitprep-eval@2`），而不是原地改 `evaluate()` | 必须能**脱离重跑**重评历史产物；也必须能被负向对照单测直接调用 |
| 2 | 拆成三条**独立轴**（report_quality / terminal_state / autonomy）+ 五项互斥计数 | 任务书 §一.1；单值 `passed` 无法区分"报告好看但预算耗尽" |
| 3 | 冲突检查改为解析第 3 节**正文**并绑定**真实分歧**（缺口 + `material_ref` + `kind`），要求具名双方 | 任务书 §一.3；修 A3 |
| 4 | 批次额度以**持久账本 `llm_attempts` 的行数**为权威口径，不另维护可加减余额 | 只增计数器是本仓的既有不变式（`merge_budget` 对 `COUNTERS` 取 `max()`，`turn_budget.py:73`）；账本行写在 dispatch **之前**，异常/崩溃也保留 |
| 5 | 批次约束下**关闭 refusal refund**（`PLANNER_PROVIDER_REFUND_REFUSALS=0`） | 让 `call_budget` 成为**发送尝试**的硬上限，而不是计费调用的上限（修 B3） |
| 6 | 材料条目只保留**一种规范形状**，写入端与读取端共用 | 修 C1；并把手工拼装的测试换成全链路验证 |
| 7 | `accept_questions` 成功时**一并解析 `plan:*` 缺口**，尝试计数改为真实计数器 | 修 C2、C3 |
| 8 | 新增**追加式** `plan_revisions`；`plan_questions` 闸门改为"存在修订触发条件" | 修 C4；修订历史与仍有效证据都要留痕 |
| 9 | 一次修订**不得删除**仍带开放 `evidence_missing`/`evidence_conflict` 的实体集 | 修 C6；这是"不得伪造完成"的唯一强制点 |
| 10 | 新增命名 scope `claim-support@1`，**不改** `conservative-lexical-v1` | 让历史 assessments 不集体失效；本仓按命名 scope 管理口径 |
| 11 | 三类角色显式分开：调查问题 / 待验证断言 / 有证据支持的结论 | 任务书 §四；修 D2、D3 |
| 12 | 持久化字段**只增不改**，不 bump `VERSION`/`CONTRACT` | 沿用上一轮决定：`restore()` 对版本不匹配一律抛错，bump 会制造迁移悬崖 |

### 决定 4 的展开：为什么用账本行数而不是余额

本仓的预算语义是**只增不减**：`merge_budget()` 对 `COUNTERS` 取 `max()`，任何递减都会被 checkpoint 恢复撤销（这正是"429 用偏移量而非递减"的由来）。批次额度如果实现成一个自己维护、可加减的余额变量，就等于在预算体系之外重新引入一个非单调状态，正是这条不变式要防的东西。

因此批次口径 = `SELECT COUNT(*) FROM llm_attempts WHERE run_id=?`。该行由 `reserve_llm_attempt()` 在 **dispatch 之前**写入，因此：

- 异常退出、进程被杀 → 行仍在，"已发生的调用记录"不丢（修 B2）；
- 计数只增 → 与 `merge_budget` 的契约一致；
- 它记的是**尝试**而非计费调用 → 与决定 5 配套后成为真上限。

---

## 三、评分协议 v2 规格（`visitprep-eval@2`）

### 三条轴

```
report_quality   — 内容规则（见下）
terminal_state   — termination_reason ∈ allowed_terminal_reasons，且分类为：
                     completed  : checks_completed
                     waiting    : waiting_review / waiting_input
                     stopped    : budget_insufficient / no_progress / cancelled /
                                  unrecoverable_failure
                   只有 completed 与 waiting 可计"完整完成"。
autonomy         — degraded_reason 为空 且 subquestion_source == 'model'
                   且 attribution.fallback == 0
```

### 五项互斥计数（取代单值 `passed`）

五项**互斥**，一个任务只落其一；`undetermined` 是与它们并列的**独立标记**，不是第六项。

| 计数 | 定义 |
|---|---|
| `report_quality_pass` | 内容规则全过 |
| `terminal_expected` | `termination_reason` 在任务预声明集合内 |
| `autonomous_without_degradation` | `report_quality_pass` 且 `terminal_expected` 且 `autonomy` 且分类为 `completed` |
| `degraded_outcome` | 发生了产品降级（`degraded_reason` 非空或 `subquestion_source != 'model'`）时的结果，单独记账 |
| `execution_failed_or_not_sampled` | 执行异常，或因额度耗尽未采样 |
| `undetermined`（并列标记） | 历史产物缺字段 → 该任务整体记不可判定，**不补造证据**，也不计入上述五项 |

### 冲突检查（修 A3）

不再做标题子串匹配。规则：

1. 解析第 3 节标题之后的正文条目；
2. 至少一条**非占位**条目（占位句是"本次未在已读取的材料与记录之间发现可记录的差异…"，须被识别为"无内容"）；
3. 该条目必须对应一次**真实分歧**：存在 `material_conflict` 或 `evidence_conflict` 缺口，且能取出其 `material_ref` 与 `kind`；`kind == 'same'` 不算分歧；
4. 条目须**具名双方**：材料条目 ref **与**它所对比的当前记录 ref。

> **配套改动（否则第 4 条同样不可满足）**：现有 `material_conflict` 缺口的描述是"材料 `<ref>` 与当前记录的差异：`<kind>`"，只点名了材料一侧，**没有**点名当前记录。因此第 3 节的渲染必须一并改为具名双方——从条目的 `current`（当前记录 ref 列表）取出对方 ref 写进该行。缺了这一步，新检查会从"恒真"变成"恒假"，仍然是不可证伪的。

### 引用正确性（在 read-back 之上，修 A4）

一条断言可作结论，需同时满足：

1. 其引用的证据体**已回读**（既有规则，保留）；
2. 引用体**支持**该断言（新 scope `claim-support@1`，见第六节）。

### 负向对照（`stage0/test_visitprep_scoring.py`）

每条**必须判失败**，否则该检查仍是不可证伪的：

| 对照 | 期望 |
|---|---|
| 有标题但正文为占位句 | `report_quality` 失败 |
| 正文明确否认实际存在的分歧 | 冲突检查失败 |
| 任务执行失败，但报告章节齐全 | `terminal_state`/`execution_failed` 失败 |
| 规则接管后完成（`subquestion_source='code_default'`） | `autonomy` 失败 |
| 引用真实存在且已回读，但不支持该断言 | 引用正确性失败 |
| `termination_reason='budget_insufficient'` 但报告完美 | 不得计为"完整完成" |

### 历史重评

`rescoring.py`（或 `scoring.py` 内的纯函数）读既有产物 → 输出到 `output/verification-2026-09-12/rescored/<原名>.json`，带：

```json
{"rescored_by": "visitprep-eval@2", "original_protocol": "<产物自报>",
 "undetermined": ["缺失字段路径..."], "not_comparable_to": "<原因>"}
```

**不覆盖**任何旧文件，不改写旧结论；缺字段一律记 `undetermined`。

---

## 四、调用上限规格

### 额度传递

`run_visitprep` 持有批次额度：

1. **任务前**：`remaining = cap - spent`；`remaining <= 0` → 该任务记 `not_sampled`，不空跑；
2. 以 `remaining` 作为该任务 `AGENT_TURN_CALL_BUDGET`；真实请求入口 `session.call` 在**每次发送前**取额（含每次重试）；
3. 批次约束下关闭 refusal refund，使 `call_budget` 是**尝试**的硬上限。

### 入账（修 B1、B2）

任务结束**在 `finally` 里**从持久账本读取该 run 的实际尝试数并入账：

```
spent += count(llm_attempts where run_id = <this task run>)
```

这取代当前"任务结束后从 trace 累加 `planner_calls`"的写法。异常路径同样执行，因此已发生的调用不会丢。

### 边界测试（`stage0/test_visitprep_budget.py`）

| 场景 | 期望 |
|---|---|
| `cap=1`，任务需要多次调用 | 发送尝试数 ≤ 1，其余记 `not_sampled` |
| 多次 429 重试 | 尝试数不因 refund 越过批次上限 |
| 跨任务共享额度 | 第二、三任务拿到的是**递减后**的剩余额度 |
| 任务中途抛异常 | 账本仍记录已发生的调用，且计入 `spent` |
| 额度恰好用尽 | 剩余任务 `not_sampled`，不发起任何请求 |

---

## 五、材料与计划状态规格

### C1 · 统一材料引用与候选实体

- 规范形状：材料条目即 `MaterialIndex.index()` 的 item（`item['fields']['name']`）。
- `observe()` 写入 `material_refs` 时同步记录**可从同一条目解析出的药名**；
- `allowed_entities()` 从**同一形状**读取，删除现在这段读字典的分支。
- 全链路测试（替换 `test_agent_visit_prep.py:332-338` 的手工拼装）：
  **导入 CSV → `list_materials` → 模型提出含材料独有药名的子问题 → 被接受**。
  用 `vp-distract-006a` 的材料（`维生素D`、`钙片` 不在权威药单内）作为真实数据。

### C2 / C3 · 关闭残留缺口 + 真实尝试计数

- `accept_questions` 成功时：解析 `subquestions` **以及全部 `plan:*`** 缺口（它们已被这次成功修订取代）。
- 新增 `plan_attempts: int` 计数器；`MAX_PLAN_ATTEMPTS` 改读它。上限语义变为"**连续失败**次数"，一次成功即归零。

### C4 · 有界修订

新增追加式历史：

```python
plan_revisions: list  # [{revision, cycle, trigger, before:[entities...], after:[...], retained:[claim_id...]}]
```

`plan_questions` 的闸门从"`subquestions` 缺口是否打开"改为**存在修订触发条件**：

- 首次规划（`subquestions` 缺口打开）；或
- 相对当前 claim 集出现了**新证据**：新回读的材料差异（新 `material_conflict` 缺口）、或存在未被任何 claim 覆盖的开放 `evidence_missing`/`evidence_conflict`。

修订上限有界，且与"被拒次数"是**两个不同的界**，不要复用同一个常量：

| 界 | 常量 | 含义 | 触发后果 |
|---|---|---|---|
| 被拒次数 | `MAX_PLAN_ATTEMPTS`（既有，改读 `plan_attempts` 计数器） | **连续**被拒的声明次数 | 达上限 → `termination_reason = 'no_progress'` |
| 修订次数 | `MAX_PLAN_REVISIONS`（新增） | 一次调查内**成功**采用的修订轮数 | 达上限 → 仍可作为首次规划提交，但不再接受**新增**修订；记 `plan_revision_capped` 缺口（可读、非失败） |

两者互不干扰：反复被拒不会消耗修订额度，成功修订会重置被拒计数。

### C6 · 防伪造完成

一次修订**被拒绝**，当它试图丢弃的实体集仍满足：

- 该实体集对应的 claim 仍带开放 `evidence_missing` 缺口；或
- 该实体集参与一个开放的 `evidence_conflict`。

拒绝时给出**约束式**反馈（沿用 `correction_for` 的既有风格：只给约束、不给现成动作）。允许的修订是**增加**或**改写措辞**，不是抹掉未决项。

### C5 · 证据保留

- 修订后按 `claim_id` 复用仍有效的 assessments，并在 `plan_revisions[].retained` 显式记录保留了哪些；
- 被丢弃的实体集若其 assessments 不再被任何 claim 引用，显式记录失效原因；
- 修掉"清 `claims` 不清 `assessments`"的静默继承：复用必须**显式**，不靠 claim_id 撞上。

---

## 六、解释与证据校验规格（`claim-support@1`）

### 三类角色分开

| 角色 | 来源 | 可出现在 |
|---|---|---|
| **调查问题** | 模型 `plan_questions` 的 statement；澄清问题 | 第 5 节（"可以向医生确认什么"） |
| **待验证断言** | `verify_statements()` 判定的 `pending_statements` | 第 4 节（"仍缺少依据，待核实"） |
| **有证据支持的结论** | 拿到正向支持的 claim | 第 2 节（"有来源支持的事实"） |

关键修复：

- 第 2 节谓词改为 `status == 'supported'`（修 D2）；
- 子问题**不再**以事实身份渲染：只有其断言获得证据体正向支持才进第 2 节，且渲染行**带证据 ref**（修 D3）；
- 第 5 节改为同时呈现模型的澄清问题（当前只有降级路径会写）（修 D5）。

### `claim-support@1` 的检查

在既有 read-back 之上追加：

1. **结构化事实按字段核对**：材料差异类断言，须与其材料条目 `kind` 及对应字段（`dose`/`unit`/`date`）一致。仅"药名相同"或"读过引用"不构成支持。
2. **自由文本须有具体片段**：断言的关键词/实体须在**引用体正文**中逐字出现（沿用并收紧既有 quote-substring 思路），否则不成立。
3. 不满足 → 该断言进入 `pending_statements`（保持为**待确认项**），**不**用免责声明替代证据校验。

### 落地位置与状态语义（避免两套判定打架）

`conservative-lexical-v1` 的既有行为**不改**，`claim-support@1` 作为独立 scope **叠加**，落在**同一个判定链路**上、但记在**独立字段**：

- `assess_claim()` 仍产出 `status`（`supported`/`contradicted`/`insufficient`）→ 决定 `_assess()` 怎么算 `supporting_evidence`/`opposing_evidence`（既有语义，不动）。
- `claim-support@1` 产出新的 `support_status` 维度（`supported_by_span` / `field_mismatch` / `no_span` / `not_applicable`），写入 assessment 的独立键，**不覆盖** `status`。
- **结论区（第 2 节）的谓词改为两者同时成立**：`status == 'supported'` **且** `support_status == 'supported_by_span'`。
- 判定不成立时 → 该断言进 `pending_statements`，在第 4 节作为待确认项呈现；**不**降级成免责声明。

这样历史 assessments 不会因新 scope 失效（`attempts` 里旧记录缺该键 → 视作 `not_applicable`，并计入 `undetermined` 而非"不支持"），也不会出现"两套 support 互相矛盾"。

---

## 七、受控验证方案

**顺序固定，前一关不过不进下一关。**

1. **离线回归**：新增单测（评分负向对照、预算边界、材料全链路、修订与防伪造、证据 support）+ 既有全量（354 条 + 上一轮 48 条）。
2. **历史重评**：用 `visitprep-eval@2` 重评 `output/visit-prep-2026-09-12/*.json`，输出新文件，带 `undetermined` 与 `not_comparable_to`，并给出**新旧评分对照表**。
3. **live（受控）**：
   - 主臂：**tokendance / glm-5.3-flash**（记录中 0 限流、100% 响应；代价 ~72s/回合）；
   - 对照：**siliconflow** 小批（标注其限流状态）；
   - 预设：批次额度、**请求间隔**、单次超时、取消机制；
   - 逐次记录请求与重试；**持续限流即停止扩量**，不靠加重试次数抬高通过率。
4. **浏览器验收**（与模型质量验收**分开报告**）：`/materials` 上传 → `/tasks` 发起调查 → 补问或部分结果 → 报告引用与降级展示。

**两个必须观察的行为**（找不到就明确写未达标）：

- 首次检索无结果后**合理改写查询**；
- 新证据出现后**修订调查计划及最终结论**。

**对照要求**：固定流程对照臂必须具备**相同的材料访问能力**（否则"材料可见性"仍是混淆变量）；脚本替身**只能**证明执行契约可满足，不得记作具备规划能力。

---

## 八、风险与可比性声明

| 风险 | 处置 |
|---|---|
| 修订闸门一开，模型可见契约即改变 | 协议版本一并升；**不**拿新批次与 `live-model-3` 逐格比较，只并排呈现并注明不可比 |
| 第 6 节收紧后通过率大概率下降 | 这是口径变严的**真实结果**，不当作回归，也不回退口径 |
| 限流仍是绑定性变量 | 主臂换到记录中 0 限流的端点；siliconflow 只做小批对照；持续限流即停 |
| 12 个任务是自写开发集 | 沿用既有口径声明，不构成泛化主张；独立 held-out 仍缺失 |
| 批次额度按尝试计数会更快耗尽 | 这是**刻意**的：上限是硬上限，不是"计费调用上限" |

---

## 九、口径声明

- 12 个任务是**合成开发集**，作者自写、参与调试，**不是**独立 held-out。
- 成对任务的 `goal` 与 `initial_state` 逐字一致，只改证据；由测试锁定。
- 评分规则先定后跑，写在任务的 `expected` 里；评分器**不识别**任何具体 `task_id`、族名或文件名。
- 工具调用次数与路径相似度**不计入**成功指标；合法的不同顺序不判失败。
- 所有尝试（含失败、崩溃、`no_progress`、429、未采样）都进入统计，不剔除任何一轮。
- 旧结论**不覆盖**；重评产物另存并标注 `rescored_by` 与 `not_comparable_to`。
- 本轮**不新增**多 Agent、不做框架迁移、不改产品默认开关（`AGENT_INVESTIGATION_ENABLED` 保持关闭）。
