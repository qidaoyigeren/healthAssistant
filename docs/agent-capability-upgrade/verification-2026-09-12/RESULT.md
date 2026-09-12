# 可信验收轮 · 结果（2026-09-12）

对应 `DESIGN.md` / `PLAN.md`。全部结论都有可复跑的工件路径；**没有证据的结论一律
写成未达标**，不写成"基本符合"。

- 第一步（本轮修复）**7/7 通过**；第二步四个用户任务**离线段与真实段都已取数**。
- 第三步：**模型增益未达标**，瓶颈已定位（第三节）。
- 第四步：**真实批次已跑**（受控、零限流、63/80 调用）；**浏览器端点击验收未做**。

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

### 真实模型臂（同一批任务）

| 用户任务 | 结果 | 终态 | 报告质量 |
|---|---|---|---|
| ① 材料一致 | 0/2 | `budget_insufficient` ×2 | 1 例第 5 节空 |
| ② 材料缺项 | 0/2 | `budget_insufficient` ×2 | 2 例未报告预期分歧 |
| ③ 材料冲突 | 0/2 | `budget_insufficient` ×2 | 1 例材料索引从未读取 |
| ④ 初次搜索无结果 | 0/2 | `budget_insufficient` ×2 | 无失败 |

**真实臂 0/8。** 瓶颈不是报告写得好不好，是**取证这一步根本没发生**——见第三节。
报告质量失败里那 2 例"未报告预期分歧"与 1 例"材料索引从未读取"，都是同一件事的
下游表现：模型读了材料清单却没有读回任何一条差异。

> **口径提醒**：这四个族是**作者自写的合成开发集**，作者参与调试，**不是**独立
> held-out。"2/2"不构成泛化主张；真实臂的 0/8 同样只对这 8 个任务成立。

> **产品入口**：以上结果由 `run_visitprep` 驱动 agent 得到，**不是** HTTP 入口。
> `/materials` 与 `/tasks` 的离线上传→调查→等待复核→取消流程由
> `test_product_full_flow.py`（4 项）与 `test_product_p1.py`（10 项）覆盖并通过，
> 但**浏览器端的实际点击验收未做**（见第五节）。

---

## 三、模型增益

**判定：未达标。瓶颈已定位到具体一步，不是"表现不好"这种说法。**

### 真实批次（受控）

| 项 | 值 |
|---|---|
| 端点 | `siliconflow` / `Qwen/Qwen2.5-7B-Instruct`（已授权列表内，`assert_live_authorized` 通过） |
| 臂 | `model`（材料可见 + 真实模型规划） |
| 任务 | 四个用户任务族的全部 8 个（`--only` 冻结） |
| 调用预算 | `--call-cap 80`，**实际花费 63**，剩余 17 |
| 未采样 | **无**（8/8 都跑到了终态） |
| 结果 | **0/8 通过**，8/8 `degraded_outcome` |
| 请求账目 | 52 次成功响应、3 次超时（`APITimeoutError`）；单次延迟 min 1130ms / median 2553ms / max 60023ms |
| 限流 | **0 次 429** |

> **一个被排除的解释**：上一轮把"真实模型不达标"归因于网关 429 限流。这一批
> **一次限流都没有**，所以那个解释在这里不成立——本轮的不达标不是限流造成的。

### 具体瓶颈：模型从不进入取证这一步

8/8 任务的动作序列里**只出现三种工具**：`memory_read`、`plan_questions`、
`list_materials`。**`rag_search`、`read_evidence`、`read_material_item` 一次都没有
被调用**（8 × 0 次）。

后果是链条式的：没有回读任何证据 → claim 全部停在 `insufficient` →
`interaction_evidence` 检查永远到不了 `checked` → `checks_completed` 不可达 →
周期被反复调用 `list_materials` 耗尽 → 以 `budget_insufficient` 收尾
（降级原因：`budget_reserved_for_wrapup` ×5、`budget_exhausted:usage_unknown` ×3）。

**这不是"工具没给"**：已实测确认，`plan_questions` 被接受后 `allowed_tools` 立即
包含 `rag_search` 与 `ddi_check`，且当时有两个开放的 `evidence_missing` 缺口。
工具在手上，模型没有选它们。

典型轨迹（`vp-noresult-005b`）：

```
0  rejected             memory_read    query="current_medications"  ← 安全拒绝
1  llm_post_correction  memory_read    query="snapshot"             ← 模型自行改正
2  llm                  plan_questions 2 条子问题，覆盖两种药
3-4 llm                 memory_read    ×2（无新增信息）
5-9 llm                 list_materials ×5（无新增信息）
   → 周期耗尽
```

### 三类角色分开记

- **脚本替身**（`scripted` 臂，离线）：证明**执行契约可满足**，10/12。**不含**任何
  模型推理，不得记作模型能力。
- **模型纠错**：**有 1 例**。`memory_read` 因 `authority_requires_full_memory_read`
  被拒后，模型自己把 query 改成 `snapshot` 并被接受（`llm_post_correction`）。
- **模型自主决策**：8/8 任务的子问题声明均来自模型（`subquestion_source='model'`，
  且经覆盖度校验），这一项**成立**。
- **规则接管**：本批 0 例（无 `code_default`、无 `policy_fallback`）。

### 题面所问的"策略调整"

> 改变关键证据后行动与结论是否合理改变；模型是否根据**工具结果**调整查询或修订计划。

**未发现**，且是明确的无。

- 模型**没有**根据工具结果改写过查询：它从未发起过检索，也就谈不上改写。
- 模型**没有**修订过计划：`plan_revisions` 在 8/8 任务中均为空。
- 唯一的一次调整是**安全拒绝之后的纠正**（上表第 0→1 步），那属于"按约束改正"，
  与本轮要观测的"证据变了就重新规划"是两回事——把它记成策略调整会是**混淆归因**。

`first_search_empty` 族（考察"首搜无果 → 改写查询"）本批**未能提供证据**：
模型一次检索都没发起，所以"首搜无果"这个前提根本没有出现。

### 附带发现的产品缺陷

**重复无效工具不计入 no-progress。** 现有的 `no_progress_count` 只统计**重复的
`rag_search` 查询**；连续 5 次 `list_materials` 或 9 次 `memory_read`
（`vp-conflict-003a` 实际发生）不算。于是"原地打转"被记成 `budget_insufficient`
（"预算用完了"），而事实是"它没在做新事"。这与本轮在评分侧修掉的是**同一类**
标签失真。修法方向明确：按"这次观测是否带来新信息"计 no-progress，而不是按工具名。

---

## 四、当前适合开放给用户的范围

**结论：尚不适合开放自主调查，可以开放"材料核对 + 有界报告"的只读部分，
但需以人工复核为交付前提。**

依据：

1. **真实臂 0/8，且瓶颈已定位**（第三节）：模型不进取证这一步，报告就永远建立在
   未回读的证据上。这是**能力**问题，不是调参问题——换更长的周期预算只会让它多
   重复几次 `list_materials`。
2. **产品开关仍是关闭的**：`AGENT_INVESTIGATION_ENABLED` 保持默认关闭，
   这不是保守，是"未达标"的正确表达。
3. **安全边界完好**：本轮的每一处改动都没有削弱既有约束——`PlannerPolicyGuard`
   的意外性不变、`EvidenceStore` 的来源/哈希/作用域校验不变、写操作幂等回执不变；
   门禁 42/42 覆盖 559 项测试。真实批次里安全拒绝**确实生效并拦下了一次**
   （`authority_requires_full_memory_read`）。
4. **可以开放的部分**：上传材料 → 看到**确定性差异**（`recompute()` 算出的
   kind/issues/来源坐标）→ 人工确认。这一段不依赖模型，且现在有"双方具名"的差异
   描述，比上一轮更可核对。
5. **必须写明的限制**：`insufficient`/`unknown` 不等于无风险；未列药物不代表停药；
   系统不做诊断、处方或剂量调整。

---

## 五、剩余最重要的三个问题及优先级

### P0 · 模型不进入取证这一步（本轮唯一的实质能力缺口）

8/8 任务从未调用 `rag_search` / `read_evidence` / `read_material_item`，工具可用、
缺口开放、预算充足（用了 63/80、零限流），模型没有选。**这是本轮最重要的发现，
也是唯一挡住"开放"的一件事。**

**下一步**（按性价比排序）：

1. 先查**契约可见性**：`plan_questions` 之后的 payload 里，`rag_search` 出现在
   `tool_catalog` 中是否足够显眼？本轮已确认它在 `allowed_tools` 里，但"在允许
   列表里"与"模型看得见它现在是主线"是两回事——上一轮"漏 query"的真根因就是
   不透明 arguments，同类问题值得先排除。
2. 再把**重复无效工具计为 no-progress**（见第三节末）：现在连续 5 次
   `list_materials` 只消耗周期，不触发任何纠正反馈。修好之后，原地打转会立刻拿到
   "这一步没有带来新信息，请换一个动作"的约束式反馈，而不是把预算烧完。
3. 最后才考虑换模型或加预算——在 1、2 之前做这两件事，等于用更长的绳子量同一个
   坑。

### P0 · 浏览器端未验收

任务书第四步要求的"通过 `/materials` 与 `/tasks` 验证上传、调查、补问后继续、
取消、部分报告、引用查看与降级展示"**未执行**。离线测试覆盖了后端流程
（`test_product_full_flow.py`、`test_product_p1.py` 等，门禁内通过），但
"用户实际点得到、看得懂"没有被验证过。这是本报告里**最大的一块未做**。

### P1 · 两处已知但未处理的风险

- **`source_invalid` 缺口同样永不 resolve**，与已修的两个死锁同类。目前没有任务
  触发它，所以未被观测到；应当在下一批真实测试前补上（或显式列入非阻塞白名单）。
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
| 真实批次（四个用户任务） | `output/verification-2026-09-12/live/model-four-tasks.json` |
| 门禁 | `scripts/verify-agent-closeout.py`（42/42，559 项测试） |

（`output/` 在本仓被 gitignore，属于本地工件；`docs/` 下的文档入库。）
