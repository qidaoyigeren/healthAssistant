# material-review@2 交付说明（2026-09-13）

本轮把"以用户交付物为目标的证据调查"做成一条**完整可用的产品路径**：任务契约、
调查状态、模型接口、交付检查、增量更新，以及一条真实模型验收。

设计依据见同目录 [DESIGN.md](DESIGN.md)。本文件回答交付要求里的六件事。

---

## 1. 设计决策摘要

**任务契约**：新增 `material-review@2`（[stage0/review/contract.py](../../../stage0/review/contract.py)）。
完成标准来自本次任务自己声明的 `requested_outputs` 与 `coverage_requirements`，
不再来自 `medication-evidence-review@1` 那三条固定条件（authority /
interaction_evidence / applicability）。覆盖要求由代码从**用户选中的材料**与当前记录
确定性构造，所以"忽略一条差异"不会让任务变便宜。

**状态模型**：`gaps` 被拆成职责清楚的五类对象——`Question`（要查什么）、`Finding`
（已取得的发现）、`Assertion`（准备主张什么）、`InputRequest`（请用户补充）、
`ExecutionIssue`（执行出了问题）。运行 / 交付 / 证据拆成**三条独立轴**，
`ended + complete + conflicting` 是合法且成功的组合。

身份规则：问题 = **方面 + 主体**，断言 = **主体 + 谓词 + 限定条件**。措辞与药名
顺序不影响身份；同一药物在不同方面上的问题、同一主体在不同谓词下的断言，都不会
共享身份，因此也不会错误复用旧的支持判断。问题与断言的修订增加 `revision` 并使
旧判断失效；未解决的问题只能被关闭（要写清凭什么）或被取代，**没有删除操作**。

**模型权限**：模型只做调查策略。它看不到也填不了 scope、事实版本、预算、principal、
内部回执——那些由运行器附加。它可以读取材料、发起一次有界取证、提出补充请求、
提交候选问题/发现/断言、请求交付；**不能**改权威药单、改任务权限、改请求账本、
把自己的断言标成人工确认、或绕过完成检查。

**工具边界**：`research_evidence` 内部确定性完成检索、读回有界原文、校验 scope 与
哈希；它不改调查目标、不扩大数据范围、不自动批准断言、不把失败换成固定答案、
也不把规则生成的查询归因为模型选择。

**交付条件**：完成 = 每个覆盖要求都有处置 + 每项交付要求都在报告里 + 事实性结论
关联有效依据 + 已知差异/反对证据未遗漏 + 未解决项已写明 + 无越权输出。"完整"指
满足本次承诺的交付要求，不等于所有医学问题都有定论。

**取舍**：刻意**没有**做（a）模型的确定性降级替身——本契约的调查策略归模型，没有
模型时交付规则整理的准确部分并标明"未经模型核查"，而不是用脚本冒充调查；
（b）模型可见的内部 id 负担——问题引用可以写 id 也可以写问题原文，解析不到就丢弃
并如实回报，而不是把整个提案拒掉。

---

## 2. 实际代码变更

### 新增（`stage0/review/`，按职责拆分）

| 模块 | 职责 |
|---|---|
| [contract.py](../../../stage0/review/contract.py) | `TaskSpec`、交付要求、覆盖要求的构造规则、`material-review@2` |
| [state.py](../../../stage0/review/state.py) | 五类对象、三条轴、`MaterialReviewState` 与版本化 `restore()` |
| [context.py](../../../stage0/review/context.py) | 调用模型前的可信上下文准备；把确定性差异落成发现 |
| [capabilities.py](../../../stage0/review/capabilities.py) | 7 个领域能力的 ToolSpec + 处理器；`ReviewPlanner` 适配器 |
| [verify.py](../../../stage0/review/verify.py) | 断言—证据关系：结构化比较 / 原文摘录 / 语义解释三档 |
| [delivery.py](../../../stage0/review/delivery.py) | 交付检查与证据轴 |
| [report.py](../../../stage0/review/report.py) | 业务语言报告 + 版本间变化说明 |
| [incremental.py](../../../stage0/review/incremental.py) | 材料版本→证据→断言/发现→问题→报告版本的关系与受影响部分重算 |
| [advance.py](../../../stage0/review/advance.py) | `advance_task` 的**唯一**实现 |

### 复用（未改语义）

`EvidenceStore`、`ToolExecutor`/`ToolSpec`/权限与钩子、`NoProgressTracker`/进展事件/
取消、`TurnBudget`、`ProductStore` 回执与事务、`MaterialIndex`、`response_safety`、
`harness/retrieval.search`、`evidence_quality`+`claim_support`、`LLMPlanner` 的传输层
（重试、429 退避、`tool_choice` 降级、多调用裁剪、用量记账）。

### 产品接入

- [care_tasks.py](../../../stage0/care_tasks.py)：新增 `material_review` goal_type，
  与既有待办共用创建/恢复/取消/worker 租约/累计预算；新增 `publish()` 短事务发布。
- [server.py](../../../stage0/server.py)：`CARE_TASK_REVIEW_OPERATIONS` 前缀匹配，
  新增契约不会再因为漏改一处而永远"排队中"。
- 路由：`POST /v1/material-reviews`（助手入口，同步推进）、
  `GET /v1/material-reviews`、`GET /v1/material-review-reports/{id}`、
  `/v1/care-tasks/{id}/input` 支持 `review_request_ids`。
- 前端：[MaterialReviewCard.tsx](../../../frontend/src/features/tasks/MaterialReviewCard.tsx)
  + [CareTasksPage.tsx](../../../frontend/src/features/tasks/CareTasksPage.tsx) 的发起入口。
  三条轴分开显示；"查看来源"读报告交付的来源清单，不从正文里正则抠 id。

### 旧路径保留

`investigation@1` / `medication-evidence-review@1`、`reconcile_material`、
Harness P3 全部原样保留；`test_*` 历史回归全部继续通过。旧任务仍能被恢复和查看，
`contract_version` 校验未改。

---

## 3. 完整可操作的演示

```bash
python scripts/material-review-demo.py --out output/material-review-demo-final2
```

走真实 HTTP 入口、真实推进核心、真实证据库与交付检查，只把"模型"换成脚本替身
（`--model live` 可换真实供应商）。产物 `output/material-review-demo-final2/demo.{json,md}`：

1. **上传材料** → 代码算出 4 条确定性差异（不一致 / 字段不完整 / 记录里没有 /
   材料未列出），逐条与当前记录对齐。
2. **核对报告** → 六节齐全，模型读回材料原文、提出补充请求、声明自己的调查问题、
   发起一次有界取证、提交引用该片段的发现。
3. **查看来源** → 报告交付 `evidence_refs` / `material_refs`，页面用 `EvidenceDrawer`
   回读原文（演示里回读了 459 / 238 字的片段）。
4. **补充信息** → 用户在界面上填写"实际剂量是 10mg"，只关闭**这条**补充请求。
5. **报告修订** → 新报告（第 2 版）发布，旧报告仍在；第 7 节写明：
   - 新增：`氨氯地平：与当前记录一致…`（重算出来的）
   - 不再出现：`氨氯地平：与当前记录不一致…`（被取代，不是被删除）
   - 沿用：3 节中仍有条目与上一版相同
   - 依据：`medications=3 → 4`

强制安全检查（DDI 检出克拉霉素/氨氯地平）单独成节，不混进本次调查的发现。

---

## 4. 验收证据

### 4.1 确定性设计约束（离线，30 个测试）

`stage0/test_material_review.py`（15）锁设计约束本身：
普通材料核对不触发相互作用调查、同药名不同问题身份独立、问题/断言修订使旧判断
失效、差异是发现不是永久阻塞、非法来源不能继续支持结论、三条轴可独立组合、
确定性发现标注为 `origin=system`、模型候选不被当成已验证结果。

`stage0/test_material_review_flow.py`（11）锁推进核心：上下文里没有执行上下文参数、
覆盖未清时交付被拦且反馈**具体**、未读回原文的材料不能被引用、结构化断言说反了
会被判 `contradicted` 并从"事实"节移出、请求补充会暂停运行、恢复不重复副作用、
取消保留部分结果、新版本不覆盖旧报告。

`stage0/test_material_review_product.py`（4）锁产品入口：完整闭环（含增量更新）、
只重算受影响部分、**聊天入口与 worker 入口共用同一个核心**、模型不可用时仍交付
准确的部分结果。

全量离线收尾：**659 个测试 / 46 个套件，全部通过**（`output/mr-closeout-final3/`）。

### 4.2 产品流程

`scripts/material-review-demo.py` 端到端跑通，产物见上。前端 `tsc -b` 与 `vite build`
通过。

### 4.3 真实模型任务质量（冻结协议）

`scripts/material-review-live-acceptance.py`，跑前先把任务、评分标准、调用上限、
时间上限与停止条件写进 `FREEZE.json`。3 个代表任务，每个含"核对 → 补充 → 重新核对"。

**`output/material-review-live-5/`：3/3 任务通过全部 8 项冻结标准。**

| 任务 | 运行 | 交付 | 依据 | 模型调用 | 限流 | token | 墙钟 |
|---|---|---|---|---|---|---|---|
| MR-L1 | waiting_input | partial | verified | 5 | 0 | 37,166 | 35.2s |
| MR-L2 | waiting_input | partial | verified | 4 | 2 | 15,491 | 8.7s |
| MR-L3 | ended | partial | verified | 4 | 3 | 8,353 | 7.1s |

模型在这三个任务里做的事是**调查策略**：L1 读回一条材料原文并针对具体疑点提出
"请确认氨氯地平的剂型和规格是否与当前记录一致"，L2 提出"请确认氨氯地平的剂量单位
和频次并读回一条材料，L3 读回一条材料。代码在这些任务里做的事是把确定性差异一次
算完、准备上下文、执行并校验每一步、判定停止、生成两版报告与变化说明。

**要看清的是它没做什么**：三个任务都只读了 4 条材料里的 1 条，也都没有发起检索。

### 4.4 失败与未验证项

- **交付状态是 partial，不是 complete。** 三个任务都只读了 **4 条材料里的 1 条**，
  其余条目没有落到结论上。报告第 6 节如实列出未覆盖项——这正是"完整报告"不等于
  "所有问题都有答案"的实例，但不能读成"任务已完整完成"。
  另外 MR-L1 把同一条补充请求重复提交了 4 次，被 `no_progress` 停下；停得对，
  但那是本轮真实运行的常见结局。
- **真实模型仍未发起外部取证（`research_evidence` 0 次）。** 三个任务都停在
  "读材料 + 问用户"，没有一个走到"查说明书"。所以**取证这一档在真实模型上尚未
  被验证**；它的离线行为由 `test_material_review_flow.py` 与 demo 脚本覆盖
  （demo 里脚本模型确实走通了检索→读回→引用→核查）。
- **限流是真的**：18 次调用中 5 次被 429 拒绝（TPM）。本轮的运行状态把限流与供应商
  故障如实标成 `provider_error`，照护者能看到"未经模型核查"并重试；但**高负载下的
  通过率没有测量**。
- **评分读法修过一次**：`revision_explained` 初版读错了快照的键，导致该项
  "测不出来"而不是"测得没有"。修的是读取而不是判定标准；`live-1` 到 `live-5` 的
  原始产物全部保留，任何一次的结果都可以回查。
- **没有迁移任何旧任务**。旧 `evidence_review` / `reconcile_material` 待办仍走旧
  执行器；本轮没有测过"把一个旧待办变成新契约"。
- **前端只做了类型检查与构建**，没有跑浏览器级验收。

---

## 5. 资源使用

**模型决策发生在这里**（每轮一次调用）：选择下一步做哪个领域动作、写检索词、
决定问用户什么、提交什么候选问题/发现/断言、什么时候请求交付。

**运行器承担的机械步骤**（模型完全不做，也不为它们付费）：

| 原先由模型做 | 现在由代码做 |
|---|---|
| 先 `memory_read(snapshot)` 才能开始 | 每轮直接准备版本引用 + 事实摘要 |
| 列材料目录、自己对齐记录 | 已选材料的索引 + 确定性字段差异（含双方具名） |
| 检索后逐个 `read_evidence` 回读 | `research_evidence` 内部读回有界原文并校验来源 |
| 填 `gap_id` / `expected_observation` | 不需要；模型只给领域参数 |
| 判断"哪些事项还没做" | `completed` / `pending` 清单直接给出 |
| 判断能不能交付 | 交付检查返回**具体缺哪几项** |

**实测数字**（来自 `output/material-review-live-5/`，没有测到的不作估计）：

- 模型调用：3 个任务共 **18 次**（每任务 4–5 次，含 2 次重试），其中 **5 次被 429 拒绝**。
- token：37,166 / 15,491 / 8,353，合计 **61,010**。
- 墙钟：每任务 **7.1s / 8.7s / 35.2s**；整个批次约 51 秒。
- 离线全量收尾 659 个测试；前端构建 2.2 秒。

**没有测量**：限流为 0 时的通过率、单次调用的 token 拆分、与旧契约在同一个模型上的
对照。这些都不写进结论。

---

## 6. 剩余问题（三个，按影响排序）

1. **真实模型不发起外部取证，交付停在 partial。**
   影响：照护者拿到的是"材料与记录怎么对不上、还需要确认什么"，而不是"说明书里
   怎么说"。**不阻断当前场景使用**——报告明确写出了它没查到什么，也列了未覆盖项；
   但它把"有界证据调查"缩水成了"材料核对"。
   下一步该查的是接口而不是提示词：三个任务里模型一次都没有尝试 `research_evidence`，
   说明"什么时候值得去查"这条判断在它的上下文里还不够显眼。

2. **材料覆盖会被模型跳过，交付因此永远是 partial。**
   影响：报告是准确的，但"每个材料条目都落到结论"这条要求实际没被满足。
   不阻断使用（未覆盖项被逐条列出），但"完整报告"这个状态在本轮的真实运行里
   一次都没有出现过。需要判断的是：这该由交付检查更强地往回推（把未读条目直接
   反馈给模型继续读），还是该承认"部分报告"就是这条路径的常态。

3. **限流下的行为只做了诚实降级，没有提高吞吐。**
   影响：5/18 次调用被 429 拒绝，两个任务因此提前收尾。降级是安全的（不冒充核查、
   可重试），但在供应商限流严重时，用户拿到的是"未经模型核查"的部分结果。
   不阻断使用。真正的上限取决于供应商配额，不是本轮能改的东西。
