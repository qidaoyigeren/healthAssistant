# 任务 C — 安全体验（前端）

**分支**：`codex/safety-experience` ｜ **worktree**：`D:\py\HealthAssistant.worktrees\safety-experience`
**接口**：[CONTRACT.md §3.6 / §4.7](./CONTRACT.md) ｜ **所有权**：[OWNERSHIP.md §1](./OWNERSHIP.md)

---

## 目标

把 A 产出的答案可信性和 B 产出的长期跟进状态，变成照护者能读懂、且**读得准确**的界面。

## 你拥有的文件

- `frontend/src/features/safety/`（整目录）
- `frontend/src/api/client.ts`、`frontend/src/api/types.ts`、`frontend/src/api/queryKeys.ts`

其余文件一律只读冻结——**包括 `frontend/vite.config.ts`**（见下）。

## 要做的两件事

### 1. 答案可信性展示（[CONTRACT.md §3](./CONTRACT.md)）

- `types.ts:664-670` 的 `answered_parts` 元素类型目前只声明 5 个键
  （`value/field/source/provenance/still_uncertain`），后端实际下发 11 个。
  **补上 `assessment`。**
- 必须区分 **5 个视觉状态**：4 个 `status`（`verified`/`candidate`/`stale`/`unsupported`）
  **加上"未核实"**（无 `assessment`）。缺 assessment ≠ verified。
- 文案必须体现含义边界：`verified` **只**表示"约定范围内的答案依据已核对"，
  不能渲染成"安全""已确认无误""可以放心"。

### 2. 长期跟进展示与操作（[CONTRACT.md §4](./CONTRACT.md)）

- 展示 `schedule_state`、`confirmed`、`last_triggered_at`/`last_trigger_reason`、`blocked_reason`。
- **要有一个清楚的"已安排但未确认"中间态**——这是本次要建立的关键区分。
- 提供三个动作入口：改期（`schedule`）/ 取消（`cancel`）/ 确认（`confirmation`）。
- `labels.ts:148` 现在硬编码 `（UTC）` 且 `at.slice(0,16)`；服务端规范化 `at` 之后，
  这个渲染要么修正、要么加注释说明为什么仍成立。

## 起点提示

`frontend/src/features/safety/` 下已有 `SafetyPage.tsx`、`SafetyCaseDetailPage.tsx`、
`CaseCard.tsx`、`labels.ts`。`SafetyCaseDetailPage.tsx` 在 `:380-433` 已经在渲染
`information_state` / `answered_parts` / `still_uncertain`，`:917-931` 在构造
`follow_up` 请求体——**从这两处入手**，不要新起一套组件树。

## 端口与后端

不要改 `vite.config.ts`，它已经支持环境变量：

```bash
cd frontend
STAGE0_DEV_API_TARGET="http://127.0.0.1:8103" npx vite --port 5203
```

## 验收标准

- 无 `assessment` 的答案显示为"未核实"，**不是** verified。
- 四种 `status` 与"未核实"在视觉上可区分。
- "已安排未确认"与"已确认"在视觉上可区分。
- 三个跟进动作能发出正确请求并处理 409（revision 冲突）。
- 不修改所有权外文件。

## 需要越界时

走 [CONTRACT.md §7](./CONTRACT.md)：在本文档追加"接口需求"一节，不要直接改别人的文件。

---
---

# 交付记录（Agent C）

基线 `01d208d`（含冻结契约）。**没有越界改动**：未动后端、依赖清单、
共享浏览器脚本、`vite.config.ts`、`queryKeys.ts`。

## 0. 改动清单

| 文件 | 变化 | 说明 |
|---|---|---|
| `frontend/src/api/types.ts` | 修改 | 新增 `SafetyAnswerAssessmentDto` / `SafetyAnsweredPartDto` / `SafetyFollowUpConditionDto` / `SafetyFollowUpCommandDto` / `SafetyFollowUpConfirmationDto`；按 §4.2 扩展 `SafetyFollowUpDto` |
| `frontend/src/api/client.ts` | 修改 | 新增三个跟进写端点 |
| `frontend/src/features/safety/labels.ts` | 修改 | 核验状态词汇与含义、调度状态、触发条件、时间渲染修正、执行任务状态 |
| `frontend/src/features/safety/assessment.tsx` | **新增** | 一条答案 + 它的依据；来源的授权入口 |
| `frontend/src/features/safety/FollowUpPanel.tsx` | **新增** | 跟进安排展示 + 三个动作 |
| `frontend/src/features/safety/fixtureBridge.ts` | **新增** | 开发期 fixture 的唯一开关点（读 3 个 / 写 3 个） |
| `frontend/src/features/safety/devFixture.ts` | **新增** | 合成数据与场景，**只在开发期经动态 import 加载** |
| `frontend/src/features/safety/SafetyCaseDetailPage.tsx` | 修改 | 接入以上；折叠区与核心区的划分保持不变 |

`queryKeys.ts` **未改**：跟进状态随 `SafetyCaseDto` 一起下发，不需要新的查询键。

## 1. 界面变化

### 1.1 信息组织（沿用现有五步结构，未重排、未新增页面）

第 3 步「仍有什么不确定」里，**已经查清的部分**现在逐条给出答案的依据
（见 §1.2）。第 4 步之后插入一张并列的「下一次跟进」卡（与「处置这件事」同级），
回答"下一次跟进安排与触发原因"。第 4 步原先把整段跟进文案内联渲染，
现在只留一行指路，正文与操作都在新卡里，避免同一段话说两遍。

模型轨迹、token、工具调用**仍然**收在第 2 步的 `<details>「调查过程明细」` 里；
浏览器验收专门断言了这一点，也断言了跟进操作**不在**折叠区内。

### 1.2 答案可信性（§3）

每个答案元素渲染：`已经拿到：值（字段）` → 核验状态徽标 → 来源属性 →
**一句"这条依据到底是什么"** → `verified` 的含义边界 → 原文定位 → 来源引用与授权入口。

**5 个视觉状态**，就是"4 个 `status` + 未核实"。徽标**文案**两两不同；
配色分四档，其中 `candidate` 与 `unsupported` 同档（都是 caution）——它们靠文案区分，
不靠颜色单独表义（沿用本项目既有做法）。外部可经 `data-assessment-status` 读到
（`unassessed` = 缺 `assessment`），断言不依赖文案措辞：

| # | 视觉状态 | 徽标 | 配色 | 触发条件 |
|---|---|---|---|---|
| 1 | 依据已在约定范围内核对 | 依据已核对 | primary | `status = verified` |
| 2 | 有候选依据，核对未完成 | 候选依据，尚未核对完 | caution | `status = candidate` |
| 3 | 来源已变化，需要重新核对 | 来源已变化，需要重新核对 | danger | `status = stale` |
| 4 | 没有可支撑的依据 | 没有可支撑的依据 | caution | `status = unsupported` |
| 5 | **未核实** | 未核实（没有核验记录） | neutral | **没有 `assessment` 键** |

任务书要求分清的五种情形，落在**徽标下面的那句读法**上——它由 `status` 与既有
`provenance`（§3.5）合起来得出，不新造词汇：

| 要分清的情形 | 条件 | 那句读法 |
|---|---|---|
| 与当前有效记录一致 | `verified` + `authoritative_record` | 与当前有效记录一致 |
| 来源原文记载 | `verified` + `reference_evidence` | 来源原文记载（已核对） |
| （补充）与上传材料一致 | `verified` + `material_record` | 与上传材料的记载一致（材料本身未经核实） |
| 用户提供、待确认 | `candidate` + `user_reported` | 用户提供、待确认 |
| 模型解释候选 | `candidate`（其余） | 模型解释候选（依据尚未核对完） |
| 来源变化，需要重新核对 | `stale` | 来源变化，需要重新核对 |
| （补充）没有依据 | `unsupported` | 没有可支撑的依据 |
| （补充）未核实 | 缺 `assessment` | 未核实：这条答案没有核验记录 |

- 缺 `assessment` 一律显示「未核实（没有核验记录）」，并在正文里写明
  **"没有核验记录不等于已经核对通过，也不能按已验证理解"**。硬约束，有断言。
- `verified` 旁边固定出现边界句：*"「依据已核对」只说明这条答案所依据的材料在约定范围内
  核对过。它不表示用药安全，不表示风险已经排除，也不是医生或药师的判断。"*
  断言了"verified 的徽标文本里不含『安全』"。
- `stale` 会单独列出它依赖的版本，并说明这些版本已不是当前版本。

**来源与原文定位只走授权入口**（硬要求，所以做成了数据驱动 + 有断言）：

- `source_ref` 前缀是 `ev-`（服务端 `_reference_is_visible` 认可的内容寻址证据 id）
  → 复用既有 `EvidenceDrawer` → `GET /v1/evidence/{id}`（服务端按认证作用域解析、复验哈希）。
- 前缀是 `memory:` → 复用既有 `MemoryRefDrawer` → `GET /v1/memory/item?ref=…`。
- **其他任何形态一律不发起请求**，也不显示读取按钮，只显示
  "这个引用形态没有对应的授权读取入口，应用内不读取，也不替它拼地址"。
  验收里放了一条 `evidence:8842`（契约 §3.2 的示例就长这样）专门测这条路径。

### 1.3 长期跟进（§4）

**第一行就是本轮要建立的那个区分**，挂 `data-follow-up-confirmed`：

- `已安排，尚未确认`（caution）—— 有 `schedule_state ∈ {scheduled, due}` 但**没有确认记录**；
- `已确认`（primary）—— `confirmed === true` **且** `confirmed_at` 与 `confirmation_ref` 都非空；
- `待确认的安排`（caution）—— 其余。

**存量记录按未确认读（§4.5）**：`confirmed: true` 但没有确认时间/确认记录时，
界面按未确认显示，并写明"这条记录自称已确认，但缺少确认时间或确认记录，因此按未确认显示"。
契约要求的这一次可见回退，在界面上是显式的，不是悄悄降级。

其余展示：`kind`（安排种类）与 `schedule_state`（走到哪了）**分开两个徽标**；
`owner` 区分**个人提醒**与**专业复核安排**（后者必须带"本项目未连接真实医护服务"）；
`last_triggered_at` / `last_trigger_reason`、`blocked_reason`、`care_task_id` 与关联执行任务状态
（到期 / 排队 / 等待用户 / 执行失败各有各的说法，都取自服务端字段）。

**三个动作**（`schedule` / `confirm` / `cancel`）：

- 请求体**从不包含 `confirmed`**；`expected_revision` 用当前 `view.revision`；
  同一个 (动作, 内容) 复用同一个幂等键。
- **确认**被前置条件挡住：没有 `scheduled|due` 的安排时按钮禁用并说明原因，
  不让用户去撞一次必然 409 的请求（与既有"关闭按钮"同一种做法）。
- **取消**两步确认；文案写明取消不清空时间与条件。
- 提交期间 `busy` 禁用重复提交；**失败不清空输入**；409 时额外给出
  "刷新这件事项（保留我已填的内容）"入口。
- 事项是 `resolved` 时不显示任何动作，并说明终态不可再安排。

### 1.4 时间渲染的修正（任务书点名的 `labels.ts:148`）

原来 `followUp.at.slice(0, 16) + （UTC）` 把**截出来的字符串**当成 UTC 显示；
遇到毫秒 + `Z` 或别的偏移量就会显示错时刻。现在 `followUpAtText()` 从解析出的
时刻算 UTC 墙钟：`+00:00`、`Z`、`+08:00` 都显示成同一个时刻；解析不了就原样显示并标注。
（这里选择**修正**而不是只加注释：注释不能防止显示错的时间。）

### 1.5 处置面板的两处收敛

- **不再发送 `follow_up`**。原来它把自由文本当触发条件发出去——按 §4.3 那会被 422；
  而且 §4.5 之后"给了时间 ⇒ `confirmed: true`"不再成立，那条路径会继续暗示
  "填了时间就是确认过了"。时间与条件统一改到「下一次跟进」里排。
  文案改成："这一步只记下「持续跟进」这个处置，不会同时排一个复核时间"。
- 删掉了本文件里重复的 `CARE_TASK_STATUS_LABELS`，改从 `labels.ts` 取
  （同一份文案只有一个出处）。

## 2. API 对接点

| 冻结端点 | 客户端方法 | 界面入口 |
|---|---|---|
| `POST /v1/safety-cases/{id}/follow-up`（`action: "schedule"`） | `api.safetyCaseFollowUpSchedule` | 「下一次跟进」→ 安排 / 改期 |
| `POST /v1/safety-cases/{id}/follow-up`（`action: "cancel"`） | `api.safetyCaseFollowUpCancel` | 「下一次跟进」→ 取消 |
| `POST /v1/safety-cases/{id}/follow-up/confirmation` | `api.safetyCaseFollowUpConfirmation` | 「下一次跟进」→ 确认 |

读取侧未新增端点：`assessment` 随 `answered_parts` 下发，跟进状态随 `SafetyCaseDto.follow_up` 下发。

**未新增**第二套并发控制、版本机制或错误信封——沿用 `key` + `expected_revision`（§1.2），
错误走既有 `ApiError` 与 `serverMessage()`（服务端原话原样显示）。

## 3. 实际验证

### 3.1 类型检查与生产构建

```
cd frontend && npx tsc -b --noEmit      → 0 error
cd frontend && npx vite build           → 成功
```

### 3.2 fixture 不进生产请求路径（可复现的证据）

`fixtureBridge.ts` 每个函数的形式都是
`if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') { await import('./devFixture'); … }`，
判断在**发起请求之前**，所以 fixture 只可能**替换**请求，不可能在真实失败之后兜底。

生产构建产物 `dist/assets/` 里只有一个 JS chunk，并按标记串逐条 grep 确认
**全部缺席**：`fixture-case-0001`、`合成药甲`、`fixtureSchedule`、`fixture-trace`、
`devFixture`、`fixture 只提供`。动态 import 的 chunk 被整体删除，
不是"打包了但走不到"。

### 3.3 浏览器验收 —— fixture 场景（37/37 通过）

驱动：`output/parallel/safety-experience/browser-check.js`（放 `output/` 下，
`.gitignore` 覆盖，不进提交）。不带后端起 Vite（`VITE_SAFETY_FIXTURE=1`，5203 端口），
真实 Chromium。

覆盖：五个核验状态各自出现且互不合并；缺 assessment 显示"未核实"；
verified 徽标不含"安全"且边界句在；`evidence:8842` 这种无授权入口的引用
**不显示按钮、不发请求**；点开原文只请求 `/v1/evidence/ev-fixture0000000001`
（断言了实际发出的 URL），无后端时如实报"证据原文不可用"；
初始"已安排未确认"→ 确认后变"已确认"；**改期后仍是未确认**；
取消后调度状态为"已取消"**且时间仍保留**；版本冲突（`?fx=conflict`）显示服务端原话 +
刷新入口 + **输入保留**；提交失败（`?fx=failure`）输入仍在；
存量记录（`?fx=legacy`）`confirmed=true` 无确认记录 → 按未确认显示且说明原因，
历史答案全部按未核实；空数据（`?fx=empty`）如实说明没有安排且不崩；
窄屏 375px / 414px 无横向溢出；工具明细在 `<details>` 里、跟进操作不在折叠区。

产物：`output/parallel/safety-experience/`（截图、`browser-check.json`、`vite.log`）。

### 3.4 浏览器验收 —— 真实后端（12/12 通过）

用**既有的**共享脚本 `scripts/safety-mainline-browser-acceptance.js`（**未修改**），
指向本 worktree 的真实合成后端（`stage0.safety_browser_fixture`，不调模型）：

```
node scripts/safety-mainline-browser-acceptance.js \
  --api-port 8103 --ui-port 5203 \
  --out output/parallel/safety-experience/real-backend-browser
→ status pass（12/12 步通过）
```

这一轮证明：**fixture 关闭时**页面在真实后端上照常工作（看事项 → 回答补问 → 看到变化），
并且基线后端不下发 `assessment` 时，那条答案如实显示为「未核实（没有核验记录）」。

> 一处**由这次验收抓到的真回归**：把内联的 `已经拿到：…` 换成组件后丢了这个前缀，
> 该脚本第 12 步（"页面说明这条回答补上了什么、来源属性与仍不确定的部分"）立刻失败。
> 已修回。这条断言是既有验收里有写下的要求，不是可以顺手改掉的文案。

> 复现前提：worktree 里没有 `.venv/` 与 `node_modules/`（都是忽略项）。本次用目录联接
> 把主仓库的这两份指过来（`mklink /J`，均被 `.gitignore` 覆盖，未进入提交）。
> 不建联接时 §3.3 与 §3.4 的脚本分别找不到 playwright。

### 3.5 没有做的

**没有调用任何真实模型。** 真实后端那一轮跑的是 `MEMORY_ENABLE_LLM=0` 的合成宿主。

## 4. 未联调项（依赖 A 与 B 落地后才能确认的部分）

1. **三个写端点在真实后端上全部未联调**——基线 `safety_cases.py` 里没有
   `/follow-up` 与 `/follow-up/confirmation`，B 落地前它们只会 404。
   fixture 里验的是**请求形状与界面行为**，不是服务端语义。
2. `assessment` 本身未在真实数据上出现过（A 尚未落地）。"真实后端"那一轮
   验的是**缺失路径**，不是四个 `status` 的真实取值。
3. `schedule_state` 的推进（`scheduled → due → triggered` / `blocked`）由 B 的
   runtime 写入，界面只是展示；这些状态在 fixture 里是手写的合成值。
4. `follow_up.condition` 是白名单对象（§4.3）。类型里**同时接受 `string`**，
   因为存量记录可能还存着收紧前的自由文本；界面遇到字符串会明确标注它是旧写法，
   不解析、不执行、也不当成可用的触发条件。B 落地后应确认存量数据里是否真有这种值。
5. 处置端点是否也按 §4.3/§4.4 收紧 `follow_up` 入参，**契约未明说**。
   C 这边已经不再经处置端点发送 `follow_up`，所以两种口径都不会踩到；
   但 D 验收时应确认这一点，因为它决定存量数据里会不会出现自由文本条件。

## 5. 接口需求（CONTRACT.md §7）

**不需要任何越界改动。** 但有一处**契约内部不一致**，需要最终集成 Agent 定夺：

> **精确接口需要**：`assessment.source_ref`（以及答案既有 `source_ref`）的**取值形态**。
>
> **冲突**：CONTRACT.md §3.2 的示例写的是 `"source_ref": "evidence:8842"`；
> 而服务端做作用域校验的 `investigation.py:764 _reference_is_visible` 只认
> `memory:` / `ev-` / `safety-case:` / `material:` 四种前缀。
> 也就是说，按 §3.2 示例字面产出的 `evidence:8842` **不会被服务端自己的解析器接受**。
>
> **C 现在的行为**：只有 `ev-` 与 `memory:` 会被当作可读取的授权入口并渲染出按钮；
> `evidence:8842` 这类形态显示成"没有对应的授权读取入口"，**不拼地址、不发请求**。
> 这是能保证不越权的那一边，但它让一条本可回读的证据退化成不可读。
>
> **影响面**：A（产出 `source_ref` 的形态）、C（能不能给出"查看原文"入口）、
> D（按 §3.2 逐字段比对时会撞到这个前缀问题）。
>
> **为什么 C 不能自己绕开**：按前缀拼 `/v1/evidence/{id}` 就是前端绕过作用域校验
> ——正是 §3.6 与本次任务书都禁止的事。所以这里只报告，不改。
>
> **建议**：明确 `source_ref` 一律用 `_reference_is_visible` 认可的前缀（证据用 `ev-`），
> 并把 §3.2 的示例改掉；或者由服务端在投影时把 `evidence:8842` 规范成可解析的
> `ev-…` 形态。两条路 C 都不用改代码。
