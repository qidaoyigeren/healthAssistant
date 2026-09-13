# material-review@2 覆盖保证、补问恢复与完整交付 —— 交付说明（2026-09-13）

设计依据见同目录 [DESIGN.md](DESIGN.md)。本文件回答交付要求里的六件事。

---

## 1. 设计决策摘要

**基础覆盖与模型调查分成两层。** 运行器负责枚举选中的材料与要比较的记录、在既有
权限路径下实际读取、校验完整性、取出字段与来源位置、执行**现有**确定性字段比较，
并给每条覆盖要求一个明确处置（`covered` / `unreadable` / `unmatched` /
`insufficient`）。模型只做调查策略：哪些差异值得解释、还要核对什么、真正必要的
补充是什么、是否查其他来源、怎么组织就诊准备。

**这不会把系统工作冒充成模型能力**，因为三条线不许越过：

* **归因**：读取凭据记 `system_read` / `model_requested_read`；`material_read_refs`
  **只**记模型读过的条目。报告同时给出 `system_material_refs` 与 `model_material_refs`。
* **分层**：系统发现 `origin=system` 是对字段的确定性比对；模型的解释仍走 `verify`
  三档核查，语义档只给"需人工确认"。
* **分账**：`attribution` 里 system / model / unattributed 各列各的。这一版模型一轮
  都没参与时，交付**不允许**是 complete。

**补问是状态转换，不是工具调用。** `request_information` 成功后先把与补问无关的
确定性覆盖跑完，若没有剩下需要判断的事就立即进入 `waiting_input` 并结束本轮——
那正是"模型继续重复提问"的止点。请求身份 = 任务 + 对象 + 待补字段；换个措辞是
同一条，不相交的字段是两条。

**三类输入不互相冒充。** 材料说明 / 用户陈述写进 `state.answers`（**不**改权威
记录）；权威记录更新走既有的受控写入与确认流程，留下原值 → 新值与来源。报告的
"依据"一节把三者分开写。"保存补充"按默认只发前者。

**交付语义收紧。** 有未回答的补充请求、或还有没查完的问题 → `delivery=partial`
（报告本身可以是完整的，但用户要的那件事没交付完）；这一版没有模型参与 → 不得
complete。两者都是非阻塞缺口。

---

## 2. 关键代码位置

### 新增

| 文件 | 职责 |
|---|---|
| [stage0/review/coverage.py](../../../stage0/review/coverage.py) | 基础覆盖 pass：有界批次 + 分页读取、读取凭据、确定性字段比较、处置、系统补问 |
| [stage0/test_material_review_coverage.py](../../../stage0/test_material_review_coverage.py) | 场景 A–H 与错误反例（25 条） |
| [stage0/material_review_browser_fixture.py](../../../stage0/material_review_browser_fixture.py) | 浏览器验收的隔离合成夹具 |
| [scripts/material-review-browser-acceptance.js](../../../scripts/material-review-browser-acceptance.js) · [run-material-review-browser.js](../../../scripts/run-material-review-browser.js) | 浏览器验收与运行器 |
| [scripts/material-review-coverage-live.py](../../../scripts/material-review-coverage-live.py) | 冻结协议的真实模型验收（含 `--rescore`） |

### 改动（都在既有模块内）

| 文件 | 改了什么 |
|---|---|
| [state.py](../../../stage0/review/state.py) | 读取凭据 `source_reads`/`read_credential`/`read_attribution`；`answers`；`coverage_runs`/`coverage_cursor`/`coverage_progress()`；补充请求的语义身份与 `purpose`/`target`；`model_cycles_this_delivery()`；`_display_subjects` |
| [context.py](../../../stage0/review/context.py) | 模型视图里给出 `base_coverage`（实际处理记录）、`user_answers`、`read_by`；待办项标注 `actionable`（system / model / waiting_input） |
| [capabilities.py](../../../stage0/review/capabilities.py) | `read_material` 记模型读取凭据；`request_information` 要求声明对象与用途，说不清就明确拒绝；`research_decisions` 记每次检索服务于哪个问题 |
| [advance.py](../../../stage0/review/advance.py) | 每轮跑基础覆盖（多趟、有界）；`_pause_for_input`；`_attribution`；交付后重算报告第 1 节 |
| [delivery.py](../../../stage0/review/delivery.py) | 依据改为按**读取凭据**判定；新增 `model_not_consulted`、`uncertainty_without_basis`；等待补充 / 未决问题 → partial |
| [report.py](../../../stage0/review/report.py) | 第 1 节的覆盖与归因数字；第 6 节的截断/失败/等待；依据里分清材料 / 当前记录 / 您的补充；自纠过的步骤不进正文 |
| [incremental.py](../../../stage0/review/incremental.py) | 版本失效不再重落发现（交给覆盖 pass 在读过之后做） |
| [care_tasks.py](../../../stage0/care_tasks.py) | `record_input(..., answers=)`；`missing_inputs` 带 `purpose`/`target`/`fields`；任务上给出 `coverage_progress`/`read_attribution`；产物带上归因、覆盖与发现 |
| [MaterialReviewCard.tsx](../../../frontend/src/features/tasks/MaterialReviewCard.tsx) | 覆盖进展、报告状态与任务状态分开、补问表单（对象/字段/用途/部分回答）、材料条目抽屉、修订说明、模型缺席提示 |

复用未改语义：`EvidenceStore`、`ToolExecutor`/权限/钩子、`NoProgressTracker`/取消、
`TurnBudget`、`ProductStore` 回执与事务、`MaterialIndex`、`response_safety`、
`LLMPlanner` 传输层、旧契约与 `reconcile_material`。

---

## 3. 完整浏览器演示

```bash
node scripts/run-material-review-browser.js --out output/material-review-browser
```

真实浏览器 + 隔离合成夹具（`--directory` 限定在 `output/` 下），走的是**构建产物**
而不是 dev server——验收要的是用户打开的那个页面，dev server 的启动与端口只会引入
与产品无关的变量。产物：`acceptance.json` + 6 张截图。

**15/15 项通过**，逐项对应交付要求：

| 检查 | 说明 |
|---|---|
| `isolated_fixture` | 用的是隔离合成夹具，不碰用户数据库 |
| `started_from_tasks_page` | 从真实页面入口发起核对 |
| `coverage_progress_visible` | 覆盖进展显示"已处理 N/M"、"系统读取 X 条 / 模型读取 Y 条" |
| `all_selected_materials_dispositioned` | 选中材料全部有处置，`items_pending == 0` |
| `reads_attributed_to_system` | 系统读取 ≥ 选中条目数（**不依赖模型读不读**） |
| `request_names_object_and_fields` | 补问带着对象、待补字段与用途 |
| `answer_form_says_it_does_not_write` | 表单写明"不会自动改动当前药单" |
| `plain_answer_does_not_write_record` | 提交回答前后**药单逐字相同** |
| `partial_answer_closes_only_that_request` | 只回答一条 → 只有那一条被关闭 |
| `sources_distinguish_system_and_model` | 来源区分"系统读过的"与"模型读回的" |
| `source_jump_shows_both_sides` | 从发现跳到材料条目：双方记录 + 来源位置 |
| `revision_basis_distinguishes_inputs` | 修订依据写明"您的补充…未改变当前记录" |
| `model_absence_is_visible_and_honest` | 关掉模型后：基础覆盖仍完成、交付为 partial、"没有模型参与"写明 |
| `cancel_stops_new_reports` | 取消后状态更新，报告数不再增长 |
| `no_page_or_server_errors` | 无页面错误、无 5xx |

命令行端的等价演示：`python scripts/material-review-demo.py --out <新目录>`，7 步
（上传 → 全部基础覆盖 + 准确补问并暂停 → 查看来源 → 只回答一部分 → 从正确位置继续
→ 显式确认后改记录 → 第三版报告）。

---

## 4. 来源与事实边界证明

四道各自独立的证据，锁的是同一件事：**普通补充不会隐式修改权威记录**。

1. **离线场景测试**：`ScenarioD`（材料说明 → 报告更新、`current_medications()` 逐字
   不变、答案标记 `recorded_as_reported` 且 `applied_authoritative=False`）、
   `ScenarioE`（显式确认 → 记录真的变了、留下 `authoritative_update` 记录、旧报告仍在）、
   `ScenarioC`（两条补问只答一条 → 另一条仍 open）。
2. **浏览器验收**：`plain_answer_does_not_write_record` —— 提交回答前后从夹具读出的
   药单**逐字相同**。
3. **端到端演示第 4 / 第 6 步**：第 4 步 `authoritative_writes: 0` 且药单不变；
   第 6 步 `authoritative_writes: 1` 且药单从 `氨氯地平 5mg` 变为 `10mg`。
4. **报告自身**：修订依据分三段写——`依据（材料）` / `依据（当前记录）：…发生了
   经确认的更新` / `依据（您的补充）：…（按您所说记录，**未**改变当前记录）`。

权威写入本身没有新代码：它走的是 `ProductStore` 既有的受控写入路径（参数校验、
预算、`BEGIN IMMEDIATE` 事务、幂等回执）。本轮只新增了**来源与前后值**的记录。

---

## 5. 必要回归和有限真实验收

### 5.1 工程正确性（离线）

- 全量收尾：**690 个测试 / 47 个套件，全部通过**（`output/mr-coverage-closeout-v2/`），
  4 个冻结评测（baseline-replay / gap-replay / gap-tools / gap-replay-negative）全部通过。
- 本轮四套 material-review 测试合计 **61 条**（原 36 + 新增 25）。
- 前端 `tsc -b` 与 `vite build` 通过。

### 5.2 基础覆盖率（代码完成）

离线场景里，模型**一条材料都没读**的情况下：

| 场景 | 结果 |
|---|---|
| A 三条材料字段完整 | 覆盖 8/8 有处置、`items_pending=0`、系统读取 3 条、模型读取 0 条、交付 **complete** |
| A' 其中一条不一致 | 覆盖完成，差异仍在第 3 节、`delivery` 仍可 complete（`evidence=verified`） |
| B 一条缺字段 | 其余照常完成；缺项被标 `insufficient` 并**带具体原因**；系统自建补问 → `waiting_input` |
| C 两条补问只答一条 | 只关闭被回答的那条，另一条仍 open；已完成的覆盖没有重做 |
| F 一条读不到 | 该条 `unreadable` 且带原因；不支撑任何结论；其余结果照常交付 |
| G 模型不可用 | 覆盖照常完成；交付 **partial** 并写明"这一版没有模型参与" |
| H 无需检索 | 0 次检索，交付 complete —— 零次检索**不是**失败 |

### 5.3 模型作用

真实批次（3 个任务，各 1 次）里模型被接受的决定数：**MC-L1 2 轮、MC-L2 1 轮、
MC-L3 1 轮**。它做的是判断，不是重算：MC-L1 在三个字段全部一致的报告上仍提出
"请确认氨氯地平的剂型和规格是否与当前记录一致"（材料里 form/strength 从未被确认
过，这条系统判不出来——它是对的）。三次都没有发起外部检索。

### 5.4 用户任务完整交付（有限真实验收，3 个任务）

`scripts/material-review-coverage-live.py --enable-live`，先冻结后跑，产物
`output/material-review-coverage-live/`（`FREEZE.json` + `RESULT.json`）。

**类别分列（不合并成一个"通过"）：**

| 类别 | 结果 |
|---|---|
| engineering（终态 / 报告齐全 / 无越权 / 新版本不覆盖旧版本） | 3/3 |
| coverage（每个覆盖要求有处置 / 材料由系统实际读取） | 3/3 |
| task_delivery（完整交付或**说得清**的部分交付） | 3/3 |
| waiting（等待补充是一次真的暂停） | 3/3 |
| model_role（模型确实参与了判断 / 读数与系统读取分开） | 3/3 |
| system_role（代码真的做了确定性工作） | 3/3 |
| resume（补问后从正确位置继续） | 1/1（只有 MC-L2 有补充步骤） |
| failure（无未解释的供应商故障） | 3/3（判据是报告里的 `degraded_reason`，不是预算读数） |

**原始读数**：三个任务都是 `waiting_input` + `partial`，`items_pending = 0`，
系统读取 3 / 3 / 4 条、模型读取 0 条，各 1 条未回答的补充请求。交付为 partial 的
原因在报告里说得出是哪一条。

### 5.5 这一批**没有测到**的东西（必须说清）

- **调用次数与 token 未测量。** 评分脚本按 `care-task:<id>:<n>` 自己拼 run_id，
  比真实 id 差一位，于是 `calls_attempted` / `tokens` 一律读成 0。那不是"没有调用"，
  是**读错了地方**；原始产物里没有可恢复的预算读数。
- **评分读法修过一次**（`material-review-coverage-scorer@1` → 同一版内的读取修正）：
  `second_report_new_version` 读的是快照 dict 自己的键，应当是 `second.report.id`；
  三项依赖补充步骤的检查被算在了没有补充步骤的任务上（空命题）。修的是**读取**，
  不是判定标准；`RESULT.json` 原样保留，修正后的结果写在同目录 `RESCORED.json`，
  两次数字都可回查。**没有追加新的提示词版本，也没有重跑到出现成功样本。**
- **限流次数同样未测量**：`rate_limited_calls` 与调用数从同一处读出来，因此
  `RESULT.json` 里的 `0` 和 `total_model_calls: 1` 一样**不能**采信。这一批既不能
  报"没有限流"，也不能报"有限流"。上一轮的限流（18 次调用里 5 次被拒）是那一轮的
  实测，与这一批无关。

### 5.6 交付之后修的三处（不影响上面已测结论）

1. **补充请求的对象是内部引用**：真实模型用了 `memory:medication:1@v1` 作为对象，
   页面会原样显示。现在这类引用在落库时换成药名（展示变了，判断没变），并有回归锁。
2. **报告正文与自身状态矛盾**：第 1 节写着"交付=尚无报告"，而它本身就是那份报告
   ——`sections` 在交付轴定下来之前算的。现在定轴后重算。
3. **自纠过的步骤被写成"执行限制"**：一次 `submit_question` 参数不完整被拒、模型
   下一步做成了，正文仍写着"工具 submit_question 未能成功执行（invalid_arguments）"。
   现在这类未执行、未触碰远端数据的参数问题只进审计记录，不进用户正文；文案也
   改成了业务语言。

这三处都是**显示与记录**层面的修正，不改变覆盖率、交付判定或任何第 5.2–5.4 节的数字。

---

## 6. 剩余问题（三个，按影响排序）

1. **"可确认属性"（剂型、规格）没有进覆盖处置，只留在句子里。**
   影响：材料里有 `form`/`strength` 时，系统把它记成"字段完整、与当前记录一致"，
   把"请核实剂型、规格"挂在同一句话的末尾，覆盖仍算 `covered`。照护者要读完整句
   才知道还有这一项要确认；真实模型每次都会把它捡起来问用户（三次运行里两次），
   说明它确实是活的未决项。**不阻断使用**（它确实写在报告里，模型也问了），但
   "已核对 N 项"这个数字偏乐观。下一步该做的是把这类属性作为覆盖要求的一种处置，
   而不是靠模型替系统发现它。

2. **这一批的调用与 token 成本没有测到。**
   影响：无法回答"每核对一份材料花多少次调用、多少 token"。数字缺失的原因是评分
   脚本读 run_id 差一位（已修），但那一批的原始预算读数不可恢复。下一次批次会有
   这个数字；**不补跑**，因为没有改善可报，补跑只会换一个样本。

3. **真实模型仍在"材料与记录不一致"处停下，不走到"说明书怎么说"。**
   影响：照护者拿到的是"哪里对不上、还要确认什么"，而不是外部依据。本轮按设计
   **不把零次检索判为失败**——普通材料核对本来就不要求查说明书；但用户**明确要求**
   "比较材料与某份说明书信息"时，那属于本次交付要求，目前真实模型会把它留成一条
   待确认问题而不是去查。报告会如实写出它没查到什么，所以不阻断使用，但它把
   "有界证据调查"的上限画在了材料核对这一层。
