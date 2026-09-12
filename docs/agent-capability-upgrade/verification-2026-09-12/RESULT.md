# 可信验收轮 · 结果（2026-09-12）

对应 `DESIGN.md` / `PLAN.md`。全部结论都有可复跑的工件路径；**没有证据的结论一律
写成未达标**，不写成"基本符合"。

- 离线段（第一、二步）**已完成**。
- 模型增益与真实批次（第三、四步）状态见下文，**未完成的部分明确标注**。

---

## 一、本轮修复验收结果

逐条对照任务书的第一步。每条给出**判定**与可复跑的证据。

| # | 项目 | 判定 | 证据 |
|---|---|---|---|
| 1 | 新评分器已接入实际评测入口 | ✅ | `stage0/agent_evals/scoring.py`（`visitprep-eval@2`）；入口 `run_visitprep.evaluate` 是薄委托，`test_visitprep_scoring.DelegationTest` 锁定 |
| 2 | 报告质量 / 终态 / 自主达成 / 降级分别统计 | ✅ | 三条独立轴 + 五项互斥计数；实测分布见第二节 |
| 3 | 每次请求及重试受批次调用硬上限约束 | ✅ | `stage0/agent_evals/batch_budget.py`；额度在每次 dispatch 前由持久账本 `llm_attempts` 取用 |
| 4 | 材料新增实体进入规划范围 | ✅ | `InvestigationState.material_items` 唯一形状；测试走全链路（导入 CSV → `list_materials` → `observe` → `allowed_entities`），含反面对照 |
| 5 | 子问题修正后错误缺口正确关闭 | ✅ | `accept_questions` 成功时解析全部 `plan:*`；`plan_attempts` 改为**连续**被拒计数 |
| 6 | 新证据出现后合法修订计划 | ✅ | `revision_trigger()` 闸门 + 追加式 `plan_revisions`（含 `retained` 证据） |
| 7 | 具体结论需要对应证据支持 | ✅ | `stage0/claim_support.py`（`claim-support@1`），叠加在既有 scope 上不覆盖它 |

**回归**：`scripts/verify-agent-closeout.py` **42/42 通过**，覆盖 559 项测试。
每一步都在提交前跑过该门禁。

### 修复过程中发现并修掉的缺陷（计划之外）

七个，其中五个属**阻断级**——不修就无法继续：

1. **`material_conflict` 缺口永不关闭**：代码注释声明它是 finding（"something to
   report, not something that ends the review"），却没有任何工具能关它，而完成
   条件要求"无任何开放缺口"。任何读到材料差异的回合都只能以 `no_progress` 收尾。
2. **`read_evidence` 与完成条件互锁**：回读所有已检索原文是完成条件，但该工具只在
   证据缺口**开放**时下发。"缺口已关闭、原文未回读"正是最常见状态。
3. **"双方具名"被自己的安全边界拦下**：报告写出 `memory:medication:N`，最终响应
   校验判它 `fabricated_memory_ref`，报告根本发不出去（7/12 任务执行失败）。
4. **节检查从恒真翻成恒假**：Task 1 要求每节都有非占位条目，但占位句在"材料本就
   一致"时才是**正确**内容——那条检查会永远判失败，与恒真一样不可证伪。
5. **`plan_revision_capped` 又是一个永不关闭的缺口**（同第 1 类，我自己引入后立刻
   发现并列入非阻塞白名单）。
6. **异常路径丢失观测形状**：`except` 分支只重建 3 个键，崩溃被记成"没观察到分歧"。
7. **`search_budget` 声明了却没人执行**：两个任务声明了它，代码只读全局常量。
   现已在状态机生效，并有测试断言"数据集声明的每个键都真的被读"。

> 值得记下的是第 4、5 两条是**同一类**缺陷的两个方向：**一个条件写出来就永不成立
> 或永不失败**。只有把终态纳入评分之后，它们才暴露出来。

### 历史产物重评（旧数字不可比）

`python -m stage0.agent_evals.rescore --in-dir output/visit-prep-2026-09-12
--out-dir output/verification-2026-09-12/rescored`。旧产物**只读**，未触碰。

**全部旧产物记 `undetermined`**，原因：v2 的自主性轴要读 `subquestion_source`，
而上一轮的产物里没有这个字段。照 `None != 'model'` 判会把每一条旧记录读成降级——
那是把"没记"当成"不是模型"，属于**补造判定**。在场的那部分仍以 `partial` 给出，
但不产生 `bucket`（五计数是完整判定）。

| 文件 | 旧口径 passed | v2 可判定部分：报告质量 ok/不 ok | v2 终态 |
|---|---|---|---|
| `grid-scripted.json` | **12/12** | 8/4 | completed 4、stopped 7、waiting 1 |
| `offline-scripted.json` | **12/12** | 8/4 | completed 4、stopped 7、waiting 1 |
| `live-model-3.json` | 2/12 | 1/11 | stopped 10、completed 2 |
| `grid-det.json` | 0/12 | 0/12 | completed 10、stopped 1、waiting 1 |
| `grid-fixed.json` | 0/12 | （无该字段） | （无该字段） |

**读法**：旧口径下的 `scripted` 12/12，是被那 24 条恒真检查（12 条节标题匹配 +
12 条从不读取的冲突声明）抬起来的。这不是"口径变严"，是旧数字本来不构成证据。

明细见 `output/verification-2026-09-12/RESCORE.md`。

---

## 二、四个用户任务的逐条结果

四个用户任务正好对应数据集里的四个族。**离线段**用 `scripted` 臂——它是唯一同时
具备"材料可见"与"规划器"的臂（`fixed`/`det`/`*-nomat` 缺材料可见性，恒 0/12，
不构成对照）。

| 用户任务 | 族 | 结果 | 终态 | 报告质量 |
|---|---|---|---|---|
| ① 材料一致：说明一致范围与未核查范围 | `information_complete` | **2/2** | both `checks_completed` | 无失败 |
| ② 材料缺项：发现缺失、提出补问并支持继续 | `key_fact_missing` | **2/2** | both `checks_completed` | 无失败 |
| ③ 材料冲突：定位双方原文、说明分歧与待确认事项 | `multi_source_conflict` | **2/2** | `waiting_review` ×1、`checks_completed` ×1 | 无失败 |
| ④ 初次搜索无结果：调整查询或明确交付部分结果 | `first_search_empty` | **1 完整 + 1 按设计交付部分结果** | `checks_completed` ×1、`budget_insufficient` ×1 | 无失败 |

③ 的 `waiting_review` 与 ④ 的 `budget_insufficient` 都是**该任务声明允许**的终态：
未决冲突交给人工复核、预算被压到 1 次检索时如实交付部分结果，都是正确的产品
行为。`complete` 也因此接受 `waiting`（DESIGN §3），而计数
`autonomous_without_degradation` 仍要求 `completed`——两个判据不同是有意的。

> **口径提醒**：这四个族是**作者自写的合成开发集**，作者参与调试，**不是**独立
> held-out。"2/2"不构成泛化主张。

> **产品入口**：以上结果由 `run_visitprep` 驱动 agent 得到，**不是** HTTP 入口。
> `/materials` 与 `/tasks` 的离线上传→调查→等待复核→取消流程由
> `test_product_full_flow.py`（4 项）与 `test_product_p1.py`（10 项）覆盖并通过，
> 但**浏览器端的实际点击验收未做**（见第五节）。

---

## 三、模型增益

**判定：未证明。** 本节给出的是**为什么未证明**，以及已经排除的解释。

离线段能确定的事：

| 观察 | 数值 | 说明 |
|---|---|---|
| 无材料可见性 | 0/12（`det-nomat`/`scripted-nomat`/`fixed`） | 材料可见性是**合取性必要条件** |
| 有材料 + 有规划器 | 10/12（`scripted`） | 契约**可满足** |
| 有材料 + 无规划器 | 0/12（`det`） | 确定性规划器从不调用 `list_materials` |

但这只证明了**执行契约可满足**，**不能**记作模型能力：`scripted` 臂是**脚本替身**，
它的"决策"由 `model_double` 决定，不含任何模型推理。三类角色必须分开记：

- **脚本替身**（`scripted`）：证明契约可满足。10/12。
- **模型纠错**（规划器被拒后按约束重提）：离线段无证据。
- **模型自主决策**（无人为脚本、由模型自行选择工具与查询词）：离线段无证据。
- **规则接管**（`code_default` / `policy_fallback`）：由评分器单独记为
  `degraded_outcome`，不与上面三项混计。

**关键证据缺口**：上一轮与本轮的真实模型产物都缺 `subquestion_source`，
而 v2 的自主性轴依赖它。**在真实批次补齐并记录该字段之前，"模型是否有增益"这个
问题无法回答**——已有的数字既不能证实也不能证伪。

---

## 四、当前适合开放给用户的范围

**结论：尚不适合开放自主调查，可以开放"材料核对 + 有界报告"的只读部分，
但需以人工复核为交付前提。**

依据：

1. **能力边界是合取性的**：材料可见 + 规划器两者缺一即 0/12。当前真实模型路径
   的表现**未被证明**（第三节），而离线替身的表现不构成模型证据。
2. **产品开关仍是关闭的**：`AGENT_INVESTIGATION_ENABLED` 保持默认关闭，
   这不是保守，是"未证明"的正确表达。
3. **安全边界完好**：本轮的每一处改动都没有削弱既有约束——`PlannerPolicyGuard`
   的意外性不变、`EvidenceStore` 的来源/哈希/作用域校验不变、写操作幂等回执不变；
   门禁 42/42 覆盖 559 项测试。
4. **可以开放的部分**：上传材料 → 看到**确定性差异**（`recompute()` 算出的
   kind/issues/来源坐标）→ 人工确认。这一段不依赖模型，且现在有"双方具名"的差异
   描述，比上一轮更可核对。
5. **必须写明的限制**：`insufficient`/`unknown` 不等于无风险；未列药物不代表停药；
   系统不做诊断、处方或剂量调整。

---

## 五、剩余最重要的三个问题及优先级

### P0 · 真实模型批次尚未产生可用证据

第三节的证据缺口直接卡住"是否开放"这个决定。更麻烦的是：上一轮的产物**没有记录
决定自主性的字段**，所以那批数据**永久不可用**——不是"需要重跑"，是"当时没记，
现在无从判断"。

**下一步**：跑一次记录完整的真实批次（`subquestion_source`、`attribution`、
`provider_attempts` 全量落盘），并用它回答第三节的问题。**限流即停，不靠重试抬
通过率**。

### P0 · 浏览器端未验收

任务书第四步要求的"通过 `/materials` 与 `/tasks` 验证上传、调查、补问后继续、
取消、部分报告、引用查看与降级展示"**未执行**。离线测试覆盖了后端流程，但
"用户实际点得到、看得懂"没有被验证过。这是本报告里**最大的一块未做**。

### P1 · 两处已知但未处理的风险

- **`source_invalid` 缺口同样永不 resolve**，与已修的两个死锁同类。目前没有任务
  触发它，所以未被观测到；应当在真实批次前补上（或显式列入非阻塞白名单）。
- **`claim_id = digest(list(entities))` 对顺序敏感**：同实体集换个顺序就是另一个
  claim，其 assessments 不会被保留。已用测试钉住这个边界，但它意味着"修订时换个
  顺序写"会让上一轮已读的证据白读。是否要把 digest 改成对顺序不敏感，是一个需要
  单独论证的决定（会影响已持久化的 claim id），本轮**没有**擅自改。

---

## 附：工件路径

| 内容 | 路径 |
|---|---|
| 评分协议 | `stage0/agent_evals/scoring.py` |
| 批次额度 | `stage0/agent_evals/batch_budget.py` |
| 重评入口 | `stage0/agent_evals/rescore.py` |
| 证据支持 scope | `stage0/claim_support.py` |
| 重评产物 | `output/verification-2026-09-12/rescored/` |
| 重评对照表 | `output/verification-2026-09-12/RESCORE.md` |
| 离线各臂 | `output/verification-2026-09-12/arms/` |
| 门禁 | `scripts/verify-agent-closeout.py`（42/42） |
